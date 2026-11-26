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
from contextlib import contextmanager
from pathlib import Path

from . import desktop, dpapi, lexers, policy as policy_mod, tmdl, tokens
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


def _snapshot_spec_path(ws, name: str) -> str:
    """One path segment under the private snapshots/specs directory. No .., separator, or absolute name."""
    _validate_snapshot_name(name)
    root = os.path.abspath(os.path.join(ws.private, "snapshots", "specs"))
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


def migrate_snapshot_layout(ws) -> dict:
    """Migrate legacy flat snapshots/ files into snapshots/results/ and snapshots/specs/.

    Protected by WorkspaceLock. Safe, two-pass, idempotent, and fail-closed.
    """
    with tokens.WorkspaceLock(ws.private):
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
            return {"migrated": False, "results": [], "specs": []}

        # Pass 1: Read-only validation & planning. Strictly no filesystem mutation occurs in this pass.
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

        # Pass 2: Execution. Destination directories are created ONLY after Pass 1 validation succeeds.
        if not plan:
            return {"migrated": False, "results": [], "specs": []}

        os.makedirs(results_dir, exist_ok=True)
        os.makedirs(specs_dir, exist_ok=True)
        if _reparse_link(results_dir) or _reparse_link(specs_dir):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")

        migrated_results = []
        migrated_specs = []
        for old_path, target, kind, base, content, already_exists in plan:
            if not already_exists:
                replace_bytes(target, content, root=ws.private)
            remove_file(old_path, root=ws.private)
            if kind == "result":
                migrated_results.append(base)
            else:
                migrated_specs.append(base)

        return {
            "migrated": bool(migrated_results or migrated_specs),
            "results": sorted(migrated_results),
            "specs": sorted(migrated_specs),
        }


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


def _snapshot_result_path(ws, name: str, *, namespaced: bool) -> str:
    """Result path for one snapshot name. Does not create the directory."""
    _validate_snapshot_name(name)
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


def _require_usable_baseline(ws, name: str) -> None:
    """Fail closed when this name's baseline cannot be read. Other files are ignored.

    A namespaced result wins and is never replaced by a legacy file. A missing
    baseline is left for the engine. A spec-shaped legacy file is not a result
    and is left in place for the existing engine behavior.
    """
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


def _snapshot_open_lock(path, readonly):
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
    def __init__(self, ws, name):
        self.name = name
        self.result = _snapshot_result_path(ws, name, namespaced=True)
        self.spec = _snapshot_spec_path(ws, name)
        self.root = os.path.dirname(os.path.dirname(self.result))
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
        ordered = {key: journal[key] for key in _SNAPSHOT_JOURNAL_FIELDS}
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
        if not _valid_snapshot_journal(journal):
            raise PbzError("CORRUPT_STATE", "invalid snapshot journal")
        return journal

    def staged_bytes(self, journal):
        try:
            data = Path(self.staged).read_bytes()
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == journal["staged_spec"] else None

    def complete(self, journal, data):
        if data is None:
            raise _snapshot_recovery_error()
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


def _valid_snapshot_journal(journal):
    fields = set(_SNAPSHOT_JOURNAL_FIELDS)
    annotations = set(_SNAPSHOT_JOURNAL_ANNOTATIONS)
    if (not isinstance(journal, dict) or not fields <= journal.keys()
            or journal.keys() - fields - annotations
            or type(journal["version"]) is not int or journal["version"] != 1
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


def _snapshot_request(ws, project, name, raw_queries):
    tm = tokens.TokenMap(ws.private)
    queries = []
    for q in raw_queries:
        dax, unknown = tokens.detokenize_dax(q["dax"], tm)
        if unknown:
            raise PbzError("BAD_REQUEST", "unknown token in query " + q["id"])
        queries.append({"id": q["id"], "dax": dax, "keys": q.get("keys", [])})
    return {"private_dir": ws.private, "port": desktop.require_port(project),
            "name": name, "queries": queries}


def _snapshot_budget(verb):
    return {"verb": verb, "status": "BUDGET",
            "note": "daily snapshot query budget reached; the user can reset it"}


def _snapshot_engine_error(res):
    return PbzError(_class_of(res.get("class"), _PY_CLASS) or "ENGINE_UNAVAILABLE")


def _save_snapshot(ws, project, pair, raw_queries, spec, handle, job, journal, before):
    verb = "snapshot-save"
    superseding = journal is not None
    try:
        request = _snapshot_request(ws, project, pair.name, raw_queries)
        data = json.dumps(spec).encode()
    except Exception as error:
        if superseding:
            pair.finish_refusal(journal, before, error, superseding=True, never_wrote=True)
        raise

    if not superseding:
        before = pair.hashes()
    candidate = {
        "version": 1, "state": "PREPARED", "before_result": before[0],
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

    inheritance = ({"inherit_handles": (handle,), "kill_on_close_job": job}
                   if os.name == "nt" else {"pass_fds": (handle,)})
    res = engine_call(verb, request, timeout=1800, **inheritance)
    state = res.get("payload_state")
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
            new_result = _snapshot_hash(pair.result, sync=True)
            _snapshot_sync_dir(os.path.dirname(pair.result))
            _snapshot_sync_dir(pair.root)
            if superseding:
                pair.stage(data)
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


def snapshot(ws, project, action: str, name: str, spec: dict | None) -> dict:
    if action not in ("save", "compare"):
        raise PbzError("BAD_REQUEST", f"unknown snapshot action {action}")
    _validate_snapshot_name(name)
    raw_queries = None if action == "compare" and spec is None else _snapshot_query_list(spec)
    _preflight_snapshot_save(ws, name)
    pair = _SnapshotPair(ws, name)
    job = create_kill_on_close_job() if action == "save" and os.name == "nt" else None
    try:
        with pair.locked(action) as handle:
            pair.validate_transient()
            journal, before = pair.recover(action)
            if action == "save":
                return _save_snapshot(ws, project, pair, raw_queries, spec, handle, job, journal, before)
            if raw_queries is None:
                spec = saved_spec(ws, name)
                if spec is None:
                    raise PbzError("BAD_REQUEST", "--spec required")
                raw_queries = _snapshot_query_list(spec)
            _require_usable_baseline(ws, name)
            request = _snapshot_request(ws, project, name, raw_queries)
            verb = "snapshot-compare"
            if not spend(ws, "snapshot_queries", len(raw_queries)):
                return _snapshot_budget(verb)
            res = engine_call(verb, request, timeout=1800)
            if not res.get("ok"):
                raise _snapshot_engine_error(res)
            return {**res, "verb": verb}
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
