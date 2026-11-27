"""Data-blind assertions, probes and snapshots through pbz-engine, with budgets and constant limits."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import desktop, dpapi, identity, lexers, policy as policy_mod, tmdl, tokens
from .classify import Classifier
from .common import (
    PbzError,
    _reparse_link,
    create_kill_on_close_job,
    engine_call,
    is_snapshot_result_doc,
    is_snapshot_spec_doc,
    list_names,
    read_checked,
    remove_file,
    replace_bytes,
)

ENGINE_KINDS = {"executes", "nonblank", "nonempty", "equal", "additive", "unique", "no_orphans",
                "no_blank_keys", "sign", "subset"}
ALL_KINDS = ENGINE_KINDS | {"differs", "filter_independent", "compiles", "rls", "perf"}
EXPR_FIELDS = ("expr", "left", "right", "column", "from", "to", "of")
RLS_EXPECT = ("EMPTY", "NON_EMPTY", "LESS", "EQUAL", "OLS_INACCESSIBLE")
_RLS_TEXT = ("query", "role", "user", "column", "table")
_CHECK_STATUS = {"PASS", "FAIL", "EMPTY", "UNSUPPORTED", "ERROR"}
_REFRESH_STATUS = frozenset({"PASS", "ERROR", "UNSUPPORTED"})

CHECK_REQUIRED = {
    **{k: ("expr",) for k in ("executes", "nonblank", "nonempty", "sign", "compiles")},
    **{k: ("left", "right") for k in ("equal", "differs")},
    **{k: ("column",) for k in ("unique", "no_blank_keys")},
    "additive": ("expr", "by"), "no_orphans": ("from", "to"),
    "subset": ("column", "of"), "filter_independent": ("expr", "column"),
}


def check_schema() -> dict:
    """Public, dependency-free schema for check files. `subset.of` names a column.

    `rls` and `perf` are separate branches. role, query, user and baseline are not expression fields.
    """
    branches = [{"type": "object", "required": ["kind", *required], "properties": {
        "kind": {"const": kind}, "id": {"type": "string", "minLength": 1},
        **{k: {"type": "string", "minLength": 1} for k in EXPR_FIELDS},
        "table": {"type": "string", "minLength": 1},
        "by": {"type": "array", "items": {"type": "string", "minLength": 1},
               **({"minItems": 1} if kind == "additive" else {})},
        "where": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "tol": {"type": "number", "minimum": 0},
        "expect": {"type": "array", "items": {"enum": ["NEG", "ZERO", "POS", "BLANK"]}},
    }} for kind, required in CHECK_REQUIRED.items()]
    branches.append({"type": "object", "required": ["kind", "expect"], "properties": {
        "kind": {"const": "rls"}, "id": {"type": "string", "minLength": 1},
        "expect": {"enum": list(RLS_EXPECT)},
        **{k: {"type": "string", "minLength": 1} for k in _RLS_TEXT},
    }})
    branches.append({"type": "object", "required": ["kind", "query"], "properties": {
        "kind": {"const": "perf"}, "id": {"type": "string", "minLength": 1},
        "query": {"type": "string", "minLength": 1}, "expect": {"const": "NOT_SLOWER"},
    }})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "required": ["checks"], "properties": {"checks": {"type": "array", "items": {"oneOf": branches}}}}


def _human_terminal() -> bool:
    """Local copy of the CLI human gate. Do not import pbz.cli (that imports this module)."""
    return sys.stdin.isatty() and sys.stdout.isatty() and not any(
        os.environ.get(name) for name in ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM"))


def _rls_synthetic(ws, project, pol: dict) -> bool:
    """True only for a verified pbz-synth tree (policy, synth.json, and the lineage HMAC).

    A policy flag or an annotation named PBZ_SYNTHETIC is not enough: either can be written
    onto a real model. `project` and `pol` stay in the signature for callers.
    """
    del project, pol
    if ws is None or not getattr(ws, "root", None):
        return False
    from . import synth
    return synth._verified_owned(ws, ws.root)


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _perf_check(ws, project, raw: dict, cid: str) -> dict:
    """perf.expect spends the probe budget. This does not also spend a check."""
    from . import perf
    query = raw.get("query") if isinstance(raw.get("query"), str) else ""
    try:
        got = perf.expect(ws, project, query, check_id=cid)
    except PbzError as err:
        return {"id": cid, "status": "ERROR", "class": _class_of(err.code, _PY_CLASS) or "OTHER"}
    status = got.get("status") if got.get("status") in _CHECK_STATUS else "ERROR"
    verdict_ = got.get("verdict") if got.get("verdict") in ("FASTER", "SAME", "SLOWER", "NOISY", "UNAVAILABLE") else "UNAVAILABLE"
    return {"id": cid, "status": status, "verdict": verdict_}


_ERROR_CLASS = frozenset({
    "SYNTAX", "UNKNOWN_OBJECT", "TYPE_MISMATCH", "CONVERSION", "CIRCULAR", "DUPLICATE_KEY",
    "TIMEOUT", "ENGINE_UNAVAILABLE", "BAD_REQUEST", "OTHER",
})
_PY_CLASS = _ERROR_CLASS | frozenset({"HUMAN_ONLY", "ENGINE_MISMATCH", "REFUSED", "UNSAFE", "NO_MIRROR", "NOT_FOUND"})
_LOG_ID = re.compile(r"^e-[0-9]{8}-[0-9]{4,}$")
_COUNT_BAND = frozenset({"0", "1-9", "10-99", "100-999", "1k-9k", "10k+"})
_MISMATCH = frozenset({"NONE", "ONE", "FEW", "MANY"})
_SIGN = frozenset({"NEG", "ZERO", "POS", "BLANK"})


def _class_of(value, allowed: frozenset) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _rls_clean(row: dict, allowed_ids: set[str]) -> dict | None:
    ident = row.get("id")
    if not isinstance(ident, str) or ident not in allowed_ids:
        return None
    status = row.get("status") if row.get("status") in _CHECK_STATUS else "ERROR"
    out = {"id": ident, "status": status}
    klass = _class_of(row.get("class"), _ERROR_CLASS)
    if klass is not None:
        out["class"] = klass
    elif status == "ERROR":
        out["class"] = "OTHER"
    return out


def _closed_engine_row(row: dict, allowed_ids: set[str]) -> dict | None:
    """Engine check rows: submitted ids and the contract fields only."""
    ident = row.get("id")
    if not isinstance(ident, str) or ident not in allowed_ids:
        return None
    status = row.get("status") if row.get("status") in _CHECK_STATUS else "ERROR"
    out = {"id": ident, "status": status}
    klass = _class_of(row.get("class"), _ERROR_CLASS)
    if klass is not None:
        out["class"] = klass
    elif status == "ERROR":
        out["class"] = "OTHER"
    log_id = row.get("log_id")
    if isinstance(log_id, str) and _LOG_ID.fullmatch(log_id):
        out["log_id"] = log_id
    if row.get("mismatch") in _MISMATCH:
        out["mismatch"] = row["mismatch"]
    if row.get("offenders_bucket") in _COUNT_BAND:
        out["offenders_bucket"] = row["offenders_bucket"]
    if row.get("sign") in _SIGN:
        out["sign"] = row["sign"]
    if row.get("note") == "differs":
        out["note"] = "differs"
    groups = row.get("groups")
    if isinstance(groups, list) and groups:
        out["groups"] = groups
    return out


def validate_spec(spec) -> dict:
    """Check request shape before opening private stores or charging disclosure budget.

    Diagnostics disclose field paths and closed messages, never submitted expressions/values.
    Runtime DAX/object validity remains the engine's responsibility.
    """
    issues = []

    def issue(index, field, message):
        issues.append({"id": f"c{index + 1}", "path": f"checks[{index}].{field}",
                       "message": message})

    if not isinstance(spec, dict) or not isinstance(spec.get("checks"), list):
        return {"valid": False, "findings": [{"id": "spec", "path": "checks",
                                              "message": "checks must be an array"}]}
    seen = set()
    for index, check in enumerate(spec["checks"]):
        if not isinstance(check, dict):
            issue(index, "kind", "each check must be an object")
            continue
        ident = check.get("id", f"c{index + 1}")
        if not isinstance(ident, str) or not ident.strip():
            issue(index, "id", "id must be a nonempty string")
        elif ident in seen:
            issue(index, "id", "check ids must be unique")
        else:
            seen.add(ident)
        kind = check.get("kind")
        if kind == "rls":
            if check.get("expect") not in RLS_EXPECT:
                issue(index, "expect", "rls expect is EMPTY, NON_EMPTY, LESS, EQUAL or OLS_INACCESSIBLE")
            for field in _RLS_TEXT:
                if field in check and not _nonempty(check.get(field)):
                    issue(index, field, "field must be a nonempty string")
            continue
        if kind == "perf":
            if "expect" in check and check.get("expect") != "NOT_SLOWER":
                issue(index, "expect", "perf expect is NOT_SLOWER")
            if not _nonempty(check.get("query")):
                issue(index, "query", "required field must be a nonempty string")
            continue
        if not isinstance(kind, str) or kind not in CHECK_REQUIRED:
            issue(index, "kind", "unknown check kind")
            continue
        for field in CHECK_REQUIRED[kind]:
            value = check.get(field)
            if field == "by":
                if not isinstance(value, list) or not value:
                    issue(index, field, "additive requires a nonempty grouping list")
            elif not isinstance(value, str) or not value.strip():
                issue(index, field, "required field must be a nonempty string")
        for field in (*EXPR_FIELDS, "table"):
            if field in check and (not isinstance(check[field], str) or not check[field].strip()):
                issue(index, field, "field must be a nonempty string")
        for field in ("where", "by"):
            if field in check and (not isinstance(check[field], list) or
                                   any(not isinstance(x, str) or not x.strip() for x in check[field])):
                issue(index, field, "field must be an array of nonempty strings")
        if "tol" in check and (isinstance(check["tol"], bool) or not isinstance(check["tol"], (int, float)) or
                               (isinstance(check["tol"], float) and not math.isfinite(check["tol"])) or check["tol"] < 0):
            issue(index, "tol", "tolerance must be a finite nonnegative number")
        if "expect" in check and (kind != "sign" or not isinstance(check["expect"], list) or
                                  any(x not in ("NEG", "ZERO", "POS", "BLANK") for x in check["expect"])):
            issue(index, "expect", "sign expectations must use closed sign categories")
    return {"valid": not issues, "findings": issues}


def _state_path(ws) -> str:
    return os.path.join(ws.private, "state.json")


# Fixed text only. JSONDecodeError echoes a snippet of the file, and an OSError can name the
# path. Never put either in the detail.
_STATE_MALFORMED = "checks state is malformed"

LEDGER_DAYS = 30
DAILY = {"checks": ("session_checks", 300), "probes": ("probes_per_day", 100),
         "snapshot_queries": ("snapshot_queries_per_day", 200), "smoke": ("smoke_runs_per_day", 50)}


def _read_state_file(path: str):
    """Saved object, {} if the file is missing, or None if it cannot be read or the JSON is corrupt.

    FileNotFoundError stays a fresh budget. Any other OSError is None so the caller refuses
    without replacing the file. Parser and OS errors stay here: their text must not leave on
    an exception chain. Reparse points raise UNSAFE_PATH via read_checked.
    """
    root = os.path.dirname(os.path.abspath(path)) or "."
    try:
        raw = read_checked(path, root=root)
    except PbzError as err:
        if err.code == "UNSAFE_PATH":
            try:
                os.lstat(path)
            except FileNotFoundError:
                return {}
            raise
        return None
    except OSError:
        return None
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _load_state(ws) -> dict:
    """Daily counters (reset each day) plus a persistent ledger: distinct constants per target fingerprint
    for LEDGER_DAYS and hashes of literals the agent introduced through pushes.

    A missing file is a fresh budget. A file that exists but cannot be read, or is not a JSON
    object, is refused and left unchanged so a locked or corrupt ledger is not replaced.
    """
    today = dt.date.today().isoformat()
    loaded = _read_state_file(_state_path(ws))
    if loaded is None:
        raise PbzError("REFUSED", _STATE_MALFORMED)
    st = loaded
    ledger = st.get("ledger") or {"constants": {}, "authored": []}
    for th, hs in (st.get("constants") or {}).items():  # migrate the old daily constants
        if st.get("day") == today:
            ledger["constants"].setdefault(th, []).extend({"h": h, "day": today} for h in hs)
    if st.get("day") != today:
        st = {"day": today, "per_target": {}}
    st.pop("constants", None)
    for k in DAILY:
        st.setdefault(k, 0)
    cutoff = (dt.date.today() - dt.timedelta(days=LEDGER_DAYS)).isoformat()
    ledger["constants"] = {th: [e for e in es if e.get("day", today) >= cutoff]
                           for th, es in ledger["constants"].items()}
    ledger["constants"] = {th: es for th, es in ledger["constants"].items() if es}
    st["ledger"] = ledger
    return st


def _lock_fd(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.02)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


class _StateLock:
    """Exclusive lock file beside state.json, held across a read-modify-write."""

    def __init__(self, ws, name=None):
        self.ws = ws
        self.name = name
        self.fd = -1
        self.path = ""

    def __enter__(self):
        if _reparse_link(self.ws.private):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        os.makedirs(self.ws.private, exist_ok=True)
        if _reparse_link(self.ws.private):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        self.path = os.path.join(self.ws.private, self.name) if self.name else _state_path(self.ws) + ".lock"
        if _reparse_link(self.path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            if _reparse_link(self.path) or not stat.S_ISREG(os.fstat(self.fd).st_mode):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            if os.fstat(self.fd).st_size < 1:
                os.write(self.fd, b"\0")
            _lock_fd(self.fd)
        except Exception:
            os.close(self.fd)
            self.fd = -1
            raise
        return self

    def __exit__(self, *exc):
        fd, path = self.fd, self.path
        self.fd = -1
        if fd >= 0:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        if path and self.name is None:
            try:
                os.remove(path)
            except OSError:
                pass
        return False


def spend(ws, kind: str, n: int = 1) -> bool:
    """Charge n units of a daily disclosure budget (checks, probes, snapshot_queries, smoke). False when spent."""
    from . import policy as policy_mod
    budgets = policy_mod.load(ws).get("budgets", {})
    with _StateLock(ws):
        st = _load_state(ws)
        key, default = DAILY[kind]
        if st[kind] + n > int(budgets.get(key, default)):
            return False
        st[kind] += n
        _save_state(ws, st)
        return True


def record_authored(ws, before: dict, after: dict) -> int:
    """Remember (as hashes) the literals a push introduced into DAX / M, so later checks that reach them
    through measure definitions count them as constants. before/after: {rel: sanitized text}."""
    def lits(text: str, rel: str) -> set[str]:
        out = set()
        doc = tmdl.parse(rel, text)
        for reg in doc.regions:
            if reg.kind not in ("dax", "m"):
                continue
            for t in (lexers.lex_dax if reg.kind == "dax" else lexers.lex_m)(text[reg.start:reg.end]):
                if t.kind == "str" and t.value and not tokens.TOKEN_RE.search(t.value):
                    out.add(t.value)
                elif t.kind == "num" and t.text not in ("0", "1"):
                    out.add(t.text)
        return out

    new = set()
    for rel, text in after.items():
        if rel.lower().endswith(".tmdl") and text is not None:
            new |= lits(text, rel) - (lits(before[rel], rel) if before.get(rel) else set())
    if not new:
        return 0
    with _StateLock(ws):
        st = _load_state(ws)
        authored = set(st["ledger"]["authored"]) | {_h(x) for x in new}
        st["ledger"]["authored"] = sorted(authored)[-5000:]
        _save_state(ws, st)
    return len(new)


def _reached_literals(model, res, expr: str, home: str) -> set[str]:
    """Literals inside the definitions of every measure the expression reaches (transitively)."""
    from . import deps
    out, seen, todo = set(), set(), [(expr, home)]
    while todo:
        text, tab = todo.pop()
        for t in lexers.significant(lexers.lex_dax(text)):
            if t.kind == "str" and t.value and not tokens.TOKEN_RE.search(t.value):
                out.add(t.value)
            elif t.kind == "num":
                out.add(t.text)
        for ref in deps.dax(text, tab, res).refs:
            if ref.kind == "measure" and (ref.table, ref.name) not in seen:
                seen.add((ref.table, ref.name))
                m = model.table(ref.table).measures.get(ref.name) if model.table(ref.table) else None
                if m is not None:
                    todo.append((m.expression, m.table))
    return out


def _save_state(ws, st: dict) -> None:
    """Write state.json via a temp file in the same directory, then os.replace.

    The destination is not opened for write, so a crash mid-write cannot truncate the ledger.
    """
    os.makedirs(ws.private, exist_ok=True)
    path = _state_path(ws)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".pbz-state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(st, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def model_literals(project) -> set[str]:
    """Literals in the pushed model. A literal that exists only in the unpushed mirror is not free."""
    out: set[str] = set()
    mdef = os.path.join(project.model_dir, "definition")
    if not os.path.isdir(mdef):
        return out
    model = tmdl.load_model(mdef)
    for doc in model.docs.values():
        for reg in doc.regions:
            if reg.kind not in ("dax", "m"):
                continue
            seg = doc.text[reg.start:reg.end]
            for t in (lexers.lex_dax(seg) if reg.kind == "dax" else lexers.lex_m(seg)):
                if t.kind == "str" and not tokens.TOKEN_RE.search(t.value):
                    out.add(t.value)
                elif t.kind == "num":
                    out.add(t.text)
    return out


def _norm_expr(expr: str) -> str:
    """Budget identity. Comments and extra parentheses do not open a new allowance."""
    parts = []
    for t in lexers.significant(lexers.lex_dax(expr)):
        if t.kind == "op" and t.text in ("(", ")"):
            continue
        if t.kind == "str":
            parts.append('"' + (t.value or "").replace('"', '""') + '"')
        elif t.kind == "table":
            parts.append("'" + (t.value or "").replace("'", "''") + "'")
        elif t.kind == "bracket":
            parts.append("[" + (t.value or "").replace("]", "]]") + "]")
        else:
            parts.append(t.text)
    return " ".join(parts)


def _constants(expr: str) -> set[str]:
    out = set()
    for t in lexers.significant(lexers.lex_dax(expr)):
        if t.kind == "str" and not tokens.TOKEN_RE.fullmatch(t.value or ""):
            out.add(t.value)
        elif t.kind == "num" and t.text not in ("0", "1"):
            out.add(t.text)
    return out


def _canon_col(ref: str) -> str:
    """'Table'[Col] / Table[Col] -> Table[Col] label."""
    m = re.match(r"^\s*'((?:[^']|'')+)'\s*\[(.+)\]\s*$", ref) or re.match(r"^\s*([^\[\]']+?)\s*\[(.+)\]\s*$", ref)
    return f"{m.group(1).replace(chr(39) * 2, chr(39))}[{m.group(2)}]" if m else ref.strip()


def _rewrite(c: dict) -> tuple[dict | None, str | None]:
    kind = c.get("kind")
    if kind == "differs":
        return {**c, "kind": "equal", "_negate": True}, None
    if kind == "filter_independent":
        col = c.get("column", "")
        expr = c.get("expr", "")
        rewritten = {"id": c["id"], "kind": "equal", "left": expr,
                     "right": f"CALCULATE({expr}, REMOVEFILTERS({col}))",
                     "by": c.get("by", []), "where": c.get("where", [])}
        if "tol" in c:
            rewritten["tol"] = c["tol"]
        return rewritten, None
    return c, None


def run_checks(ws, project, spec: dict) -> dict:
    shape = validate_spec(spec)
    if not shape["valid"]:
        return {"verb": "check", "project": project.name, "spec_valid": False,
                "results": [{"id": f["id"], "status": "ERROR", "class": "BAD_REQUEST",
                             "field": f["path"], "note": f["message"]} for f in shape["findings"]]}
    pol = policy_mod.load(ws)
    synthetic = _rls_synthetic(ws, project, pol)
    perf_done = []
    for index, raw in enumerate(spec.get("checks", [])):
        if isinstance(raw, dict) and raw.get("kind") == "perf":
            cid = str(raw.get("id") or f"c{index + 1}")
            perf_done.append((index, _perf_check(ws, project, raw, cid)))
    budgets = pol.get("budgets", {})
    strict = pol.get("disclosure") == "strict"
    tm = tokens.TokenMap(ws.private)
    with _StateLock(ws):
        st = _load_state(ws)
        known = model_literals(project)
        real_model = tmdl.load_model(os.path.join(project.model_dir, "definition"))
        clf = Classifier(real_model, ws.private, pol, project.name)
        from . import deps
        mdef = os.path.join(project.mirror_model, "definition")
        mmodel = tmdl.load_model(mdef, root=os.path.dirname(project.mirror_model)) if os.path.isdir(mdef) else tmdl.Model()
        mres = deps.Resolver(mmodel)
        authored = set(st["ledger"]["authored"])
        results, to_engine, compiles_items, negate = [], [], [], set()
        rls_ids = set()
        for index, raw in enumerate(spec.get("checks", [])):
            cid = str(raw.get("id") or f"c{index + 1}")
            kind = raw.get("kind")
            if kind == "perf":
                continue
            if kind == "rls":
                if not synthetic and not _human_terminal():
                    results.append({"id": cid, "status": "ERROR", "class": "HUMAN_ONLY"})
                    continue
                query = raw.get("query") if isinstance(raw.get("query"), str) else None
                if query is not None:
                    _q, unknown = tokens.detokenize_dax(query, tm)
                    if unknown:
                        results.append({"id": cid, "status": "ERROR", "class": "BAD_REQUEST",
                                        "note": "unknown or opaque token"})
                        continue
                    query = _q
                th = _h("rls")
                if st["checks"] >= int(budgets.get("session_checks", 300)) or \
                        st["per_target"].get(th, 0) >= int(budgets.get("per_target", 60)):
                    results.append({"id": cid, "status": "BUDGET",
                                    "note": "daily check budget reached; the user can reset it"})
                    continue
                st["checks"] += 1
                st["per_target"][th] = st["per_target"].get(th, 0) + 1
                payload = {"id": cid, "kind": "rls", "expect": raw.get("expect")}
                for field in _RLS_TEXT:
                    if field == "query":
                        if query is not None:
                            payload["query"] = query
                        continue
                    if isinstance(raw.get(field), str):
                        payload[field] = raw[field]
                rls_ids.add(cid)
                to_engine.append(payload)
                continue
            if kind not in ALL_KINDS:
                results.append({"id": cid, "status": "ERROR", "class": "BAD_REQUEST", "note": "unknown kind"})
                continue
            if kind == "between":
                results.append({"id": cid, "status": "UNSUPPORTED", "note": "between is not offered"})
                continue
            c, _ = _rewrite({**raw, "id": cid})
            target = _norm_expr(str(c.get("left") or c.get("expr") or c.get("column") or c.get("from") or ""))
            th = _h(target)
            consts = set()
            for f in EXPR_FIELDS:
                if isinstance(c.get(f), str):
                    consts |= _constants(c[f])
            for w in c.get("where", []) or []:
                if isinstance(w, str):
                    consts |= _constants(w)
            consts = {x for x in consts if x not in known}
            # literals the agent pushed into measures this target reaches count as constants too: editing a
            # measure body must not turn a fixed check into a fresh guessing channel
            for f in EXPR_FIELDS:
                if isinstance(c.get(f), str):
                    consts |= {x for x in _reached_literals(mmodel, mres, c[f], next(iter(mmodel.tables), ""))
                               if _h(x) in authored}
            ledger = st["ledger"]["constants"].setdefault(th, [])
            seen_consts = {e["h"] for e in ledger}
            if len(seen_consts | {_h(x) for x in consts}) > int(budgets.get("constants_per_target", 3)):
                results.append({"id": cid, "status": "BUDGET",
                                "note": f"too many distinct constants against this target in {LEDGER_DAYS} days"})
                continue
            if st["checks"] >= int(budgets.get("session_checks", 300)) or \
                    st["per_target"].get(th, 0) >= int(budgets.get("per_target", 60)):
                results.append({"id": cid, "status": "BUDGET", "note": "daily check budget reached; the user can reset it"})
                continue
            bad = []
            out = {}
            for k, v in c.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, str) and k in EXPR_FIELDS:
                    v, unknown = tokens.detokenize_dax(v, tm)
                    bad += unknown
                elif k in ("where", "by") and isinstance(v, list):
                    vv = []
                    for item in v:
                        if isinstance(item, str):
                            item, unknown = tokens.detokenize_dax(item, tm)
                            bad += unknown
                        vv.append(item)
                    v = vv
                out[k] = v
            if bad:
                results.append({"id": cid, "status": "ERROR", "class": "BAD_REQUEST", "note": "unknown or opaque token"})
                continue
            st["checks"] += 1
            st["per_target"][th] = st["per_target"].get(th, 0) + 1
            ledger += [{"h": h, "day": st["day"]} for h in sorted({_h(x) for x in consts} - seen_consts)]
            if kind == "differs":
                negate.add(cid)
            if out["kind"] == "compiles":
                compiles_items.append({"id": cid, "table": out.get("table") or next(iter(real_model.tables), ""),
                                       "expression": out.get("expr", "")})
            else:
                to_engine.append(out)
        _save_state(ws, st)
    port = desktop.require_port(project) if (to_engine or compiles_items) else None
    reveal = []
    for c in to_engine:
        for b in c.get("by", []) or []:
            lab = _canon_col(b)
            t, _, col = lab.partition("[")
            col = col[:-1]
            if clf.column_class(t, col) in ("category", "date") or lab in pol.get("reveal_columns", []):
                reveal.append(b)
    allowed_ids = {str(c.get("id")) for c in to_engine if c.get("id")}
    allowed_ids |= {item["id"] for item in compiles_items if item.get("id")}
    if to_engine:
        _write_policy_snapshot(ws, pol)
        res = engine_call("check", {"private_dir": ws.private, "port": port,
                                    "disclosure": pol.get("disclosure", "shape"),
                                    "reveal": sorted(set(reveal)), "checks": to_engine}, timeout=600)
        if not res.get("ok"):
            raise PbzError(_class_of(res.get("class"), _PY_CLASS) or "ENGINE_UNAVAILABLE")
        with tokens.WorkspaceLock(ws.private):
            tm = tokens.TokenMap(ws.private)
            key = dpapi.load_key(ws.private)
            for r in res.get("checks", res.get("results", [])):
                r = dict(r)
                if r.get("id") in negate and r.get("status") in ("PASS", "FAIL"):
                    r["status"] = "FAIL" if r["status"] == "PASS" else "PASS"
                    r["note"] = "differs"
                groups = []
                for g in r.get("groups", []) or []:
                    ng = {}
                    for col, v in g.items():
                        if isinstance(v, dict) and v.get("blank"):
                            ng[col] = "(blank)"
                        elif isinstance(v, dict) and "reveal" in v:
                            ng[col] = v["reveal"]
                        elif isinstance(v, dict) and "hmac" in v:
                            lab = _canon_col(col)
                            ng[col] = tm.find_by_hmac(key, lab, v["hmac"]) or tm.opaque(lab, v["hmac"])
                    if ng:
                        groups.append(ng)
                if groups and not strict:
                    r["groups"] = groups
                else:
                    r.pop("groups", None)
                if strict:
                    for k in list(r):
                        if k.endswith("_bucket"):
                            r.pop(k)
                if str(r.get("id")) in rls_ids:
                    r = _rls_clean(r, rls_ids)
                else:
                    r = _closed_engine_row(r, allowed_ids)
                if r is not None:
                    results.append(r)
            tm.save()
    if compiles_items:
        res = engine_call("compiles", {"private_dir": ws.private, "port": port, "items": compiles_items}, timeout=300)
        if not res.get("ok"):
            raise PbzError(_class_of(res.get("class"), _PY_CLASS) or "ENGINE_UNAVAILABLE")
        for it in res.get("items", []):
            item = dict(it)
            item.pop("groups", None)
            row = _closed_engine_row(item, allowed_ids)
            if row is not None:
                results.append(row)
    results.extend(row for _index, row in perf_done)
    order = {str(c.get("id") or f"c{i + 1}"): i for i, c in enumerate(spec.get("checks", []))}
    results.sort(key=lambda r: order.get(str(r.get("id")), 1e9))
    return {"verb": "check", "project": project.name, "results": results,
            "budget": {"used_today": st["checks"], "limit": int(budgets.get("session_checks", 300))}}


def health(ws, project) -> dict:
    port = desktop.require_port(project)
    res = engine_call("health", {"private_dir": ws.private, "port": port}, timeout=300)
    if not res.get("ok"):
        raise PbzError(_class_of(res.get("class"), _PY_CLASS) or "ENGINE_UNAVAILABLE")
    from .validate import retokenizer
    scrub = retokenizer(tokens.TokenMap(ws.private))
    for e in res.get("errors", []):
        if "message" in e:
            e["message"] = scrub(e["message"])
    res["verb"] = "health"
    return res


def probe(ws, project, table: str, columns: list[str] | None) -> dict:
    pol = policy_mod.load(ws)
    if not spend(ws, "probes"):
        return {"verb": "probe", "status": "BUDGET", "note": "daily probe budget reached; the user can reset it"}
    port = desktop.require_port(project)
    req = {"private_dir": ws.private, "port": port, "table": table, "disclosure": pol.get("disclosure", "shape")}
    model = tmdl.load_model(os.path.join(project.mirror_model, "definition"), root=os.path.dirname(project.mirror_model))
    matched = next((t for t in model.tables.values() if t.name == table), None)
    if matched is None:
        matched = next((t for t in model.tables.values() if t.name.casefold() == table.casefold()), None)
    if not columns and matched is None:
        raise PbzError("NOT_FOUND", "table not found in mirror")
    known = [c.name for c in matched.columns.values()] if matched is not None else []
    req["columns"] = columns or known
    res = engine_call("probe", req, timeout=300)
    if not res.get("ok"):
        raise PbzError(_class_of(res.get("class"), _PY_CLASS) or "ENGINE_UNAVAILABLE")
    res["verb"] = "probe"
    return res


def refresh(ws, project, tables: list[str] | None) -> dict:
    port = desktop.require_port(project)
    req = {"private_dir": ws.private, "port": port}
    if tables:
        req["tables"] = tables
    res = engine_call("refresh", req, timeout=3600)
    out = {"verb": "refresh", "project": project.name}
    if not res.get("ok"):
        out["status"] = "ERROR"
        out["class"] = _class_of(res.get("class"), _PY_CLASS) or "OTHER"
    else:
        status = res.get("status")
        out["status"] = status if status in _REFRESH_STATUS else "ERROR"
    return out


def _write_policy_snapshot(ws, pol: dict) -> None:
    """Copy the class boundary and reveal allowlist into the private store.

    The engine reads this file and ignores any workspace path on the request, so a direct
    caller cannot point the cutoff or the reveal list at a different policy.
    """
    doc = {
        "category_max": int(pol.get("category_max", 50)),
        "reveal_columns": [c for c in pol.get("reveal_columns", []) if isinstance(c, str)],
    }
    dest = os.path.join(ws.private, "policy-snapshot.json")
    if _reparse_link(ws.private) or _reparse_link(dest):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    os.makedirs(ws.private, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="policy-snapshot.", dir=ws.private)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        if _reparse_link(ws.private) or _reparse_link(dest):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        os.replace(tmp, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def index(ws, project) -> dict:
    # Keep this lock file: unlinking lets another writer lock a different inode.
    with _StateLock(ws, "index.lock"):
        return _index(ws, project)


def _index(ws, project) -> dict:
    pol = policy_mod.load(ws)
    _write_policy_snapshot(ws, pol)
    port = desktop.require_port(project)
    res = engine_call("index", {"private_dir": ws.private, "port": port,
                                "category_max": int(pol.get("category_max", 50)),
                                "value_cap": 50000, "disclosure": pol.get("disclosure", "shape")}, timeout=1800)
    if not res.get("ok"):
        raise PbzError(_class_of(res.get("class"), _PY_CLASS) or "ENGINE_UNAVAILABLE")
    moved, pinned = harden_index(ws, pol)
    blobs = {name: pinned[name] if pinned[name] is not None else
             read_checked(os.path.join(ws.private, name), root=ws.private)
             for name in ("index.json", "categories.bin")}
    expected = {name: hashlib.sha256(raw).hexdigest() for name, raw in blobs.items()}
    res["tables"] = _rebuild_summary(ws.private, res.get("tables", []), idx=json.loads(blobs["index.json"]))
    from . import index_store
    res["project"] = project.name
    index_store.publish(ws.private, project.name, res, expected=expected, blobs=blobs)
    cols = [c for t in res.get("tables", []) for c in t.get("columns", [])]
    return {"verb": "index", "project": project.name, "tables": len(res.get("tables", [])), "columns": len(cols),
            "category_columns": sum(1 for c in cols if c.get("class") == "category"),
            "personal_or_id_columns_hashed": moved,
            "hint": "run `pbz pull --force` (or --rebase) to reclassify the mirror with the new index"}


def _rebuild_summary(private_dir, tables, idx=None):
    """Copy hardened classes into the engine summary tables."""
    if idx is None:
        idx = json.loads(read_checked(os.path.join(private_dir, "index.json"), root=private_dir))
    for table in tables:
        for col in table.get("columns", []):
            info = idx.get("columns", {}).get(f"{table['table']}[{col['column']}]", {})
            if "class" in info:
                col["class"] = info["class"]
    return tables


def harden_index(ws, pol: dict) -> tuple[int, dict[str, bytes | None]]:
    """Low-cardinality columns with personal or ID names are sensitive whatever their size: move their
    values out of categories.bin into HMAC sets (whole values + name words) so free text can be checked."""
    from .classify import is_id_column, is_pii_column
    idx_path = os.path.join(ws.private, "index.json")
    cat_path = os.path.join(ws.private, "categories.bin")
    try:
        os.lstat(idx_path)
    except FileNotFoundError:
        raise PbzError("REFUSED", "index is missing or unreadable") from None
    except OSError:
        raise PbzError("REFUSED", "index is missing or unreadable") from None
    try:
        idx_raw = read_checked(idx_path, root=ws.private)
        idx = json.loads(idx_raw.decode("utf-8"))
    except PbzError:
        raise
    except ValueError:
        raise PbzError("REFUSED", "index is missing or unreadable") from None
    cat_raw = read_checked(cat_path, root=ws.private) if os.path.lexists(cat_path) else None
    raw = dpapi.unprotect(cat_raw) if cat_raw is not None else None
    pinned = {"index.json": idx_raw, "categories.bin": cat_raw}
    cats = json.loads(raw.decode("utf-8")) if raw else {}
    key = dpapi.load_key(ws.private)
    forced = {c.lower() for c in pol.get("sensitive_columns", [])}
    moved = 0
    for lab in list(cats):
        if "[" not in lab or not lab.endswith("]"):
            continue
        col = lab[lab.index("[") + 1:-1]
        if not (is_pii_column(col) or is_id_column(col) or lab.lower() in forced):
            continue
        vals = [v for v in cats.pop(lab) if v not in (None, "")]
        info = idx.setdefault("columns", {}).setdefault(lab, {})
        info["hmacs"] = sorted(set(info.get("hmacs", [])) | {dpapi.hmac_hex(key, v) for v in vals})
        if is_pii_column(col):
            words = set(info.get("word_hmacs", []))
            for v in vals:
                for w in str(v).split():
                    core = dpapi.word_core(w)
                    if sum(ch.isalpha() for ch in core) >= 3:
                        words.add(dpapi.hmac_hex(key, core))
            info["word_hmacs"] = sorted(words)
        info["class"] = "high"
        moved += 1
    if moved:
        if _reparse_link(ws.private) or _reparse_link(idx_path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        directory = os.path.dirname(idx_path) or "."
        fd, tmp = tempfile.mkstemp(prefix="index.", suffix=".tmp", dir=directory)
        try:
            data = json.dumps(idx).encode("utf-8")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            if _reparse_link(idx_path):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            os.replace(tmp, idx_path)
            tmp = ""
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        pinned["index.json"] = data
        pinned["categories.bin"] = dpapi.write_protected(
            cat_path, json.dumps(cats, ensure_ascii=False).encode("utf-8"))
    return moved, pinned


def _validate_snapshot_name(name: str) -> None:
    """Validate snapshot name is a single path segment. Refuse .., separators, whitespace, or absolute paths."""
    if not isinstance(name, str) or not name or name != name.strip() or name in (".", ".."):
        raise PbzError("BAD_REQUEST", "snapshot name must be a single path segment")
    if ".." in name or any(ch in name for ch in "/\\:\x00") or os.path.isabs(name) or os.path.basename(name) != name:
        raise PbzError("BAD_REQUEST", "snapshot name must be a single path segment")


def _snapshot_spec_path(ws, name: str, project_id=None) -> str:
    """One path segment under the private snapshots/specs directory. No .., separator, or absolute name."""
    if project_id is not None and not identity.is_uuid(project_id):
        raise PbzError("BAD_REQUEST", "project identity is invalid")
    _validate_snapshot_name(name)
    if project_id is None:
        root = os.path.abspath(os.path.join(ws.private, "snapshots", "specs"))
    else:
        root = os.path.abspath(os.path.join(ws.private, "snapshots", "projects", project_id, "specs"))
    path = os.path.abspath(os.path.join(root, name + ".json"))
    if os.path.dirname(path) != root:
        raise PbzError("BAD_REQUEST", "snapshot name must be a single path segment")
    return path


def _legacy_snapshot_spec_path(ws, name: str) -> str:
    """Legacy path segment under the private snapshots directory (read-only fallback)."""
    _validate_snapshot_name(name)
    root = os.path.abspath(os.path.join(ws.private, "snapshots"))
    path = os.path.abspath(os.path.join(root, name + ".spec.json"))
    if os.path.dirname(path) != root:
        raise PbzError("BAD_REQUEST", "snapshot name must be a single path segment")
    return path


def _plan_snapshot_layout(ws) -> list:
    """Read-only planning half of the layout migration.

    Returns plan entries (old_path, target, kind, base, content,
    already_exists). Raises the same refusals the combined function raised
    in its first pass. Takes no lock and writes nothing.
    """
    if _reparse_link(ws.private):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    snaps = os.path.abspath(os.path.join(ws.private, "snapshots"))
    if os.path.lexists(snaps):
        if _reparse_link(snaps) or not os.path.isdir(snaps):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")

    results_dir = os.path.join(snaps, "results")
    specs_dir = os.path.join(snaps, "specs")

    # Refuse pre-existing reparse points or non-directories before planning
    for d in (results_dir, specs_dir):
        if os.path.lexists(d) and (_reparse_link(d) or not os.path.isdir(d)):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")

    if not os.path.lexists(snaps):
        return []

    plan = []
    planned_targets: dict[str, bytes] = {}
    for name in sorted(list_names(snaps)):
        if name in ("results", "specs"):
            continue
        old_path = os.path.join(snaps, name)
        if _reparse_link(old_path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        if os.path.isdir(old_path):
            continue
        if not name.endswith(".json"):
            continue

        content = read_checked(old_path, root=ws.private)
        try:
            doc = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PbzError("CORRUPT_STATE", f"legacy snapshot file {name} has unparseable JSON")
        if not isinstance(doc, dict):
            raise PbzError("CORRUPT_STATE", f"legacy snapshot file {name} is not a JSON object")

        # Content shape inspection:
        # Query specs have "queries" as a list and must carry the .spec.json suffix.
        # Results have "queries" as a dict and "refreshed" as str or null.
        if is_snapshot_spec_doc(doc):
            if not name.endswith(".spec.json"):
                raise PbzError("CORRUPT_STATE", f"spec-shaped legacy file {name} lacks .spec.json suffix")
            base = name[:-10]
            kind = "spec"
            target = os.path.join(specs_dir, base + ".json")
        elif is_snapshot_result_doc(doc):
            kind = "result"
            base = name[:-5]
            target = os.path.join(results_dir, base + ".json")
        else:
            raise PbzError("CORRUPT_STATE", f"legacy snapshot file {name} has unrecognized shape")

        try:
            _validate_snapshot_name(base)
        except PbzError:
            raise PbzError("CORRUPT_STATE", f"legacy snapshot file {name} resolves to invalid name {base}")

        if _reparse_link(target):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")

        # Intra-plan and on-disk collision detection
        if target in planned_targets:
            if planned_targets[target] != content:
                raise PbzError("CONFLICT", f"snapshot migration conflict for target {os.path.basename(target)}")
            plan.append((old_path, target, kind, base, content, True))
        else:
            planned_targets[target] = content
            if os.path.lexists(target):
                existing = read_checked(target, root=ws.private)
                if existing != content:
                    raise PbzError("CONFLICT", f"snapshot migration conflict for {name}")
                plan.append((old_path, target, kind, base, content, True))
            else:
                plan.append((old_path, target, kind, base, content, False))
    return plan


def _execute_snapshot_layout(ws, plan, *, revalidate: bool, progress: list | None = None) -> dict:
    """Executing half of the layout migration.

    With revalidate=False this is the combined function's original second
    pass exactly. With revalidate=True (the adoption executor), each entry
    is re-read before it executes and refuses on any byte change, the
    destination is checked before publication, the published copy is
    re-read and hash-verified before the source is unlinked, and completed
    entries are appended to progress before any refusal propagates.
    """
    snaps = os.path.abspath(os.path.join(ws.private, "snapshots"))
    results_dir = os.path.join(snaps, "results")
    specs_dir = os.path.join(snaps, "specs")
    if not plan:
        return {"migrated": False, "results": [], "specs": []}

    os.makedirs(results_dir, exist_ok=True)
    os.makedirs(specs_dir, exist_ok=True)
    if _reparse_link(results_dir) or _reparse_link(specs_dir):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")

    migrated_results = []
    migrated_specs = []
    for old_path, target, kind, base, content, already_exists in plan:
        if revalidate:
            current = read_checked(old_path, root=ws.private)
            if current != content:
                raise PbzError("CONFLICT", f"legacy snapshot file {os.path.basename(old_path)} changed after the plan was approved")
            dest_raw = _read_bytes_or_none(target, ws.private)
            if dest_raw is None:
                _durable_replace_bytes(target, content, root=ws.private)
                written = read_checked(target, root=ws.private)
                if hashlib.sha256(written).digest() != hashlib.sha256(content).digest():
                    raise PbzError("CORRUPT_STATE", f"legacy snapshot file {os.path.basename(old_path)} did not verify after publication")
            elif dest_raw != content:
                raise PbzError("CONFLICT", f"snapshot migration conflict for {os.path.basename(old_path)}")
            remove_file(old_path, root=ws.private)
        else:
            if not already_exists:
                replace_bytes(target, content, root=ws.private)
            remove_file(old_path, root=ws.private)
        if progress is not None:
            progress.append((old_path, target, kind, base))
        if kind == "result":
            migrated_results.append(base)
        else:
            migrated_specs.append(base)

    return {
        "migrated": bool(migrated_results or migrated_specs),
        "results": sorted(migrated_results),
        "specs": sorted(migrated_specs),
    }


def _durable_replace_bytes(path, content: bytes, *, root) -> None:
    """Atomic replace with the section 1 durability discipline: file fsync
    on every platform, containing-directory fsync on POSIX. The resolved
    containing directory must stay inside root, so a linked directory
    cannot redirect the write outside the store."""
    directory = os.path.dirname(path)
    resolved_root = os.path.realpath(root)
    resolved_dir = os.path.realpath(directory)
    if resolved_dir != resolved_root and not resolved_dir.startswith(resolved_root + os.sep):
        raise PbzError("UNSAFE_PATH", f"refusing to write outside the private store: {path}")
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".pbz-durable-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    _snapshot_sync_dir(directory)


def migrate_snapshot_layout(ws) -> dict:
    """Migrate legacy flat snapshots/ files into snapshots/results/ and snapshots/specs/.

    Protected by WorkspaceLock. Safe, two-pass, idempotent, and fail-closed.
    """
    with tokens.WorkspaceLock(ws.private):
        plan = _plan_snapshot_layout(ws)
        return _execute_snapshot_layout(ws, plan, revalidate=False)


_SNAPSHOT_ID = re.compile(r"\A[A-Za-z0-9_.:/\- ]{1,128}\Z")


def _snapshot_query_list(spec: dict | None) -> list:
    """Reject a bad query list before any port, budget, or engine call.

    Shape only. DAX syntax stays in the engine. A single string `keys` value is
    the one-item list the engine already accepts. Missing `keys` means none.
    """
    raw = spec.get("queries", []) if isinstance(spec, dict) else []
    if not isinstance(raw, list) or not raw or any(not isinstance(q, dict) for q in raw):
        raise PbzError("BAD_REQUEST", "queries must not be empty")
    seen: set[str] = set()
    for query in raw:
        qid = query.get("id")
        if not isinstance(qid, str) or _SNAPSHOT_ID.fullmatch(qid) is None:
            raise PbzError("BAD_REQUEST", "query id is invalid")
        if qid in seen:
            raise PbzError("BAD_REQUEST", "duplicate query id")
        seen.add(qid)
        dax = query.get("dax")
        if not isinstance(dax, str) or not dax.strip():
            raise PbzError("BAD_REQUEST", "dax is required")
        if "keys" in query and query.get("keys") is not None:
            keys = query.get("keys")
            if isinstance(keys, str):
                continue
            if not isinstance(keys, list) or any(not isinstance(item, str) for item in keys):
                raise PbzError("BAD_REQUEST", "keys must be a list of strings")
    return raw


def _preflight_snapshot_save(ws, name: str) -> None:
    """Refuse a file or link where the named save needs a real directory. Create nothing."""
    spec_path = _snapshot_spec_path(ws, name)
    result_path = _snapshot_result_path(ws, name, namespaced=True)
    snaps = os.path.join(ws.private, "snapshots")
    pair = _SnapshotPair(ws, name)
    directories = (ws.private, snaps, os.path.dirname(spec_path), os.path.dirname(result_path), pair.pending)
    named = (spec_path, result_path, pair.lock)
    for path in (*directories, *named):
        if _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    for path in directories:
        if os.path.lexists(path) and not os.path.isdir(path):
            raise PbzError("CORRUPT_STATE", "snapshot directory is not a directory")
    for path in named:
        if os.path.lexists(path) and not os.path.isfile(path):
            raise PbzError("CORRUPT_STATE", "snapshot path is not a file")


def _preflight_snapshot_workspace(ws):
    root = os.path.join(ws.private, "snapshots")
    projects = os.path.join(root, "projects")
    for path in (ws.private, root, projects):
        if _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    for path in (root, projects):
        if os.path.lexists(path) and not os.path.isdir(path):
            raise PbzError("CORRUPT_STATE", f"snapshot directory is not a directory: {path}")
    if os.path.lexists(ws.private) and not os.path.isdir(ws.private):
        raise PbzError("CORRUPT_STATE", "snapshot directory is not a directory")


def _preflight_snapshot_project(ws, name, project_id):
    pair = _SnapshotPair(ws, name, project_id)
    snaps_projects = os.path.join(ws.private, "snapshots", "projects")
    directories = (snaps_projects, pair.root, os.path.dirname(pair.spec), os.path.dirname(pair.result), pair.pending)
    named = (pair.spec, pair.result, pair.lock, _project_guard_path(ws, project_id))
    for path in (*directories, *named):
        if _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    for path in directories:
        if os.path.lexists(path) and not os.path.isdir(path):
            raise PbzError("CORRUPT_STATE", "snapshot directory is not a directory")
    for path in named:
        if os.path.lexists(path) and not os.path.isfile(path):
            raise PbzError("CORRUPT_STATE", "snapshot path is not a file")


def _project_guard_path(ws, project_id):
    return os.path.join(ws.private, "snapshots", "projects", project_id + ".guard")


def _ensure_project_namespace(ws, project_id):
    try:
        _ensure_project_namespace_inner(ws, project_id)
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"project namespace is unavailable: {error}") from error


def _ensure_project_namespace_inner(ws, project_id):
    pair_root = os.path.join(ws.private, "snapshots", "projects", project_id)
    for directory in (os.path.join(ws.private, "snapshots"),
                      os.path.join(ws.private, "snapshots", "projects"), pair_root,
                      os.path.join(pair_root, "results"), os.path.join(pair_root, "specs"),
                      os.path.join(pair_root, "pending")):
        if _reparse_link(directory):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        _snapshot_mkdir(directory)
    guard = _project_guard_path(ws, project_id)
    if _reparse_link(guard):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    if not os.path.lexists(guard):
        fd = os.open(guard, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        _snapshot_sync_dir(os.path.dirname(guard))


def _acquire_project_guard(ws, project_id, mode="shared"):
    try:
        return _snapshot_open_lock(_project_guard_path(ws, project_id), False, mode=mode)
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"project guard is unavailable: {error}") from error


def _snapshot_result_path(ws, name: str, *, namespaced: bool, project_id=None) -> str:
    """Result path for one snapshot name. Does not create the directory."""
    if project_id is not None and not identity.is_uuid(project_id):
        raise PbzError("BAD_REQUEST", "project identity is invalid")
    _validate_snapshot_name(name)
    if project_id is not None:
        base = os.path.join(ws.private, "snapshots", "projects", project_id, "results")
    else:
        base = os.path.join(ws.private, "snapshots", "results") if namespaced else os.path.join(ws.private, "snapshots")
    root = os.path.abspath(base)
    path = os.path.abspath(os.path.join(root, name + ".json"))
    if os.path.dirname(path) != root:
        raise PbzError("BAD_REQUEST", "snapshot name must be a single path segment")
    return path


def _load_snapshot_json(ws, path: str):
    """Read one snapshot file. A bad file is corrupt. Nothing is created or removed."""
    try:
        raw = read_checked(path, root=ws.private)
        return json.loads(raw.decode("utf-8-sig"))
    except PbzError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PbzError("CORRUPT_STATE", "snapshot baseline is unreadable") from None


def _doc_project_stamp(doc):
    return doc.get("project_id") if isinstance(doc, dict) else None


def _require_project_stamp(doc, project_id, what):
    if _doc_project_stamp(doc) != project_id:
        raise PbzError("CORRUPT_STATE", f"snapshot {what} belongs to a different project")


def _require_usable_baseline(ws, name: str, project_id=None) -> None:
    """Fail closed when this name's baseline cannot be read. Other files are ignored.

    A namespaced result wins and is never replaced by a legacy file. A missing
    baseline is left for the engine. A spec-shaped legacy file is not a result
    and is left in place for the existing engine behavior.
    """
    if project_id is not None:
        path = _snapshot_result_path(ws, name, namespaced=True, project_id=project_id)
        if _inspected_present(path):
            doc = _load_snapshot_json(ws, path)
            if not is_snapshot_result_doc(doc):
                raise PbzError("CORRUPT_STATE", "snapshot baseline is not a result document")
            _require_project_stamp(doc, project_id, "baseline")
        return
    namespaced = _snapshot_result_path(ws, name, namespaced=True)
    if os.path.lexists(namespaced):
        if not is_snapshot_result_doc(_load_snapshot_json(ws, namespaced)):
            raise PbzError("CORRUPT_STATE", "snapshot baseline is not a result document")
        return
    legacy = _snapshot_result_path(ws, name, namespaced=False)
    if not os.path.lexists(legacy):
        return
    doc = _load_snapshot_json(ws, legacy)
    if is_snapshot_result_doc(doc) or is_snapshot_spec_doc(doc):
        return
    raise PbzError("CORRUPT_STATE", "snapshot baseline is not a result document")


def _snapshot_recovery_error(note="snapshot publication needs recovery"):
    return PbzError("RECOVERY_REQUIRED", note)


def _snapshot_sync_dir(directory):
    if os.name != "nt":
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as error:
            raise _snapshot_recovery_error() from error


def _snapshot_mkdir(directory):
    try:
        os.mkdir(directory)
    except FileExistsError:
        if not os.path.isdir(directory):
            raise
    else:
        _snapshot_sync_dir(os.path.dirname(directory))


def _snapshot_write(path, data, prefix, *, verify=None):
    directory = os.path.dirname(path)
    fd, temp_path = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError as error:
                raise _snapshot_recovery_error() from error
        if _reparse_link(directory) or _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        if verify is not None:
            verify()
        os.replace(temp_path, path)
        temp_path = None
        _snapshot_sync_dir(directory)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def _snapshot_hash(path, *, sync=False):
    with open(path, "r+b" if sync and os.name == "nt" else "rb") as handle:
        data = handle.read()
        if sync:
            os.fsync(handle.fileno())
    return hashlib.sha256(data).hexdigest()


def _snapshot_optional_hash(path):
    try:
        return _snapshot_hash(path)
    except FileNotFoundError:
        return None


def _snapshot_open_lock(path, readonly=False, *, mode=None):
    if mode in ("shared", "try_shared", "try_exclusive"):
        exclusive = mode == "try_exclusive"
        if os.name != "nt":
            import fcntl

            if exclusive:
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            else:
                # Shared acquisition only reads: the guard file is created by
                # the identity step before any shared use.
                fd = os.open(path, os.O_RDONLY)
            flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if mode != "shared":
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except BaseException as error:
                os.close(fd)
                if mode != "shared" and isinstance(error, BlockingIOError):
                    return None
                raise
            return fd

        import ctypes
        from ctypes import wintypes as w

        create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                           w.DWORD, w.DWORD, w.HANDLE]
        create.restype = w.HANDLE
        invalid = ctypes.c_void_p(-1).value
        while True:
            handle = create(path, 0xC0000000 if exclusive else 0x80000000,
                            0 if exclusive else 1, None, 4, 0x80, None)
            if handle != invalid:
                return handle
            error = ctypes.get_last_error()
            if error != 32:  # ERROR_SHARING_VIOLATION
                raise ctypes.WinError(error)
            if mode != "shared":
                return None
            time.sleep(0.02)

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w

        create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                           w.DWORD, w.DWORD, w.HANDLE]
        create.restype = w.HANDLE
        invalid = ctypes.c_void_p(-1).value
        while True:
            handle = create(path, 0x80000000 if readonly else 0xC0000000,
                            0, None, 3 if readonly else 4, 0x80, None)
            if handle != invalid:
                return handle
            error = ctypes.get_last_error()
            if error != 32:  # ERROR_SHARING_VIOLATION
                raise ctypes.WinError(error)
            time.sleep(0.02)

    fd = os.open(path, os.O_RDONLY if readonly else os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _lock_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _snapshot_close_handle(handle):
    if os.name == "nt":
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(handle))
    else:
        os.close(handle)


_SNAPSHOT_JOURNAL_FIELDS = (
    "version", "state", "before_result", "before_spec", "before_coherent",
    "staged_spec", "new_result",
)
_SNAPSHOT_JOURNAL_ANNOTATIONS = ("superseding", "result_foreign")


class _SnapshotPair:
    def __init__(self, ws, name, project_id=None):
        self.name = name
        self.project_id = project_id
        self.result = _snapshot_result_path(ws, name, namespaced=True, project_id=project_id)
        self.spec = _snapshot_spec_path(ws, name, project_id)
        if project_id is None:
            self.root = os.path.dirname(os.path.dirname(self.result))
            self.journal_fields = _SNAPSHOT_JOURNAL_FIELDS
        else:
            self.root = os.path.join(ws.private, "snapshots", "projects", project_id)
            self.journal_fields = _SNAPSHOT_JOURNAL_FIELDS + ("project_id", "name")
        self.pending = os.path.join(self.root, "pending")
        self.lock = os.path.join(self.pending, name + ".lock")
        self.journal = os.path.join(self.pending, name + ".journal.json")
        self.staged = os.path.join(self.pending, name + ".spec.next")

    def validate_transient(self):
        for path in (self.journal, self.staged):
            if _reparse_link(path):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        for path in (self.journal, self.staged):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise PbzError("CORRUPT_STATE", "snapshot path is not a file") from error
            if not stat.S_ISREG(info.st_mode):
                raise PbzError("CORRUPT_STATE", "snapshot path is not a file")

    @contextmanager
    def locked(self, action):
        try:
            private = os.path.dirname(self.root)
            if not os.path.isdir(private):
                os.makedirs(private, exist_ok=True)
            _snapshot_mkdir(self.root)
            _snapshot_mkdir(self.pending)
            handle = _snapshot_open_lock(self.lock, False)
        except OSError as error:
            if action == "save":
                raise PbzError("LOCAL_STATE_UNAVAILABLE") from error
            try:
                handle = _snapshot_open_lock(self.lock, True)
            except OSError as fallback:
                if os.path.lexists(self.journal):
                    raise _snapshot_recovery_error() from fallback
                raise PbzError("LOCAL_STATE_UNAVAILABLE") from fallback
        try:
            yield handle
        finally:
            # The child may still hold the same POSIX open file description.
            _snapshot_close_handle(handle)

    def hashes(self):
        try:
            return _snapshot_optional_hash(self.result), _snapshot_optional_hash(self.spec)
        except OSError as error:
            raise _snapshot_recovery_error() from error

    def sync_file_entry(self, path, expected):
        if expected is not None:
            _snapshot_hash(path, sync=True)
        directory = os.path.dirname(path)
        if os.name != "nt":
            if expected is None:
                try:
                    os.stat(directory)
                except FileNotFoundError:
                    return
            _snapshot_sync_dir(directory)

    def remove(self, expected):
        try:
            self.sync_file_entry(self.result, expected[0])
            self.sync_file_entry(self.spec, expected[1])
            _snapshot_sync_dir(self.root)
            if self.hashes() != expected:
                raise _snapshot_recovery_error()
            os.unlink(self.journal)
            try:
                os.unlink(self.staged)
            except OSError:
                pass  # An orphan without a journal does not govern the pair.
            _snapshot_sync_dir(self.pending)
        except OSError as error:
            raise _snapshot_recovery_error() from error

    def write_journal(self, journal):
        if self.project_id is not None:
            journal = {**journal, "version": 2, "project_id": self.project_id, "name": self.name}
        ordered = {key: journal[key] for key in self.journal_fields}
        ordered.update((key, journal[key]) for key in _SNAPSHOT_JOURNAL_ANNOTATIONS if key in journal)
        _snapshot_write(self.journal, json.dumps(ordered).encode(), self.name + ".journal.")

    def stage(self, data):
        _snapshot_write(self.staged, data, self.name + ".spec.")

    def read_journal(self):
        try:
            with open(self.journal, "rb") as handle:
                journal = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError) as error:
            raise PbzError("CORRUPT_STATE", "unreadable snapshot journal") from error
        if not _valid_snapshot_journal(journal, project_id=self.project_id, name=self.name):
            raise PbzError("CORRUPT_STATE", "invalid snapshot journal")
        return journal

    def staged_bytes(self, journal):
        try:
            data = Path(self.staged).read_bytes()
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() != journal["staged_spec"]:
            return None
        if self.project_id is not None:
            self._require_spec_stamp(data, "staged snapshot spec")
        return data

    def _require_spec_stamp(self, data, what):
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except (ValueError, RecursionError):
            doc = None
        if _doc_project_stamp(doc) != self.project_id:
            raise PbzError("CORRUPT_STATE", f"{what} belongs to a different project")

    def complete(self, journal, data):
        if data is None:
            raise _snapshot_recovery_error()
        if self.project_id is not None:
            self._require_spec_stamp(data, "snapshot spec")
        new_pair = journal["new_result"], journal["staged_spec"]
        try:
            _snapshot_mkdir(os.path.dirname(self.spec))
            expected = new_pair[0], journal["before_spec"]
            _snapshot_write(self.spec, data, "snapshot-spec.", verify=lambda: self.require_pair(expected))
            if self.hashes() != new_pair:
                raise _snapshot_recovery_error()
            self.remove(new_pair)
        except (OSError, PbzError) as error:
            if isinstance(error, PbzError) and error.code == "RECOVERY_REQUIRED":
                raise
            raise _snapshot_recovery_error() from error

    def require_pair(self, expected):
        if self.hashes() != expected:
            raise _snapshot_recovery_error()

    def revalidate_save(self, journal, before):
        live = self.hashes()
        if live != before:
            if live[0] != before[0] and not journal.get("result_foreign"):
                try:
                    self.write_journal({**journal, "result_foreign": True})
                except (OSError, PbzError) as error:
                    raise _snapshot_recovery_error() from error
            raise _snapshot_recovery_error()

    def recover(self, action):
        journal = self.read_journal()
        if journal is None:
            return None, None
        live = self.hashes()
        before = journal["before_result"], journal["before_spec"]
        new = journal["new_result"], journal["staged_spec"]
        published = journal["state"] == "RESULT_PUBLISHED"
        known_results = (before[0], new[0]) if published else (before[0],)
        if live[0] in known_results and (journal.get("superseding") or journal.get("result_foreign")):
            try:
                self.sync_file_entry(self.result, live[0])
                _snapshot_sync_dir(self.root)
                if _snapshot_optional_hash(self.result) != live[0]:
                    raise _snapshot_recovery_error()
                journal = {key: value for key, value in journal.items()
                           if key not in _SNAPSHOT_JOURNAL_ANNOTATIONS}
                self.write_journal(journal)
            except OSError as error:
                raise _snapshot_recovery_error() from error
            live = self.hashes()

        if published and live == new:
            self.remove(live)
            return None, None
        if published and live == (new[0], before[1]):
            data = self.staged_bytes(journal)
            if data is not None:
                self.complete(journal, data)
                return None, None
            # The higher-priority completion rule has a missing or invalid stage.
            # A save may still supersede; compare cannot use this generation.
        elif live == before and journal["before_coherent"]:
            self.remove(live)
            return None, None

        if (action == "save" and not journal.get("result_foreign")
                and live[1] in (before[1], new[1])
                and (not published or live[0] in known_results or journal.get("superseding"))):
            return journal, live
        raise _snapshot_recovery_error()

    def finish_refusal(self, journal, before, original, *, superseding, never_wrote,
                       self_flag=False, keep_journal=False):
        """Resolve a non-completing exit with a confirmed child exit or no dispatch."""
        cause = original if isinstance(original, Exception) else None
        note = original.code if isinstance(original, PbzError) else (
            "snapshot save failed" if cause is not None else original)
        try:
            live = self.hashes()
            if self_flag and (never_wrote or live == before):
                cleared = {key: value for key, value in journal.items() if key != "superseding"}
                try:
                    self.write_journal(cleared)
                except (OSError, PbzError):
                    if never_wrote and live[0] != before[0]:
                        # Read back which version survived if the clear failed after replace.
                        current = self.read_journal()
                        self.write_journal({**current, "result_foreign": True})
                    raise
            elif never_wrote and live[0] != before[0]:
                if not journal.get("result_foreign"):
                    self.write_journal({**journal, "result_foreign": True})
            if not superseding and not keep_journal and live == before:
                self.remove(before)
        except (OSError, PbzError) as error:
            raise _snapshot_recovery_error(note) from (cause if cause is not None else error)
        if (never_wrote and live != before) or live[1] != before[1] or (not superseding and live != before):
            raise _snapshot_recovery_error(note) from cause


def _valid_snapshot_journal(journal, project_id=None, name=None):
    fields = set(_SNAPSHOT_JOURNAL_FIELDS)
    version = 1
    if project_id is not None:
        fields.update(("project_id", "name"))
        version = 2
    annotations = set(_SNAPSHOT_JOURNAL_ANNOTATIONS)
    if (not isinstance(journal, dict) or not fields <= journal.keys()
            or journal.keys() - fields - annotations
            or type(journal["version"]) is not int or journal["version"] != version
            or (project_id is not None and (journal["project_id"] != project_id or journal["name"] != name))
            or type(journal["before_coherent"]) is not bool
            or any(journal[key] is not True for key in annotations & journal.keys())):
        return False

    def valid_hash(value):
        return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None

    return (valid_hash(journal["staged_spec"])
            and all(journal[key] is None or valid_hash(journal[key])
                    for key in ("before_result", "before_spec"))
            and ((journal["state"] == "PREPARED" and journal["new_result"] is None
                  and journal["before_coherent"] and "superseding" not in journal)
                 or (journal["state"] == "RESULT_PUBLISHED" and valid_hash(journal["new_result"]))))


def _snapshot_request(ws, project, name, raw_queries, project_id):
    tm = tokens.TokenMap(ws.private)
    queries = []
    for q in raw_queries:
        dax, unknown = tokens.detokenize_dax(q["dax"], tm)
        if unknown:
            raise PbzError("BAD_REQUEST", "unknown token in query " + q["id"])
        queries.append({"id": q["id"], "dax": dax, "keys": q.get("keys", [])})
    return {"private_dir": ws.private, "port": desktop.require_port(project),
            "name": name, "queries": queries, "project_id": project_id}


def _snapshot_budget(verb):
    return {"verb": verb, "status": "BUDGET",
            "note": "daily snapshot query budget reached; the user can reset it"}


def _snapshot_engine_error(res):
    return PbzError(_class_of(res.get("class"), _PY_CLASS) or "ENGINE_UNAVAILABLE")


class _StampMismatch(Exception):
    """Carries a CORRUPT_STATE stamp failure out of the save success block
    without widening its recovery conversion to unrelated failures."""

    def __init__(self, error):
        super().__init__(str(error))
        self.error = error


def _save_snapshot(ws, project, pair, raw_queries, spec, handle, job, journal, before, *, guard_handle=None):
    verb = "snapshot-save"
    superseding = journal is not None
    try:
        request = _snapshot_request(ws, project, pair.name, raw_queries, pair.project_id)
        data = json.dumps(spec).encode()
    except Exception as error:
        if superseding:
            pair.finish_refusal(journal, before, error, superseding=True, never_wrote=True)
        raise

    if not superseding:
        before = pair.hashes()
    candidate = {
        "version": 2 if pair.project_id else 1, "state": "PREPARED", "before_result": before[0],
        "before_spec": before[1], "before_coherent": not superseding,
        "staged_spec": hashlib.sha256(data).hexdigest(), "new_result": None,
    }
    if not superseding:
        try:
            pair.stage(data)
            pair.write_journal(candidate)
        except OSError as error:
            raise PbzError("LOCAL_STATE_UNAVAILABLE") from error
        journal = candidate

    pair.revalidate_save(journal, before)
    try:
        charged = spend(ws, "snapshot_queries", len(raw_queries))
    except Exception as error:
        pair.finish_refusal(journal, before, error, superseding=superseding, never_wrote=True)
        raise
    if not charged:
        pair.finish_refusal(journal, before, "BUDGET", superseding=superseding, never_wrote=True)
        return _snapshot_budget(verb)

    try:
        for directory in (os.path.dirname(pair.root), pair.root, pair.pending):
            _snapshot_sync_dir(directory)
    except PbzError as error:
        pair.finish_refusal(journal, before, error, superseding=superseding,
                            never_wrote=True, keep_journal=True)
        raise
    pair.revalidate_save(journal, before)

    self_flag = superseding and journal["state"] == "RESULT_PUBLISHED" and not journal.get("superseding")
    if self_flag:
        journal = {**journal, "superseding": True}
        try:
            pair.write_journal(journal)
        except (OSError, PbzError) as error:
            raise _snapshot_recovery_error() from error

    handles = (handle,) if guard_handle is None else (handle, guard_handle)
    inheritance = ({"inherit_handles": handles, "kill_on_close_job": job}
                   if os.name == "nt" else {"pass_fds": handles})
    res = engine_call(verb, request, timeout=1800, **inheritance)
    state = res.get("payload_state")
    if res.get("ok") and pair.project_id and res.get("project_id") != pair.project_id:
        error = PbzError("ENGINE_UNAVAILABLE", "engine response project_id does not match the selected project")
        if state not in ("not_sent", "may_have_been_sent"):
            raise _snapshot_recovery_error(str(error)) from error
        pair.finish_refusal(journal, before, error, superseding=superseding,
                            self_flag=self_flag, never_wrote=state == "not_sent")
        raise error
    succeeded = res.get("ok") and any(isinstance(q, dict) and q.get("result") == "SAVED"
                                      for q in res.get("queries") or [])
    if not succeeded:
        error = None if res.get("ok") else _snapshot_engine_error(res)
        original = error if error is not None else "all queries failed"
        if state not in ("not_sent", "may_have_been_sent"):
            raise _snapshot_recovery_error(str(original)) from error
        pair.finish_refusal(journal, before, original, superseding=superseding,
                            self_flag=self_flag, never_wrote=state == "not_sent" or error is None)
        if error is not None:
            raise error
    else:
        try:
            if pair.project_id:
                raw = read_checked(pair.result, root=ws.private)
                try:
                    doc = json.loads(raw.decode("utf-8-sig"))
                except (UnicodeDecodeError, ValueError, RecursionError) as error:
                    raise PbzError("CORRUPT_STATE", "snapshot baseline is unreadable") from error
                try:
                    if not is_snapshot_result_doc(doc):
                        raise PbzError("CORRUPT_STATE", "snapshot baseline is not a result document")
                    _require_project_stamp(doc, pair.project_id, "baseline")
                except PbzError as stamp_error:
                    raise _StampMismatch(stamp_error) from stamp_error
                new_result = _snapshot_hash(pair.result, sync=True)
                if new_result != hashlib.sha256(raw).hexdigest():
                    raise PbzError("RECOVERY_REQUIRED", "snapshot baseline changed while its stamp was being verified")
            else:
                new_result = _snapshot_hash(pair.result, sync=True)
            _snapshot_sync_dir(os.path.dirname(pair.result))
            _snapshot_sync_dir(pair.root)
            if superseding:
                pair.stage(data)
        except _StampMismatch as marked:
            # The just-published result is not this project's document. The
            # journal is not advanced; recovery arbitrates the pair.
            raise marked.error
        except (OSError, PbzError) as error:
            refusal = _snapshot_recovery_error()
            if superseding and self_flag:
                pair.finish_refusal(journal, before, error, superseding=True,
                                    self_flag=True, never_wrote=False)
            raise refusal from error
        published = {**candidate, "state": "RESULT_PUBLISHED", "new_result": new_result}
        try:
            pair.write_journal(published)
        except (OSError, PbzError) as error:
            raise _snapshot_recovery_error() from error
        pair.complete(published, pair.staged_bytes(published))
    return {**{key: value for key, value in res.items() if key != "payload_state"}, "verb": verb}


def _resolve_selected_project(ws, project):
    if project is None:
        return ws.project(None)
    if os.path.normcase(os.path.abspath(project.root)) != os.path.normcase(os.path.abspath(ws.root)):
        raise PbzError("BAD_REQUEST", "project does not belong to this workspace")
    return ws.project(project.name)


def _snapshot_with_notes(res, notes):
    if notes:
        extra = "; ".join(notes)
        res = {**res, "note": (res["note"] + "; " + extra) if res.get("note") else extra}
    return res


def snapshot(ws, project, action: str, name: str, spec: dict | None) -> dict:
    # Step 1: selection.
    if action not in ("save", "compare"):
        raise PbzError("BAD_REQUEST", f"unknown snapshot action {action}")
    _validate_snapshot_name(name)
    project = _resolve_selected_project(ws, project)
    # Step 2: name and query-list validation.
    raw_queries = None if action == "compare" and spec is None else _snapshot_query_list(spec)
    # Step 3: every read-only refusal, before any engine contact.
    _preflight_snapshot_workspace(ws)
    planned = identity.resolve(ws, project, apply=False)
    project_id = planned.project_id
    pair = _SnapshotPair(ws, name, project_id) if project_id else None
    if pair is not None:
        _preflight_snapshot_project(ws, name, project_id)
        _check_pair_readonly(ws, pair)
    ns_spec_missing = pair is None or not os.path.lexists(pair.spec)
    flat = _classify_flat_pair(ws, name)
    journal_present = pair is not None and os.path.lexists(pair.journal)
    if action == "compare" and spec is None and ns_spec_missing and not journal_present:
        if flat["result"] or flat["spec"]:
            raise PbzError("LEGACY_BASELINE_UNADOPTED", "a legacy snapshot pair with this name has not been adopted; run pbz snapshot adopt-legacy --project " + project.name)
        raise PbzError("BAD_REQUEST", "--spec required")
    if (action == "compare" and not journal_present and flat["result"]
            and not (pair is not None and os.path.lexists(pair.result))):
        # A settled pair with no namespaced result while a flat result exists
        # is legacy-only; refuse before the ping, the identity writes and the
        # pair lock. The step 6 recheck covers races; a journaled pair is
        # arbitrated by recovery instead.
        raise PbzError("LEGACY_BASELINE_UNADOPTED", "a legacy snapshot baseline with this name has not been adopted; run pbz snapshot adopt-legacy --project " + project.name)
    # Step 4: engine capability gate. The ping carries no private_dir.
    ping = engine_call("ping", {})
    if not ping.get("ok"):
        raise _snapshot_engine_error(ping)
    if ping.get("snapshot_layout") != "project-v1":
        raise PbzError("ENGINE_VERSION", "the pbz engine does not support project-scoped snapshots; upgrade the engine")
    # Step 5: identity writes under the workspace identity lock.
    notes = []
    with identity.identity_lock(ws):
        current = identity.resolve(ws, project, apply=False)
        if planned.project_id is not None and current.project_id != planned.project_id:
            raise PbzError("PROJECT_IDENTITY_CHANGED", "the project identity changed during the operation")
        applied = identity.resolve(ws, project, apply=True)
        if planned.project_id is not None and applied.project_id != planned.project_id:
            raise PbzError("PROJECT_IDENTITY_CHANGED", "the project identity changed during the operation")
        project_id = applied.project_id
        _ensure_project_namespace(ws, project_id)
    if applied.action == "refresh":
        notes.append(f"project identity {project_id} now recorded under name {project.name}")
        if applied.orphan_marked:
            notes.append(f"identity {applied.orphan_marked[0]} (recorded name {applied.orphan_marked[1]}) marked orphan")
    if pair is None or pair.project_id != project_id:
        pair = _SnapshotPair(ws, name, project_id)
        _preflight_snapshot_project(ws, name, project_id)
        _check_pair_readonly(ws, pair)
    # Step 6: guard, pair lock, revalidation, recovery, dispatch.
    job = create_kill_on_close_job() if os.name == "nt" else None
    try:
        guard = _acquire_project_guard(ws, project_id, "shared")
        try:
            with pair.locked(action) as handle:
                current = identity.resolve(ws, project, apply=False)
                if current.project_id != project_id:
                    raise PbzError("PROJECT_IDENTITY_CHANGED", "the project identity changed during the operation")
                _check_pair_readonly(ws, pair)
                pair.validate_transient()
                journal, before = pair.recover(action)
                # Reclassify under the lock: a flat pair published after the
                # step 3 classification governs the locked decision points.
                flat = _classify_flat_pair(ws, name)
                if action == "save":
                    stamped_spec = {**spec, "project_id": project_id}
                    res = _save_snapshot(ws, project, pair, raw_queries, stamped_spec, handle, job, journal, before, guard_handle=guard)
                    if flat["result"] or flat["spec"]:
                        notes.append("a legacy snapshot pair with this name remains unadopted")
                    return _snapshot_with_notes(res, notes)
                if raw_queries is None:
                    saved = saved_spec_for_project(ws, name, project_id)
                    if saved is None:
                        if flat["result"] or flat["spec"]:
                            raise PbzError("LEGACY_BASELINE_UNADOPTED", "a legacy snapshot pair with this name has not been adopted; run pbz snapshot adopt-legacy --project " + project.name)
                        raise PbzError("BAD_REQUEST", "--spec required")
                    raw_queries = _snapshot_query_list(saved)
                _require_usable_baseline(ws, name, project_id)
                if not os.path.lexists(pair.result) and flat["result"]:
                    raise PbzError("LEGACY_BASELINE_UNADOPTED", "a legacy snapshot baseline with this name has not been adopted; run pbz snapshot adopt-legacy --project " + project.name)
                request = _snapshot_request(ws, project, name, raw_queries, project_id)
                verb = "snapshot-compare"
                if not spend(ws, "snapshot_queries", len(raw_queries)):
                    return _snapshot_with_notes(_snapshot_budget(verb), notes)
                inheritance = ({"inherit_handles": (guard,), "kill_on_close_job": job}
                               if os.name == "nt" else {"pass_fds": (guard,)})
                res = engine_call(verb, request, timeout=1800, **inheritance)
                if not res.get("ok"):
                    raise _snapshot_engine_error(res)
                if res.get("project_id") != project_id:
                    raise PbzError("ENGINE_UNAVAILABLE", "engine response project_id does not match the selected project")
                out = {key: value for key, value in res.items() if key != "payload_state"}
                out["verb"] = verb
                return _snapshot_with_notes(out, notes)
        finally:
            _snapshot_close_handle(guard)
    finally:
        if job is not None:
            _snapshot_close_handle(job)


def saved_spec(ws, name: str) -> dict | None:
    try:
        path = _snapshot_spec_path(ws, name)
    except PbzError:
        return None
    try:
        if os.path.lexists(path):
            doc = json.loads(read_checked(path, root=ws.private).decode("utf-8-sig"))
            if is_snapshot_spec_doc(doc):
                return doc
            return None
    except PbzError:
        raise
    except (OSError, ValueError):
        return None
    try:
        leg_path = _legacy_snapshot_spec_path(ws, name)
        if os.path.lexists(leg_path):
            doc = json.loads(read_checked(leg_path, root=ws.private).decode("utf-8-sig"))
            if is_snapshot_spec_doc(doc):
                return doc
    except PbzError:
        raise
    except (OSError, ValueError):
        return None
    return None


def saved_spec_for_project(ws, name, project_id):
    try:
        path = _snapshot_spec_path(ws, name, project_id)
    except PbzError:
        return None
    try:
        if os.path.lexists(path):
            doc = json.loads(read_checked(path, root=ws.private).decode("utf-8-sig"))
            if is_snapshot_spec_doc(doc):
                _require_project_stamp(doc, project_id, "spec")
                return doc
    except PbzError:
        raise
    except (OSError, ValueError):
        return None
    return None


def _load_pair_doc(path):
    """Read one pair document for the read-only usability check. Unlike
    _load_snapshot_json this tolerates a hard-linked pair file (the pair
    machinery handles linked staged and spec files safely); symlinks and
    junctions are still refused, and unreadable or unparseable bytes are
    CORRUPT_STATE, mirroring _load_snapshot_json's error surface."""
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        return json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PbzError("CORRUPT_STATE", "snapshot baseline is unreadable") from None


def _inspected_present(path):
    """True when path exists; only a proven absence reads as missing."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"snapshot state cannot be inspected: {error}") from error
    return True


def _check_pair_readonly(ws, pair):
    pair.validate_transient()
    journal_present = _inspected_present(pair.journal)
    if journal_present:
        pair.read_journal()
    if not journal_present:
        # A journaled pair is arbitrated by recovery under the pair lock; the
        # content checks judge only a settled pair.
        if _inspected_present(pair.result):
            doc = _load_pair_doc(pair.result)
            if not is_snapshot_result_doc(doc):
                raise PbzError("CORRUPT_STATE", "snapshot baseline is not a result document")
            if pair.project_id:
                _require_project_stamp(doc, pair.project_id, "baseline")
        if _inspected_present(pair.spec):
            doc = _load_pair_doc(pair.spec)
            if not is_snapshot_spec_doc(doc):
                raise PbzError("CORRUPT_STATE", "snapshot spec is not a spec document")
            if pair.project_id:
                _require_project_stamp(doc, pair.project_id, "spec")
    adopt = os.path.join(pair.pending, pair.name + ".adopt.json")
    if _inspected_present(adopt):
        raise PbzError("ADOPTION_IN_PROGRESS", "snapshot adoption is in progress for this name; re-run pbz snapshot adopt-legacy to complete it")


# Classify the requested name's four flat candidates by shape per the S1 table.
# Flat documents are never used as a baseline or spec.
_FLAT_ABSENT = object()


def _flat_candidate(ws, path, name, kind):
    """Load one flat candidate document, or _FLAT_ABSENT when it is absent.

    Existence is probed with lstat: a candidate the process cannot inspect
    refuses instead of reading as absent, and a dangling link still reaches
    the loader, which refuses it as a link. A present document whose JSON
    value is null is returned as None and reaches shape validation.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return _FLAT_ABSENT
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"legacy snapshot {kind} for {name} cannot be inspected: {error}") from error
    try:
        return _load_snapshot_json(ws, path)
    except OSError as error:
        raise PbzError("CORRUPT_STATE", f"legacy snapshot {kind} for {name} is unreadable") from error


def _classify_flat_pair(ws, name):
    result_present = False
    for path, prelayout in (
        (_snapshot_result_path(ws, name, namespaced=True), False),
        (_snapshot_result_path(ws, name, namespaced=False), True),
    ):
        doc = _flat_candidate(ws, path, name, "result")
        if doc is _FLAT_ABSENT:
            continue
        if is_snapshot_result_doc(doc):
            result_present = True
        elif is_snapshot_spec_doc(doc) and prelayout and name.endswith(".spec"):
            continue
        else:
            raise PbzError(
                "CORRUPT_STATE", f"legacy snapshot result for {name} is malformed"
            )
    spec_present = False
    for path, legacy in (
        (_snapshot_spec_path(ws, name), False),
        (_legacy_snapshot_spec_path(ws, name), True),
    ):
        doc = _flat_candidate(ws, path, name, "spec")
        if doc is _FLAT_ABSENT:
            continue
        if is_snapshot_spec_doc(doc):
            spec_present = True
        elif is_snapshot_result_doc(doc) and legacy:
            continue
        else:
            raise PbzError(
                "CORRUPT_STATE", f"legacy snapshot spec for {name} is malformed"
            )
    return {"result": result_present, "spec": spec_present}


# ---------------------------------------------------------------------------
# Legacy adoption, file-only recovery, and rollback readout (S1 C3).
# The library entry points here serve the human-only snapshot subcommands;
# the CLI layer owns the HUMAN_ONLY gate and the typed confirmations.
# ---------------------------------------------------------------------------

_ADOPT_CLAIM_FIELDS = {"version", "target_name", "target_id", "adoption_id"}


def _adopt_claim_path(ws):
    return os.path.join(ws.private, "snapshots", "pending", "adopt-claim.json")


def _read_adopt_claim(ws):
    """(status, claim): status is 'absent', 'ok', or 'unverifiable'."""
    path = _adopt_claim_path(ws)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "absent", None
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"adoption claim cannot be inspected: {error}") from error
    try:
        raw = read_checked(path, root=ws.private)
    except PbzError:
        return "unverifiable", None
    except OSError:
        return "unverifiable", None
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "unverifiable", None
    if not isinstance(doc, dict) or not _ADOPT_CLAIM_FIELDS.issubset(doc):
        return "unverifiable", None
    if not identity.verify_doc(ws, doc):
        return "unverifiable", None
    return "ok", {field: value for field, value in doc.items() if field != "hmac"}


def _write_adopt_claim(ws, claim) -> None:
    signed = identity.sign_doc(ws, claim)
    data = (json.dumps(signed, indent=2, sort_keys=True) + "\n").encode("utf-8")
    os.makedirs(os.path.dirname(_adopt_claim_path(ws)), exist_ok=True)
    _durable_replace_bytes(_adopt_claim_path(ws), data, root=ws.private)


def _clear_adopt_claim(ws) -> None:
    try:
        os.unlink(_adopt_claim_path(ws))
    except FileNotFoundError:
        pass


def _stamped_bytes(doc, project_id) -> bytes:
    # The stamped representation is deterministic for a given source
    # document: the parsed document with project_id set, serialized the
    # same way the adapter serializes stamped pair documents elsewhere.
    stamped = dict(doc)
    stamped["project_id"] = project_id
    return json.dumps(stamped).encode("utf-8")


def _hash_bytes(data) -> str:
    return hashlib.sha256(data).hexdigest()



def _read_bytes_or_none(path, root):
    """File bytes, or None when the file is proven absent. Absence is
    established by lstat first: read_checked refuses a missing file with
    UNSAFE_PATH, so absence is never inferred from a failed read."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"snapshot state cannot be inspected: {error}") from error
    return read_checked(path, root=root)


def _adopt_journal_path(ws, project_id, name):
    return os.path.join(ws.private, "snapshots", "projects", project_id, "pending", name + ".adopt.json")


def _write_adopt_journal(ws, project_id, journal) -> None:
    path = _adopt_journal_path(ws, project_id, journal["name"])
    data = (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _snapshot_write(path, data, ".pbz-adopt-")


def _read_adopt_journal(ws, project_id, name):
    path = _adopt_journal_path(ws, project_id, name)
    raw = _read_bytes_or_none(path, ws.private)
    if raw is None:
        return None
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise PbzError("CORRUPT_STATE", f"adoption journal for {name} is unparseable") from error
    fields = {"version", "name", "target_id", "adoption_id", "state", "unit",
              "source_hashes", "expected_hashes", "dest_pre", "dest_pre_satisfied"}
    if not isinstance(doc, dict) or not fields.issubset(doc) or doc.get("version") != 1 \
            or doc.get("target_id") != project_id or doc.get("name") != name \
            or doc.get("state") not in ("PLANNED", "COPIES_WRITTEN", "DONE") \
            or not isinstance(doc.get("unit"), list) or not doc["unit"] \
            or not all(isinstance(kind, str) for kind in doc["unit"]) \
            or not set(doc["unit"]) <= {"result", "spec"}:
        raise PbzError("CORRUPT_STATE", f"adoption journal for {name} is malformed")
    for field in ("source_hashes", "expected_hashes", "dest_pre", "dest_pre_satisfied"):
        mapping = doc.get(field)
        if not isinstance(mapping, dict) or not set(doc["unit"]) <= set(mapping):
            raise PbzError("CORRUPT_STATE", f"adoption journal for {name} is malformed")
    if not all(isinstance(value, bool) for value in doc["dest_pre_satisfied"].values()):
        raise PbzError("CORRUPT_STATE", f"adoption journal for {name} is malformed")
    return doc


def _list_adopt_journals(ws, project_id) -> list[str]:
    directory = os.path.join(ws.private, "snapshots", "projects", project_id, "pending")
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"adoption journals cannot be listed: {error}") from error
    return sorted(entry[:-len(".adopt.json")] for entry in entries if entry.endswith(".adopt.json"))


def _flat_pair_paths(ws, name):
    return {
        "result": _snapshot_result_path(ws, name, namespaced=True),
        "spec": _snapshot_spec_path(ws, name),
        "journal": os.path.join(ws.private, "snapshots", "pending", name + ".journal.json"),
    }


def _post_normalization_flat(ws, layout_plan):
    """The flat pair inventory as it will stand after the approved layout
    normalization executes: name -> {result: bytes|None, spec: bytes|None,
    journal: bool}."""
    inventory: dict[str, dict] = {}

    def entry_for(name):
        return inventory.setdefault(name, {"result": None, "spec": None, "journal": False})

    results_dir = os.path.join(ws.private, "snapshots", "results")
    specs_dir = os.path.join(ws.private, "snapshots", "specs")
    for directory, kind in ((results_dir, "result"), (specs_dir, "spec")):
        try:
            entries = os.listdir(directory)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise PbzError("LOCAL_STATE_UNAVAILABLE", f"flat snapshot directory {directory} cannot be listed: {error}") from error
        for entry in entries:
            if entry.endswith(".json"):
                path = os.path.join(directory, entry)
                entry_for(entry[:-5])[kind] = read_checked(path, root=ws.private)
    pending_dir = os.path.join(ws.private, "snapshots", "pending")
    try:
        entries = os.listdir(pending_dir)
    except FileNotFoundError:
        entries = []
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"flat snapshot directory {pending_dir} cannot be listed: {error}") from error
    for entry in entries:
        if entry.endswith(".journal.json"):
            entry_for(entry[:-len(".journal.json")])["journal"] = True
    for _old, _target, kind, base, content, _exists in layout_plan:
        entry_for(base)[kind] = content
    return inventory


def _plan_adoption(ws, target, target_id, exclude) -> dict:
    """The read-only adoption plan for one target (canonical sequence step
    2): the layout normalization listing as step 0 plus one entry per flat
    name. target_id is the bound id, or the proposed id for a new target."""
    layout = _plan_snapshot_layout(ws)
    inventory = _post_normalization_flat(ws, layout)
    names = []
    foreign_stamped = []
    for name in sorted(inventory):
        if name in exclude:
            names.append({"name": name, "status": "excluded"})
            continue
        unit_data = inventory[name]
        if unit_data["journal"]:
            names.append({"name": name, "status": "blocked-journal",
                          "remedy": "pbz snapshot recover-legacy --name " + name})
            continue
        target_journal = os.path.join(ws.private, "snapshots", "projects", target_id,
                                      "pending", name + ".journal.json")
        if _inspected_present(target_journal):
            names.append({"name": name, "status": "blocked-journal",
                          "remedy": "pbz snapshot recover-pair --project " + target.name + " " + name})
            continue
        unit = [kind for kind in ("result", "spec") if unit_data[kind] is not None]
        if not unit:
            continue
        entry = {"name": name, "status": "adoptable", "unit": unit,
                 "source_hashes": {}, "expected_hashes": {}, "dest_pre": {}, "dest_pre_satisfied": {}}
        conflict = False
        for kind in unit:
            raw = unit_data[kind]
            try:
                doc = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError, RecursionError) as error:
                raise PbzError("CORRUPT_STATE", f"flat snapshot {kind} for {name} is unparseable; adoption changes nothing") from error
            if not isinstance(doc, dict):
                raise PbzError("CORRUPT_STATE", f"flat snapshot {kind} for {name} is not a snapshot document; adoption changes nothing")
            if kind == "result" and not is_snapshot_result_doc(doc):
                raise PbzError("CORRUPT_STATE", f"flat snapshot result for {name} does not have the snapshot result shape; adoption changes nothing")
            if kind == "spec" and not is_snapshot_spec_doc(doc):
                raise PbzError("CORRUPT_STATE", f"flat snapshot spec for {name} does not have the snapshot spec shape; adoption changes nothing")
            stamp = doc.get("project_id")
            if stamp is not None and stamp != target_id and name not in foreign_stamped:
                foreign_stamped.append(name)
            entry["source_hashes"][kind] = _hash_bytes(raw)
            stamped = _stamped_bytes(doc, target_id)
            entry["expected_hashes"][kind] = _hash_bytes(stamped)
            if kind == "result":
                dest = _snapshot_result_path(ws, name, namespaced=True, project_id=target_id)
            else:
                dest = _snapshot_spec_path(ws, name, project_id=target_id)
            dest_raw = _read_bytes_or_none(dest, ws.private)
            dest_hash = _hash_bytes(dest_raw) if dest_raw is not None else None
            entry["dest_pre"][kind] = dest_hash
            entry["dest_pre_satisfied"][kind] = dest_hash == entry["expected_hashes"][kind]
            if dest_hash is not None and not entry["dest_pre_satisfied"][kind]:
                conflict = True
        if conflict:
            entry["status"] = "conflict"
        elif all(entry["dest_pre_satisfied"].values()):
            entry["status"] = "already-adopted"
        names.append(entry)
    normalization = [
        {"source": os.path.basename(old), "target": os.path.relpath(target_path, ws.private),
         "kind": kind, "name": base, "sha256": _hash_bytes(content)}
        for old, target_path, kind, base, content, _exists in layout
    ]
    plan = {"target": target.name, "target_id": target_id,
            "normalization": normalization, "names": names,
            "foreign_stamped": foreign_stamped}
    return plan, layout


def _remove_flat_original(ws, name, kind, expected_hash) -> bool:
    """Remove one flat original after re-verifying its hash. Returns False
    when the original is already gone (a completed sub-step of a crash)."""
    path = _flat_pair_paths(ws, name)[kind]
    raw = _read_bytes_or_none(path, ws.private)
    if raw is None:
        return False
    if _hash_bytes(raw) != expected_hash:
        raise PbzError("ADOPTION_RECOVERY_BLOCKED", f"flat {kind} for {name} changed before removal; nothing further was deleted")
    remove_file(path, root=ws.private)
    return True


def _verify_published_destinations(ws, journal) -> None:
    """Every published destination half must still hash to the journal's
    expected stamped hash before any flat original is removed."""
    name = journal["name"]
    target_id = journal["target_id"]
    for kind in journal["unit"]:
        dest = _snapshot_result_path(ws, name, namespaced=True, project_id=target_id) if kind == "result" \
            else _snapshot_spec_path(ws, name, project_id=target_id)
        raw = _read_bytes_or_none(dest, ws.private)
        if raw is None or _hash_bytes(raw) != journal["expected_hashes"][kind]:
            raise PbzError("ADOPTION_RECOVERY_BLOCKED", f"adopted {kind} for {name} does not verify against the adoption journal; no original was removed")


def _finish_adoption_name(ws, journal) -> None:
    """From verified published copies: remove the flat originals, advance
    the journal to DONE, and delete it."""
    _verify_published_destinations(ws, journal)
    name = journal["name"]
    for kind in journal["unit"]:
        _remove_flat_original(ws, name, kind, journal["source_hashes"][kind])
    journal = dict(journal, state="DONE")
    _write_adopt_journal(ws, journal["target_id"], journal)
    os.unlink(_adopt_journal_path(ws, journal["target_id"], name))


def _settle_adoption_journal(ws, journal) -> str:
    """Settle one adoption journal under the transition table. Returns
    'completed', 'restart' (nothing published; the name re-enters fresh
    planning), or 'blocked'."""
    name = journal["name"]
    target_id = journal["target_id"]
    unit = journal["unit"]

    def dest_hash(kind):
        if kind == "result":
            path = _snapshot_result_path(ws, name, namespaced=True, project_id=target_id)
        else:
            path = _snapshot_spec_path(ws, name, project_id=target_id)
        raw = _read_bytes_or_none(path, ws.private)
        return _hash_bytes(raw) if raw is not None else None

    def source_hash(kind):
        path = _flat_pair_paths(ws, name)[kind]
        raw = _read_bytes_or_none(path, ws.private)
        return _hash_bytes(raw) if raw is not None else None

    current = {kind: dest_hash(kind) for kind in unit}
    for kind in unit:
        if current[kind] is not None and current[kind] != journal["dest_pre"][kind] \
                and current[kind] != journal["expected_hashes"][kind]:
            return "blocked"
    if journal["state"] == "DONE":
        os.unlink(_adopt_journal_path(ws, target_id, name))
        return "completed"
    if journal["state"] == "COPIES_WRITTEN":
        for kind in unit:
            if current[kind] != journal["expected_hashes"][kind]:
                # A copy that verified before the state advanced no longer
                # verifies: external interference. Delete nothing further.
                return "blocked"
        try:
            _finish_adoption_name(ws, journal)
        except PbzError as error:
            if error.code == "ADOPTION_RECOVERY_BLOCKED":
                return "blocked"
            raise
        return "completed"

    # PLANNED: classify publication progress per the observable test.
    published = [kind for kind in unit
                 if current[kind] == journal["expected_hashes"][kind] and not journal["dest_pre_satisfied"][kind]]
    satisfied = [kind for kind in unit if current[kind] == journal["expected_hashes"][kind]]
    if len(satisfied) == len(unit):
        journal = dict(journal, state="COPIES_WRITTEN")
        _write_adopt_journal(ws, target_id, journal)
        try:
            _finish_adoption_name(ws, journal)
        except PbzError as error:
            if error.code == "ADOPTION_RECOVERY_BLOCKED":
                return "blocked"
            raise
        return "completed"
    if published:
        # Complete publication of the pending halves, spec first.
        for kind in ("spec", "result"):
            if kind not in unit or current[kind] == journal["expected_hashes"][kind]:
                continue
            staged = os.path.join(ws.private, "snapshots", "projects", target_id, "pending", name + ".adopt-" + kind)
            raw = _read_bytes_or_none(staged, ws.private)
            data = raw if raw is not None and _hash_bytes(raw) == journal["expected_hashes"][kind] else None
            if data is None:
                if source_hash(kind) != journal["source_hashes"][kind]:
                    return "blocked"
                raw_source = read_checked(_flat_pair_paths(ws, name)[kind], root=ws.private)
                doc = json.loads(raw_source.decode("utf-8-sig"))
                data = _stamped_bytes(doc, target_id)
                if _hash_bytes(data) != journal["expected_hashes"][kind]:
                    return "blocked"
                _durable_replace_bytes(staged, data, root=ws.private)
            dest = _snapshot_result_path(ws, name, namespaced=True, project_id=target_id) if kind == "result" \
                else _snapshot_spec_path(ws, name, project_id=target_id)
            os.replace(staged, dest)
            _snapshot_sync_dir(os.path.dirname(dest))
        journal = dict(journal, state="COPIES_WRITTEN")
        _write_adopt_journal(ws, target_id, journal)
        try:
            _finish_adoption_name(ws, journal)
        except PbzError as error:
            if error.code == "ADOPTION_RECOVERY_BLOCKED":
                return "blocked"
            raise
        return "completed"

    # Nothing published: verify the flat originals, discard this adoption's
    # own staging artifacts (exact-hash proof only), and restart the name.
    for kind in unit:
        if source_hash(kind) != journal["source_hashes"][kind]:
            return "blocked"
    for kind in unit:
        staged = os.path.join(ws.private, "snapshots", "projects", target_id, "pending", name + ".adopt-" + kind)
        raw = _read_bytes_or_none(staged, ws.private)
        if raw is None:
            continue
        if _hash_bytes(raw) != journal["expected_hashes"][kind]:
            return "blocked"
        os.unlink(staged)
    os.unlink(_adopt_journal_path(ws, target_id, name))
    return "restart"


def _adopt_one_name(ws, target_id, adoption_id, entry) -> str:
    """Move one planned name into the target namespace under both pair
    locks. Returns 'adopted', 'already-adopted', or 'conflict'."""
    name = entry["name"]
    flat_pair = _SnapshotPair(ws, name)
    target_pair = _SnapshotPair(ws, name, project_id=target_id)
    with flat_pair.locked("save"):
        with target_pair.locked("save"):
            # Locked revalidation: the plan's hashes must still hold.
            for kind in entry["unit"]:
                raw = _read_bytes_or_none(_flat_pair_paths(ws, name)[kind], ws.private)
                if raw is None or _hash_bytes(raw) != entry["source_hashes"][kind]:
                    return "conflict"
                dest = target_pair.result if kind == "result" else target_pair.spec
                dest_raw = _read_bytes_or_none(dest, ws.private)
                dest_hash = _hash_bytes(dest_raw) if dest_raw is not None else None
                if dest_hash != entry["dest_pre"][kind]:
                    return "conflict"
            if _inspected_present(os.path.join(target_pair.pending, name + ".journal.json")):
                return "blocked"
            journal = {
                "version": 1, "name": name, "target_id": target_id,
                "adoption_id": adoption_id, "state": "PLANNED",
                "unit": entry["unit"],
                "source_hashes": entry["source_hashes"],
                "expected_hashes": entry["expected_hashes"],
                "dest_pre": entry["dest_pre"],
                "dest_pre_satisfied": entry["dest_pre_satisfied"],
            }
            _write_adopt_journal(ws, target_id, journal)
            pending = os.path.join(ws.private, "snapshots", "projects", target_id, "pending")
            for kind in entry["unit"]:
                if entry["dest_pre_satisfied"][kind]:
                    continue
                raw = read_checked(_flat_pair_paths(ws, name)[kind], root=ws.private)
                doc = json.loads(raw.decode("utf-8-sig"))
                data = _stamped_bytes(doc, target_id)
                if _hash_bytes(data) != entry["expected_hashes"][kind]:
                    raise PbzError("CORRUPT_STATE", f"stamped copy for {name} does not match the approved plan")
                _durable_replace_bytes(os.path.join(pending, name + ".adopt-" + kind), data, root=ws.private)
            for kind in ("spec", "result"):
                if kind not in entry["unit"] or entry["dest_pre_satisfied"][kind]:
                    continue
                staged = os.path.join(pending, name + ".adopt-" + kind)
                dest = target_pair.spec if kind == "spec" else target_pair.result
                os.replace(staged, dest)
                _snapshot_sync_dir(os.path.dirname(dest))
            _verify_published_destinations(ws, journal)
            journal = dict(journal, state="COPIES_WRITTEN")
            _write_adopt_journal(ws, target_id, journal)
            _finish_adoption_name(ws, journal)
            return "adopted"


def adopt_legacy(ws, target, *, exclude=frozenset(), confirm_fn) -> dict:
    """adopt-legacy: the canonical adoption sequence of the approved
    design, steps 1 to 8. confirm_fn receives the plan and must return
    True before the claim (the first write) is set."""
    with identity.identity_lock(ws):
        claim_status, claim = _read_adopt_claim(ws)
        if claim_status == "unverifiable":
            # The claim itself is unreadable, but adoption journals settle
            # from their own recorded state: finish what a previous,
            # owner-confirmed adoption started, then refuse. Once no
            # journal remains, --cancel-claim can clear the dead claim.
            resolution = identity.resolve(ws, target, apply=False)
            settled_names = []
            if resolution.project_id is not None:
                target_id = resolution.project_id
                _ensure_project_namespace(ws, target_id)
                guard = _acquire_project_guard(ws, target_id, mode="shared")
                try:
                    for name in _list_adopt_journals(ws, target_id):
                        journal = _read_adopt_journal(ws, target_id, name)
                        flat_pair = _SnapshotPair(ws, name)
                        target_pair = _SnapshotPair(ws, name, project_id=target_id)
                        with flat_pair.locked("save"):
                            with target_pair.locked("save"):
                                if _inspected_present(os.path.join(flat_pair.pending, name + ".journal.json")) \
                                        or _inspected_present(os.path.join(target_pair.pending, name + ".journal.json")):
                                    continue
                                if _settle_adoption_journal(ws, journal) == "completed":
                                    settled_names.append(name)
                finally:
                    _snapshot_close_handle(guard)
            raise PbzError("ADOPTION_CLAIMED", f"the adoption claim cannot be verified; settled {len(settled_names)} adoption journal(s) for {target.name} from their recorded state and started no new adoption; when no journal remains, clear the claim with pbz snapshot adopt-legacy --cancel-claim")
        resolution = identity.resolve(ws, target, apply=False)
        bound_id = resolution.project_id
        if claim is not None:
            if claim["target_name"] != target.name:
                raise PbzError("ADOPTION_CLAIMED", f"flat snapshot state is claimed by an adoption for project {claim['target_name']}; only that target's adoption may resume it")
            adoption_id = claim["adoption_id"]
            target_id = claim["target_id"]
            if bound_id is not None and target_id is not None and bound_id != target_id:
                raise PbzError("ADOPTION_CLAIMED", "the adoption claim names a different identity than the target's current binding")
            resuming = True
        else:
            adoption_id = str(uuid.uuid4())
            target_id = bound_id if bound_id is not None else str(uuid.uuid4())
            resuming = False

        # Settlement requires the namespace and its guard, so it runs only
        # for an already-bound target; a claim taken before binding has no
        # namespace and therefore no journals to settle, and its binding
        # step follows the plan confirmation below, as in the canonical
        # sequence.
        guard = None
        settled, blocked = [], []
        if bound_id is not None:
            _ensure_project_namespace(ws, target_id)
            guard = _acquire_project_guard(ws, target_id, mode="shared")
        try:
            if bound_id is not None:
                # Journal-first settlement: every journal of this claim's
                # adoption settles from its own recorded state, under the
                # flat and target pair locks, before fresh planning.
                for name in _list_adopt_journals(ws, target_id):
                    journal = _read_adopt_journal(ws, target_id, name)
                    if journal["adoption_id"] != adoption_id:
                        blocked.append({"name": name, "reason": "foreign adoption journal from an earlier adoption"})
                        continue
                    flat_pair = _SnapshotPair(ws, name)
                    target_pair = _SnapshotPair(ws, name, project_id=target_id)
                    with flat_pair.locked("save"):
                        with target_pair.locked("save"):
                            if _inspected_present(os.path.join(flat_pair.pending, name + ".journal.json")) \
                                    or _inspected_present(os.path.join(target_pair.pending, name + ".journal.json")):
                                blocked.append({"name": name, "reason": "an unsettled pair journal blocks settlement of this name"})
                                continue
                            outcome = _settle_adoption_journal(ws, journal)
                    if outcome == "completed":
                        settled.append(name)
                    elif outcome == "blocked":
                        blocked.append({"name": name, "reason": "adoption recovery blocked; see pbz snapshot rollback-check"})
                    # A "restart" outcome deleted the journal and left the
                    # flat pair untouched; the name re-enters fresh planning.

            plan, layout = _plan_adoption(ws, target, target_id, exclude)
            plan["adoption_id"] = adoption_id
            plan["resuming"] = resuming
            plan["settled_before_plan"] = settled
            plan["blocked"] = blocked
            if plan["foreign_stamped"]:
                raise PbzError("CORRUPT_STATE", f"flat snapshot documents for {', '.join(plan['foreign_stamped'])} already carry another project's stamp; adoption never re-stamps another project's documents")
            if not confirm_fn(plan):
                return {"ok": False, "confirmed": False, "command": "adopt-legacy", "plan": plan}

            if claim is None:
                _write_adopt_claim(ws, {"version": 1, "target_name": target.name,
                                        "target_id": target_id, "adoption_id": adoption_id})
            if bound_id is None:
                identity.mint_with_id(ws, target, target_id)
                bound_id = target_id
                _ensure_project_namespace(ws, target_id)
                guard = _acquire_project_guard(ws, target_id, mode="shared")

            report = {"ok": True, "confirmed": True, "command": "adopt-legacy",
                      "target": target.name, "target_id": target_id,
                      "adopted": list(settled), "conflicts": [], "blocked": list(blocked),
                      "excluded": sorted(exclude), "partial": False}

            if layout:
                lock_names = set()
                for old, _t, _kind, base, _content, _exists in layout:
                    lock_names.add(base)
                    filename = os.path.basename(old)
                    if filename.endswith(".spec.json"):
                        lock_names.add(filename[:-len(".json")])
                flat_pairs = [_SnapshotPair(ws, lock_name) for lock_name in sorted(lock_names)]
                contexts = [pair.locked("save") for pair in flat_pairs]
                entered = []
                try:
                    for context in contexts:
                        context.__enter__()
                        entered.append(context)
                    progress: list = []
                    try:
                        with tokens.WorkspaceLock(ws.private):
                            _execute_snapshot_layout(ws, layout, revalidate=True, progress=progress)
                    except PbzError as error:
                        report["normalization"] = {"completed": [base for _o, _t, _k, base in progress],
                                                   "failed": error.code}
                        report["partial"] = True
                        return report
                finally:
                    for context in reversed(entered):
                        context.__exit__(None, None, None)

            blocked_names = {entry["name"] for entry in blocked}
            settled_names = set(settled)
            journal_names = set(_list_adopt_journals(ws, target_id))
            for entry in plan["names"]:
                name = entry["name"]
                if entry["status"] == "excluded":
                    continue
                if name in settled_names and entry["status"] == "already-adopted":
                    # Settled from its journal in this run; the plan entry
                    # only reflects the state settlement produced.
                    continue
                if name in blocked_names or name in journal_names:
                    if not any(item["name"] == name for item in report["blocked"]):
                        report["blocked"].append({"name": name, "reason": "an adoption journal for this name is unresolved"})
                    continue
                if entry["status"] == "blocked-journal":
                    report["blocked"].append({"name": name, "reason": entry["remedy"]})
                    continue
                if entry["status"] == "conflict":
                    report["conflicts"].append(name)
                    continue
                outcome = _adopt_one_name(ws, target_id, adoption_id, entry)
                if outcome == "conflict":
                    report["conflicts"].append(name)
                elif outcome == "blocked":
                    report["blocked"].append({"name": name, "reason": "an unsettled pair journal blocks this name; see pbz snapshot rollback-check"})
                else:
                    report["adopted"].append(name)
        finally:
            if guard is not None:
                _snapshot_close_handle(guard)

        remaining = _list_adopt_journals(ws, target_id)
        unresolved = [name for name in remaining
                      if _read_adopt_journal(ws, target_id, name)["adoption_id"] == adoption_id]
        if unresolved:
            report["partial"] = True
            already = {entry["name"] for entry in report["blocked"]}
            report["blocked"].extend({"name": name, "reason": "adoption journal unresolved"}
                                      for name in unresolved if name not in already)
        else:
            _clear_adopt_claim(ws)
            report["claim_released"] = True
        report["partial"] = report["partial"] or bool(report["blocked"]) or bool(report["conflicts"])
        return report


def _adopt_journal_names_anywhere(ws, project_id) -> list[str]:
    """Adoption journal names for one identity in the active namespace
    and, when the namespace was quarantined, in quarantine."""
    names = list(_list_adopt_journals(ws, project_id))
    pending = os.path.join(ws.private, "snapshots", "quarantine", project_id, "pending")
    try:
        entries = os.listdir(pending)
    except FileNotFoundError:
        entries = []
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"quarantined namespace {project_id} cannot be listed: {error}") from error
    names.extend(entry[:-len(".adopt.json")] for entry in entries if entry.endswith(".adopt.json"))
    return sorted(set(names))


def cancel_adoption_claim(ws) -> dict:
    """adopt-legacy --cancel-claim: release the claim when no adoption
    journal for it remains unresolved. The target stays bound."""
    with identity.identity_lock(ws):
        claim_status, claim = _read_adopt_claim(ws)
        if claim_status == "absent":
            return {"ok": True, "command": "adopt-legacy --cancel-claim", "claim": None}
        if claim_status == "unverifiable":
            outstanding = []
            for project_id in sorted(identity.namespace_ids(ws) | identity.quarantine_ids(ws)):
                outstanding.extend(f"{project_id}/{name}"
                                   for name in _adopt_journal_names_anywhere(ws, project_id))
            if outstanding:
                raise PbzError("ADOPTION_CLAIMED", f"the adoption claim cannot be verified and adoption journals remain ({', '.join(outstanding)}); re-run pbz snapshot adopt-legacy for the target to settle them, then cancel")
            _clear_adopt_claim(ws)
            return {"ok": True, "command": "adopt-legacy --cancel-claim",
                    "claim": "unverifiable claim cleared; no adoption journals were outstanding"}
        target_id = claim["target_id"]
        unresolved = []
        if target_id is not None:
            for name in _adopt_journal_names_anywhere(ws, target_id):
                active = _read_adopt_journal(ws, target_id, name)
                if active is not None and active["adoption_id"] == claim["adoption_id"]:
                    unresolved.append(name)
                raw = _read_bytes_or_none(os.path.join(
                    ws.private, "snapshots", "quarantine", target_id, "pending", name + ".adopt.json"), ws.private)
                if raw is not None:
                    try:
                        doc = json.loads(raw.decode("utf-8-sig"))
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        doc = None
                    required = {"version", "name", "target_id", "adoption_id", "state", "unit",
                                "source_hashes", "expected_hashes", "dest_pre", "dest_pre_satisfied"}
                    if not isinstance(doc, dict) or not required.issubset(doc) \
                            or not isinstance(doc.get("adoption_id"), str) or not doc["adoption_id"] \
                            or doc.get("target_id") != target_id or doc.get("name") != name:
                        # Ownership of the quarantined copy cannot be
                        # established; the claim stays until it can.
                        unresolved.append(name + " (quarantined journal unreadable)")
                    elif doc["adoption_id"] == claim["adoption_id"]:
                        unresolved.append(name)
        if unresolved:
            raise PbzError("ADOPTION_CLAIMED", f"cancellation refused: adoption journals remain unresolved for {', '.join(unresolved)}; re-run pbz snapshot adopt-legacy --project {claim['target_name']} to settle them")
        _clear_adopt_claim(ws)
        return {"ok": True, "command": "adopt-legacy --cancel-claim",
                "claim": claim, "target_stays_bound": target_id}


def recover_legacy(ws, name) -> dict:
    """recover-legacy: settle one flat pair journal with compare-mode
    recovery. No engine, no port, no budget; binds nothing."""
    _validate_snapshot_name(name)
    with identity.identity_lock(ws):
        pair = _SnapshotPair(ws, name)
        with pair.locked("compare"):
            pair.validate_transient()
            if not _inspected_present(pair.journal):
                return {"ok": True, "command": "recover-legacy", "name": name, "journal": None}
            journal = pair.read_journal()
            pair.recover("compare")
            return {"ok": True, "command": "recover-legacy", "name": name,
                    "journal": journal.get("state") if journal else None, "settled": True}


def recover_pair(ws, target, name) -> dict:
    """recover-pair: ordinary pair recovery inside one project's
    namespace, with no capability ping and no engine contact."""
    _validate_snapshot_name(name)
    resolution = identity.resolve(ws, target, apply=False)
    if resolution.project_id is None:
        raise PbzError("PROJECT_IDENTITY_UNBOUND", f"project {target.name} has no bound identity; there is no namespace to recover in")
    project_id = resolution.project_id
    pair = _SnapshotPair(ws, name, project_id=project_id)
    guard = _acquire_project_guard(ws, project_id, mode="shared")
    try:
        anchors = identity.discover_anchors(ws)
        registry = identity.load_registry(ws)
        record = (registry or {}).get(project_id)
        if anchors.get(target.name.lower()) != project_id or record is None \
                or record["status"] != "bound" or record["name"] != target.name:
            raise PbzError("PROJECT_IDENTITY_CHANGED", f"project {target.name} no longer resolves to identity {project_id}")
        with pair.locked("compare"):
            # No ADOPTION_IN_PROGRESS refusal here: recover-pair is the
            # remedy that unblocks an adoption settlement waiting on this
            # pair journal, and adoption revalidates every byte it
            # publishes, so settling the pair first cannot be raced into
            # a wrong adoption outcome. (Ordinary save and compare do
            # refuse while an adoption journal exists.)
            pair.validate_transient()
            if not _inspected_present(pair.journal):
                return {"ok": True, "command": "recover-pair", "name": name, "journal": None}
            journal = pair.read_journal()
            pair.recover("compare")
            return {"ok": True, "command": "recover-pair", "name": name,
                    "journal": journal.get("state") if journal else None, "settled": True}
    finally:
        _snapshot_close_handle(guard)


def rollback_check(ws) -> dict:
    """rollback-check: read-only unsettled-state readout for the private
    store this invocation resolved. Changes nothing."""
    def _listdir(directory):
        try:
            return sorted(os.listdir(directory)), False
        except FileNotFoundError:
            return [], False
        except OSError:
            return [], True

    flat = {"store": "flat", "pair_journals": [], "adoption_journals": []}
    pending_dir = os.path.join(ws.private, "snapshots", "pending")
    entries, unreadable = _listdir(pending_dir)
    if unreadable:
        flat["unreadable"] = True
    for entry in entries:
        if entry.endswith(".journal.json"):
            name = entry[:-len(".journal.json")]
            try:
                doc = _load_snapshot_json(ws, os.path.join(pending_dir, entry))
                flat["pair_journals"].append({"name": name, "state": doc.get("state") if isinstance(doc, dict) else None})
            except PbzError:
                flat["pair_journals"].append({"name": name, "state": "unreadable"})
    stores = [flat]
    store_ids = [(project_id, False) for project_id in sorted(identity.namespace_ids(ws))]
    store_ids += [(project_id, True) for project_id in sorted(identity.quarantine_ids(ws))]
    for project_id, in_quarantine in store_ids:
        base = os.path.join(ws.private, "snapshots", "quarantine" if in_quarantine else "projects", project_id, "pending")
        store = {"store": project_id, "quarantined": in_quarantine, "pair_journals": [], "adoption_journals": []}
        entries, unreadable = _listdir(base)
        if unreadable:
            store["unreadable"] = True
        for entry in entries:
            if entry.endswith(".journal.json") and not entry.endswith(".adopt.json"):
                name = entry[:-len(".journal.json")]
                try:
                    doc = _load_snapshot_json(ws, os.path.join(base, entry))
                    store["pair_journals"].append({"name": name, "state": doc.get("state") if isinstance(doc, dict) else None})
                except PbzError:
                    store["pair_journals"].append({"name": name, "state": "unreadable"})
            elif entry.endswith(".adopt.json"):
                store["adoption_journals"].append(entry[:-len(".adopt.json")])
        stores.append(store)
    claim_status, claim = _read_adopt_claim(ws)
    intent_present = os.path.lexists(identity.repair_intent_path(ws))
    unsettled = any(store["pair_journals"] or store["adoption_journals"] or store.get("unreadable") for store in stores)
    return {
        "ok": True, "command": "rollback-check",
        "private_root": os.path.abspath(ws.private),
        "stores": stores,
        "adoption_claim": claim if claim_status == "ok" else ({"unverifiable": True} if claim_status == "unverifiable" else None),
        "repair_intent_present": intent_present,
        "passes": not unsettled and claim_status == "absent",
    }
