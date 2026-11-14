"""Revision-bound evidence and `pbz review`: what changed, what it touches, how it ships, which checks
ran on which revision, and what is still unverified.

Every verb that proves something (validate, push, desktop open/reload, health, check, snapshot
compare, smoke, synth test run, handoff) appends a line to `.pbz/evidence.jsonl`: the stage, verdict
counts and ids only (never values), and the revision it ran on: a fingerprint of the mirror and of the
real files as pbz last wrote them. `pbz review` turns pending edits plus that log into a review packet
(`pbz-workspace/REVIEW.md` and JSON). Evidence from another revision is STALE, never PASS.
`pbz review --accept` records the human's acceptance of the current revision (terminal only).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import stat
import unicodedata

from . import dpapi
from .common import (
    PbzError, SIZE_CAP, append_checked, pbz_state_path, read_checked, write_text, _reparse_link,
    _workspace_root,
)

STAGES = ("PARSED", "BOUND", "LOADED", "EXECUTED", "STATIC_CONTRACT", "SYNTHETIC", "HANDOFF", "HUMAN_ACCEPTED")
LOG = "evidence.jsonl"
LOG_CAP = 1024 * 1024
GENESIS = "0" * 64
_CHAIN_RE = re.compile(r"^[0-9a-f]{64}$")
OVER_CAP = (
    "evidence log is over 1 MiB; archive or remove .pbz/evidence.jsonl by hand in your own terminal"
)
UNUSABLE = "evidence log is unusable"
CLASS_ORDER = ["SECURITY_REVIEW", "REFRESH_REQUIRED", "RECALC_REQUIRED", "LOGIC", "METADATA_ONLY", "REPORT_ONLY", "UNKNOWN"]
_VERBS = {
    "validate", "push", "desktop open", "desktop reload", "health", "check",
    "snapshot compare", "snapshot save", "smoke", "interactions", "synth-test run",
    "handoff", "review --accept",
}
_STATIC_RESULTS = {
    "MATCH", "MISMATCH", "DEFAULT_UNRESOLVED", "MISSING_ID", "RUNTIME_ONLY",
    "LEGACY", "UNKNOWN_TYPE", "UNSUPPORTED", "REFUSED",
}
_TOP = {"ts", "project", "verb", "mirror_rev", "real_rev", "stage", "summary", "scope"}
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_MREV = re.compile(r"^m[0-9a-f]{12}$")
_RREV = re.compile(r"^r[0-9a-f]{12}$")
_SYNTH_RESULTS = {"SAME", "DIFF", "ERROR", "NOT_COMPARABLE"}
_VALIDATION = {"PASS", "FAIL", "NOT_RUN"}
_LOOSE = 256


# ---------------------------------------------------------------- revisions

def mirror_rev(ws, project) -> str:
    from . import mirror
    h = hashlib.sha256()
    for rel, text in sorted(mirror.mirror_texts(ws, project).items()):
        h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(text.encode("utf-8")).digest())
    return "m" + h.hexdigest()[:12]


def real_rev(ws, project) -> str:
    from . import mirror
    man = mirror.load_manifest(ws, project) or {}
    h = hashlib.sha256()
    for rel, rec in sorted((man.get("files") or {}).items()):
        h.update(f"{rel}\0{rec.get('real')}\n".encode("utf-8"))
    return "r" + h.hexdigest()[:12]


def _log_path(ws) -> str:
    return pbz_state_path(ws, LOG)


def _io_error(err: PbzError) -> PbzError:
    if err.code == "REFUSED" and err.detail == SIZE_CAP:
        return PbzError("REFUSED", OVER_CAP)
    return err


def _read_log(ws, path: str | None = None) -> str | None:
    """Checked log text. Missing is None. Present-unusable is REFUSED or UNSAFE_PATH, never a silent empty log."""
    path = path or _log_path(ws)
    if not os.path.lexists(path):
        return None
    try:
        data = read_checked(path, root=_workspace_root(ws), max_bytes=LOG_CAP)
    except PbzError as err:
        raise _io_error(err) from None
    except OSError:
        raise PbzError("REFUSED", UNUSABLE) from None
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise PbzError("REFUSED", UNUSABLE) from None


def _segment_paths(ws) -> list[str]:
    """Rotated segments, oldest first. A junction at the segment directory is refused."""
    root = _workspace_root(ws)
    directory = os.path.join(root, ".pbz", "evidence.d")
    if not os.path.lexists(directory):
        return []
    directory = pbz_state_path(ws, "evidence.d")
    out = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        raise PbzError("REFUSED", UNUSABLE) from None
    for name in names:
        if re.fullmatch(r"\d{4}\.jsonl", name):
            out.append(os.path.join(directory, name))
    return out


def _log_files(ws) -> list[str]:
    files = list(_segment_paths(ws))
    active = _log_path(ws)
    if os.path.lexists(active):
        files.append(active)
    return files


def _nocr(value):
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, dict):
        return {k: _nocr(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nocr(v) for v in value]
    return value


def _canon(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "chain_mac"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mac(key: bytes, prev: str, entry: dict) -> str:
    return hmac.new(key, (prev + "\n" + _canon(entry)).encode("utf-8"), hashlib.sha256).hexdigest()


def _chain_ok(key, prev: str, obj: dict) -> bool:
    """True when verify_chain would take this object as the next link."""
    got_prev = obj.get("chain_prev")
    got_mac = obj.get("chain_mac")
    if key is None or got_prev != prev or not isinstance(got_mac, str):
        return False
    return _mac(key, prev, obj) == got_mac


def _chain_key(ws):
    try:
        return dpapi.load_key(ws.private, create=False)
    except FileNotFoundError:
        return None


def _accepted_macs(ws) -> set[str]:
    """chain_mac values verify_chain would count before the first break."""
    key = _chain_key(ws)
    prev = _anchor(ws)
    found = set()
    for path in _log_files(ws):
        text = _read_log(ws, path)
        stopped = False
        for line in (text or "").split("\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                stopped = True
                break
            if not isinstance(obj, dict) or "chain_mac" not in obj:
                stopped = True
                break
            if not _chain_ok(key, prev, obj):
                stopped = True
                break
            prev = obj["chain_mac"]
            found.add(prev)
        if stopped:
            break
    return found


def _index_path(ws) -> str:
    return pbz_state_path(ws, "evidence.chain.json")


def _anchor(ws) -> str:
    path = os.path.join(_workspace_root(ws), ".pbz", "evidence.chain.json")
    if not os.path.lexists(path):
        return GENESIS
    try:
        raw = read_checked(_index_path(ws), root=_workspace_root(ws), max_bytes=4096)
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, PbzError):
        return GENESIS
    anchor = doc.get("anchor") if isinstance(doc, dict) else None
    return anchor if isinstance(anchor, str) and _CHAIN_RE.fullmatch(anchor) else GENESIS


def _last_mac(text: str | None) -> str | None:
    last = None
    for line in (text or "").split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        mac = obj.get("chain_mac") if isinstance(obj, dict) else None
        if isinstance(mac, str) and _CHAIN_RE.fullmatch(mac):
            last = mac
    return last


def _tip(ws) -> str:
    """MAC the next record must cite. An already-over-cap active file is refused here.

    A MAC in the active log wins. An active log with none yields the newest rotated
    segment's last MAC, then the anchor. Prune order is unchanged.
    """
    active = _log_path(ws)
    if os.path.lexists(active):
        mac = _last_mac(_read_log(ws, active))
        if mac:
            return mac
    segs = _segment_paths(ws)
    if segs:
        return _last_mac(_read_log(ws, segs[-1])) or _anchor(ws)
    return _anchor(ws)


def _stamp(ws, entries: list[dict]) -> None:
    key = dpapi.load_key(ws.private)
    prev = _tip(ws)
    for i, entry in enumerate(entries):
        clean = _nocr(entry)
        clean.pop("chain_mac", None)
        clean["chain_prev"] = prev
        clean["chain_mac"] = _mac(key, prev, clean)
        entries[i] = clean
        prev = clean["chain_mac"]


def _rotate(ws) -> str:
    """Move the active log to the next numbered segment, after the highest existing number.

    Reusing a hole left by prune would put a new segment before an older one and break the
    HMAC chain order. The active path is free afterwards.
    """
    src = _log_path(ws)
    directory = pbz_state_path(ws, "evidence.d")
    os.makedirs(directory, exist_ok=True)
    if _reparse_link(directory):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    nums = []
    try:
        names = os.listdir(directory)
    except OSError:
        raise PbzError("REFUSED", UNUSABLE) from None
    for name in names:
        if len(name) == 10 and name.endswith(".jsonl") and name[:4].isdigit() and name[4] == ".":
            nums.append(int(name[:4]))
    n = max(nums, default=0) + 1
    if n > 9999:
        raise PbzError("REFUSED", UNUSABLE)
    dest = os.path.join(directory, f"{n:04d}.jsonl")
    if os.path.lexists(dest):
        raise PbzError("REFUSED", UNUSABLE)
    os.replace(src, dest)
    return dest


def _newest_day(text: str) -> str | None:
    newest = None
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        ts = obj.get("ts") if isinstance(obj, dict) else None
        if isinstance(ts, str) and _TS.fullmatch(ts):
            day = ts[:10]
            if newest is None or day > newest:
                newest = day
    return newest


def prune(ws, today: dt.date | None = None) -> dict:
    """Drop a prefix of rotated segments whose newest record is outside the ledger window.

    The anchor is written before any segment is deleted. A crash after that write leaves an
    anchor that the next prune can finish; a deleted segment without a new anchor cannot be
    verified. Undated segments stay. The active log is never pruned.
    """
    from .checks import LEDGER_DAYS
    cutoff = ((today or dt.date.today()) - dt.timedelta(days=LEDGER_DAYS)).isoformat()
    anchor = _anchor(ws)
    doomed = []
    for path in _segment_paths(ws):
        text = _read_log(ws, path)
        if text is None:
            continue
        newest = _newest_day(text)
        if newest is None or newest >= cutoff:
            break
        mac = _last_mac(text)
        if mac:
            anchor = mac
        doomed.append(path)
    if doomed:
        write_text(_index_path(ws), json.dumps({"anchor": anchor}) + "\n", root=_workspace_root(ws))
        for path in doomed:
            try:
                os.remove(path)
            except OSError:
                raise PbzError("REFUSED", UNUSABLE) from None
    return {"verb": "evidence", "pruned_segments": len(doomed), "anchor_kept": bool(doomed)}


def verify_chain(ws) -> dict:
    """Recompute the HMAC chain across rotated segments and the active log."""
    key = _chain_key(ws)
    prev = _anchor(ws)
    chained = 0
    files = _log_files(ws)
    for path in files:
        text = _read_log(ws, path)
        for line in (text or "").split("\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                return {"verb": "evidence", "chain": "BROKEN", "reason": "UNCHAINED", "records": chained}
            if not isinstance(obj, dict) or "chain_mac" not in obj:
                return {"verb": "evidence", "chain": "BROKEN", "reason": "UNCHAINED", "records": chained}
            if not _chain_ok(key, prev, obj):
                reason = "NO_KEY" if key is None else "MISMATCH"
                return {"verb": "evidence", "chain": "BROKEN", "reason": reason, "records": chained}
            prev = obj["chain_mac"]
            chained += 1
    return {"verb": "evidence", "chain": "OK", "records": chained, "files": len(files)}


def _append_lines(ws, entries: list[dict]) -> None:
    """One checked append for the whole batch. A file already over 1 MiB is refused.

    A batch that would cross the cap rotates the active log into a numbered segment first,
    then writes the batch into a new active log. The HMAC chain continues across that split.
    """
    if not entries:
        return
    _stamp(ws, entries)
    text = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
    if len(text.encode("utf-8")) > LOG_CAP:
        raise PbzError("REFUSED", OVER_CAP)
    path = _log_path(ws)
    root = _workspace_root(ws)
    try:
        append_checked(path, text, root=root, max_bytes=LOG_CAP)
    except PbzError as err:
        if not (err.code == "REFUSED" and err.detail == SIZE_CAP and os.path.lexists(path)):
            raise _io_error(err) from None
        try:
            size = os.path.getsize(path)
        except OSError:
            raise PbzError("REFUSED", OVER_CAP) from None
        if size > LOG_CAP:
            raise PbzError("REFUSED", OVER_CAP) from None
        _rotate(ws)
        try:
            append_checked(path, text, root=root, max_bytes=LOG_CAP)
        except PbzError as err2:
            raise _io_error(err2) from None
        except OSError:
            raise PbzError("REFUSED", UNUSABLE) from None
    except OSError:
        raise PbzError("REFUSED", UNUSABLE) from None
    try:
        prune(ws)
    except PbzError:
        # The batch is already durable. A prune problem must not look like a lost append.
        pass


def _controls(value: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def _loose(value) -> bool:
    return isinstance(value, str) and len(value) <= _LOOSE and not _controls(value)


def _nn(value) -> bool:
    return type(value) is int and value >= 0


def _bool(value) -> bool:
    return type(value) is bool


def _canonical_ts(value) -> str | None:
    if not isinstance(value, str) or not _TS.fullmatch(value):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None or parsed.microsecond:
        return None
    rendered = parsed.replace(microsecond=0).isoformat()
    return rendered if rendered == value else None


def _components(value, *, synth: bool = False) -> set[str] | None:
    """Scope pieces a companion field may produce, including the null `?` / `None` fallbacks."""
    if value is None:
        return {"?", "None"}
    if synth:
        from .synthtest import NAME_RE
        if not isinstance(value, str) or not NAME_RE.fullmatch(value):
            return None
        return {value}
    if not _loose(value):
        return None
    return {value}


def _count_map(value) -> bool:
    if not isinstance(value, dict):
        return False
    for key, count in value.items():
        if not isinstance(key, str) or _verdict(key) != key or not _nn(count):
            return False
    return True


def _scope_is(scope, expected: str) -> bool:
    return isinstance(scope, str) and len(scope) <= _LOOSE and scope == expected


def _joined(scope, prefix: str, parts: list[set[str]]) -> bool:
    if not isinstance(scope, str) or len(scope) > _LOOSE:
        return False
    choices: list[tuple[str, ...]] = [()]
    for part in parts:
        choices = [acc + (piece,) for acc in choices for piece in part]
    for pieces in choices:
        if scope == prefix + ":".join(pieces):
            return True
    return False


def _closed_str(value, allowed) -> bool:
    """True when value is a str that belongs to allowed. Never hashes a non-str."""
    return isinstance(value, str) and value in allowed


def _tom_flag(value) -> bool:
    """BOUND summary.tom is an actual bool or null — not 0/1/0.0."""
    return value is True or value is False or value is None


def _summary_scope(verb: str, stage: str, scope, summary: dict) -> str | None:
    """Return the scope to keep when the whole summary matches one emitted shape, else None."""
    if not isinstance(summary, dict):
        return None
    try:
        keys = set(summary)
    except TypeError:
        return None
    if stage == "PARSED" and verb == "validate" and keys == {"errors", "warnings", "outcome"}:
        if (_nn(summary.get("errors")) and _nn(summary.get("warnings"))
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _verdict(summary["outcome"]) == summary["outcome"]):
            return "validate" if _scope_is(scope, "validate") else None
    if stage == "BOUND" and verb == "push" and keys == {"result", "errors", "tom", "validation", "outcome"}:
        validation = summary.get("validation")
        ok_val = (isinstance(validation, dict)
                  and set(validation) == {"tom", "report_schema"}
                  and _closed_str(validation.get("tom"), _VALIDATION)
                  and _closed_str(validation.get("report_schema"), _VALIDATION))
        if (ok_val and _closed_str(summary.get("result"), {_verdict(summary.get("result"))})
                and _verdict(summary["result"]) == summary["result"] and _nn(summary.get("errors"))
                and _tom_flag(summary.get("tom"))
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _scope_is(scope, "push")):
            return "push"
    if stage == "LOADED" and keys == {"action", "ready", "outcome"}:
        action = summary.get("action")
        if (_closed_str(action, {"open", "reload"}) and _bool(summary.get("ready"))
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})):
            if action == "open" and verb == "desktop open" and _scope_is(scope, "desktop:open"):
                return "desktop:open"
            if action == "reload" and verb in ("desktop reload", "push") and _scope_is(scope, "desktop:reload"):
                return "desktop:reload"
    if (stage == "EXECUTED" and keys == {"outcome", "status", "executed"}
            and summary.get("executed") is False
            and _closed_str(summary.get("status"), {_verdict(summary.get("status"))})
            and _verdict(summary["status"]) == summary["status"]
            and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
            and _verdict(summary["outcome"]) == summary["outcome"]
            and verb in ("health", "check", "snapshot compare", "snapshot save", "smoke")):
        expected = verb.replace(" ", ":", 1)
        return expected if _scope_is(scope, expected) else None
    if stage == "EXECUTED" and verb == "health" and keys == {"health_errors", "unprocessed_tables", "outcome", "executed"}:
        if (_nn(summary.get("health_errors")) and _nn(summary.get("unprocessed_tables"))
                and _bool(summary.get("executed"))
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _scope_is(scope, "health")):
            return "health"
    if stage == "EXECUTED" and verb == "check" and keys == {"checks", "ids", "outcome", "executed"}:
        ids = summary.get("ids")
        ids_ok = isinstance(ids, list) and len(ids) <= 50 and all(item is None or _loose(item) for item in ids)
        if (_count_map(summary.get("checks")) and ids_ok and _bool(summary.get("executed"))
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _scope_is(scope, "check")):
            return "check"
    if stage == "EXECUTED" and verb == "check" and keys == {"id", "status", "outcome", "executed"}:
        pieces = _components(summary.get("id"))
        if (pieces is not None and _closed_str(summary.get("status"), {_verdict(summary.get("status"))})
                and _verdict(summary["status"]) == summary["status"] and _bool(summary.get("executed"))
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _joined(scope, "check:", [pieces])):
            return scope
    if stage == "EXECUTED" and verb == "snapshot compare" and keys == {"snapshot", "queries", "outcome", "executed"}:
        if (_components(summary.get("snapshot")) is not None and _count_map(summary.get("queries"))
                and _bool(summary.get("executed"))
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _scope_is(scope, "snapshot:compare")):
            return "snapshot:compare"
    if stage == "EXECUTED" and verb == "snapshot compare" and keys == {"snapshot", "id", "result", "outcome", "executed"}:
        snaps = _components(summary.get("snapshot"))
        ids = _components(summary.get("id"))
        if (snaps is not None and ids is not None
                and _closed_str(summary.get("result"), {_verdict(summary.get("result"))})
                and _verdict(summary["result"]) == summary["result"]
                and _bool(summary.get("executed"))
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _joined(scope, "snapshot:", [snaps, ids])):
            return scope
    if stage == "EXECUTED" and verb == "smoke" and keys == {"smoke", "partial_filters", "outcome", "executed"}:
        if (_count_map(summary.get("smoke")) and _bool(summary.get("partial_filters"))
                and _bool(summary.get("executed"))
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _scope_is(scope, "smoke")):
            return "smoke"
    if (stage == "EXECUTED" and verb == "smoke"
            and keys == {"page", "smoke", "partial_filters", "coverage", "outcome", "executed"}):
        pages = _components(summary.get("page"))
        if (pages is not None and _count_map(summary.get("smoke")) and _bool(summary.get("partial_filters"))
                and summary.get("coverage") == "QUERY_ONLY" and _bool(summary.get("executed"))
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _joined(scope, "smoke:", [pages])):
            return scope
    if stage == "SYNTHETIC" and verb == "synth-test run" and keys == {"test", "result", "outcome"}:
        names = _components(summary.get("test"), synth=True)
        if (names is not None and _closed_str(summary.get("result"), _SYNTH_RESULTS)
                and _verdict(summary.get("outcome")) == summary.get("outcome")
                and _closed_str(summary.get("outcome"), {_verdict(summary.get("outcome"))})
                and _joined(scope, "synth-test:", [names])):
            return scope
    if stage == "HANDOFF" and verb == "handoff" and keys == {"file", "outcome"}:
        if (summary.get("file") == "HANDOFF.md" and summary.get("outcome") == "GENERATED"
                and _scope_is(scope, "handoff")):
            return "handoff"
    if (stage == "HUMAN_ACCEPTED" and verb == "review --accept" and keys == {"accepted"}
            and summary.get("accepted") is True):
        if scope is None or _scope_is(scope, "review:--accept"):
            return "review:--accept"
    if (stage == "STATIC_CONTRACT" and verb == "interactions"
            and keys == {"ids", "counts", "spec_digest", "outcome", "executed", "native_executed"}):
        ids = summary.get("ids")
        digest = summary.get("spec_digest")
        ids_ok = isinstance(ids, list) and len(ids) <= 50 and all(item is None or _loose(item) for item in ids)
        if (ids_ok and _count_map(summary.get("counts"))
                and isinstance(digest, str) and re.fullmatch(r"s[0-9a-f]{12}", digest)
                and summary.get("outcome") == "INCOMPLETE"
                and summary.get("executed") is False and summary.get("native_executed") is False
                and _scope_is(scope, "interactions")):
            return "interactions"
    return None


def _validate_record(obj: dict, project_name: str) -> dict | None:
    """Whole-record closed shape. Unknown fields are not copied. None means skip."""
    if not isinstance(obj, dict):
        return None
    mac, prev = obj.get("chain_mac"), obj.get("chain_prev")
    if not (isinstance(mac, str) and _CHAIN_RE.fullmatch(mac)
            and isinstance(prev, str) and _CHAIN_RE.fullmatch(prev)):
        return None
    obj = {k: v for k, v in obj.items() if k not in ("chain_mac", "chain_prev")}
    try:
        extras = set(obj) - _TOP
    except TypeError:
        return None
    if extras or any(key not in obj for key in _TOP - {"scope"}):
        return None
    verb, stage = obj.get("verb"), obj.get("stage")
    if (obj.get("project") != project_name or not isinstance(verb, str) or verb not in _VERBS
            or not isinstance(stage, str) or stage not in STAGES):
        return None
    ts = _canonical_ts(obj.get("ts"))
    if ts is None or not _MREV.fullmatch(str(obj.get("mirror_rev", ""))) or not _RREV.fullmatch(str(obj.get("real_rev", ""))):
        return None
    summary = obj.get("summary")
    if not isinstance(summary, dict):
        return None
    scope = _summary_scope(verb, stage, obj.get("scope"), summary)
    if scope is None:
        return None
    return {"ts": ts, "project": project_name, "verb": verb, "mirror_rev": obj["mirror_rev"],
            "real_rev": obj["real_rev"], "stage": stage, "scope": scope, "summary": summary}


def _load_entries(ws, project) -> tuple[list[dict], int]:
    files = _log_files(ws)
    if not files:
        return [], 0
    entries: list[dict] = []
    skipped = 0
    key = _chain_key(ws)
    prev = _anchor(ws)
    broken = False
    # JSONL delimiter is LF only — do not use splitlines() (it splits on U+2028/U+2029).
    for path in files:
        text = _read_log(ws, path)
        if text is None:
            continue
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                broken = True
                skipped += 1
                continue
            if not isinstance(obj, dict) or "chain_mac" not in obj:
                broken = True
                skipped += 1
                continue
            linked = False
            if not broken and "chain_mac" in obj:
                if _chain_ok(key, prev, obj):
                    prev = obj["chain_mac"]
                    linked = True
                else:
                    broken = True
            other = obj.get("project")
            if not isinstance(other, str):
                skipped += 1
                continue
            if other != project.name:
                continue
            # A line verify_chain would skip or reject does not count, even if the shape is valid.
            if not linked:
                skipped += 1
                continue
            try:
                clean = _validate_record(obj, project.name)
            except (TypeError, ValueError, RecursionError):
                skipped += 1
                continue
            if clean is None:
                skipped += 1
                continue
            entries.append(clean)
    return entries, skipped


# ---------------------------------------------------------------- recording

def _verdict(value) -> str:
    value = str(value).upper()
    allowed = {"PASS", "FAIL", "ERROR", "EMPTY", "UNSUPPORTED", "BUDGET", "SAME", "DIFF", "NEW", "MISSING",
               "STALE_DATA", "OK", "N/A", "PUSHED", "DRY_RUN_OK", "INVALID", "RELOADED", "RELOAD_FAILED",
               "ENGINE_UNAVAILABLE", "DESKTOP_DIRTY", "DESKTOP_STATE_UNKNOWN", "NOT_READY", "TIMEOUT",
               "INCOMPLETE", "BLOCKED", "GENERATED", "UNKNOWN", "PROTOCOL_ERROR",
               "MATCH", "MISMATCH", "DEFAULT_UNRESOLVED", "MISSING_ID", "RUNTIME_ONLY", "LEGACY",
               "UNKNOWN_TYPE", "REFUSED", "STATIC_ONLY"}
    if value in allowed:
        return value
    # Engine error classes are closed-vocabulary, never arbitrary diagnostic text.
    if value.startswith("ERROR:") and value[6:] in {"SYNTAX", "UNKNOWN_OBJECT", "TYPE_MISMATCH", "CONVERSION",
            "CIRCULAR", "DUPLICATE_KEY", "TIMEOUT", "ENGINE_UNAVAILABLE", "BAD_REQUEST", "OTHER"}:
        return value
    return "UNKNOWN"


def _counts(items, key="status") -> dict:
    out: dict = {}
    for it in items or []:
        if isinstance(it, dict):
            v = _verdict(it.get(key, "UNKNOWN"))
            out[v] = out.get(v, 0) + 1
    return out


def _outcome(statuses: list[str], empty: str = "INCOMPLETE") -> str:
    statuses = [_verdict(x) for x in statuses]
    if not statuses:
        return empty
    if any(x in ("FAIL", "ERROR", "PROTOCOL_ERROR", "INVALID", "DIFF", "RELOAD_FAILED") or x.startswith("ERROR:") for x in statuses):
        return "FAIL"
    if any(x in ("BUDGET", "ENGINE_UNAVAILABLE", "DESKTOP_DIRTY", "DESKTOP_STATE_UNKNOWN") for x in statuses):
        return "BLOCKED"
    if any(x not in ("PASS", "OK", "SAME", "RELOADED", "N/A") for x in statuses):
        return "INCOMPLETE"
    return "PASS"


def _smoke_partial(rows) -> bool:
    return any(isinstance(r, dict) and (r.get("coverage") == "PARTIAL" or bool(r.get("note"))) for r in rows)


def _smoke_counts(summary, visuals) -> dict:
    """Flatten executed / not_executed buckets, or a legacy status->count map, into verdict counts."""
    if isinstance(summary, dict) and {"executed", "not_executed"} & set(summary):
        counts: dict = {}
        for part in ("executed", "not_executed"):
            bucket = summary.get(part)
            if isinstance(bucket, dict):
                for k, v in bucket.items():
                    if isinstance(v, int) and not isinstance(v, bool):
                        key = _verdict(k)
                        counts[key] = counts.get(key, 0) + v
        return counts
    if isinstance(summary, dict) and summary and all(isinstance(v, int) and not isinstance(v, bool) for v in summary.values()):
        return {_verdict(k): v for k, v in summary.items()}
    return _counts(visuals)


def _reload_ready(value) -> bool:
    if isinstance(value, str):
        return value == "RELOADED"
    return isinstance(value, dict) and (value.get("ok") is True or value.get("result") == "RELOADED")


def summarize(verb: str, action: str | None, out: dict) -> tuple[str, dict] | None:
    """Summaries contain only outcomes, object/check identifiers and coarse counts."""
    if not isinstance(out, dict):
        return None
    # `pbz check --validate-only` proves nothing about the model; do not append a line.
    if verb == "check" and (action in ("validate", "validate-only") or str(out.get("verb") or "") == "check validate"):
        return None
    blocked = out.get("status") or out.get("error") or out.get("class")
    if verb == "validate":
        f = out.get("findings") or []
        errors = sum(1 for x in f if x.get("severity") == "error")
        return "PARSED", {"errors": errors, "warnings": sum(1 for x in f if x.get("severity") == "warning"),
                          "outcome": "FAIL" if errors or out.get("ok") is False else "PASS"}
    if verb == "push" and out.get("result") in ("PUSHED", "DRY_RUN_OK", "INVALID"):
        f = out.get("findings") or []
        errors = sum(1 for x in f if x.get("severity") == "error")
        codes = {str(x.get("code", "")) for x in f}
        tom_failed = any(str(x.get("code", "")).startswith(("TOM_", "TMDL_")) and
                         x.get("severity") == "error" for x in f)
        tom = "FAIL" if tom_failed else "PASS" if "TOM_PASSED" in codes else "NOT_RUN"
        schema = "PASS" if "REPORT_SCHEMA_PASSED" in codes else "FAIL" if any(
            x.get("code") == "PBIR" and x.get("severity") == "error" for x in f) else "NOT_RUN"
        return "BOUND", {"result": out["result"], "errors": errors,
                         "tom": True if tom == "PASS" else False if tom == "FAIL" else None,
                         "validation": {"tom": tom, "report_schema": schema},
                         "outcome": "FAIL" if errors or out["result"] == "INVALID" else
                                    "PASS" if tom == schema == "PASS" else "INCOMPLETE"}
    if verb == "desktop" and action in ("open", "reload"):
        ready = (_reload_ready(out.get("reload")) if action == "reload" else out.get("engine") == "READY")
        return "LOADED", {"action": action, "ready": ready,
                          "outcome": "PASS" if ready else _outcome([str(blocked or out.get("result") or "NOT_READY")])}
    if verb in ("health", "check", "snapshot", "smoke") and blocked in ("BUDGET", "ENGINE_UNAVAILABLE", "UNSUPPORTED"):
        return "EXECUTED", {"outcome": _outcome([blocked]), "status": blocked, "executed": False}
    if out.get("ok") is False:
        return None
    if verb == "health":
        bad = out.get("errors") or out.get("objects_in_error") or []
        unprocessed = out.get("unprocessed_tables") or []
        count = len(bad) if isinstance(bad, list) else bad
        return "EXECUTED", {"health_errors": count, "unprocessed_tables": len(unprocessed),
                            "outcome": "FAIL" if count else "INCOMPLETE" if unprocessed else "PASS", "executed": True}
    if verb == "check":
        res = out.get("results") or []
        return "EXECUTED", {"checks": _counts(res), "ids": [r.get("id") for r in res if isinstance(r, dict)][:50],
                            "outcome": _outcome([r.get("status", "UNKNOWN") for r in res]), "executed": bool(res)}
    if verb == "snapshot" and action == "compare":
        res = out.get("queries") or []
        return "EXECUTED", {"snapshot": out.get("name"), "queries": _counts(res, "result"),
                            "outcome": _outcome([r.get("result", "UNKNOWN") for r in res]), "executed": bool(res)}
    if verb == "smoke":
        counts = _smoke_counts(out.get("summary"), out.get("visuals"))
        rows = out.get("visuals") or []
        base = _outcome(list(counts))
        # Partial coverage must not aggregate to PASS even when every executed status is OK.
        if base == "PASS" and _smoke_partial(rows):
            base = "INCOMPLETE"
        return "EXECUTED", {"smoke": counts, "partial_filters": _smoke_partial(rows),
                            "outcome": base, "executed": bool(counts)}
    if verb == "synth-test" and action == "run":
        return "SYNTHETIC", {"test": out.get("name"), "result": out.get("result"),
                             "outcome": _outcome([str(out.get("result", "UNKNOWN"))])}
    if verb == "handoff" and out.get("file") and out.get("result") != "NOTHING_NEW":
        return "HANDOFF", {"file": "HANDOFF.md", "outcome": "GENERATED"}
    if verb == "interactions" and out.get("stage") == "STATIC_CONTRACT":
        res = [r for r in (out.get("results") or []) if isinstance(r, dict)]
        counts: dict = {}
        for row in res:
            status = row.get("status")
            if status in _STATIC_RESULTS:
                counts[status] = counts.get(status, 0) + 1
        digest = out.get("spec_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"s[0-9a-f]{12}", digest):
            return None
        return "STATIC_CONTRACT", {
            "ids": [r.get("id") for r in res][:50],
            "counts": counts,
            "spec_digest": digest,
            "outcome": "INCOMPLETE",
            "executed": False,
            "native_executed": False,
        }
    return None


def _scoped_summaries(verb: str, action: str | None, out: dict) -> list[tuple[str, str, dict]]:
    base = summarize(verb, action, out)
    if base is None:
        return []
    stage, summary = base
    if verb == "check" and out.get("results"):
        return [(stage, "check:" + str(r.get("id", "?")),
                 {"id": r.get("id"), "status": _verdict(r.get("status", "UNKNOWN")),
                  "outcome": _outcome([r.get("status", "UNKNOWN")]),
                  "executed": r.get("status") not in ("BUDGET", "UNSUPPORTED")})
                for r in out["results"] if isinstance(r, dict)]
    if verb == "snapshot" and out.get("queries"):
        return [(stage, "snapshot:" + str(out.get("name", "?")) + ":" + str(r.get("id", "?")),
                 {"snapshot": out.get("name"), "id": r.get("id"), "result": _verdict(r.get("result", "UNKNOWN")),
                  "outcome": _outcome([r.get("result", "UNKNOWN")]), "executed": True})
                for r in out["queries"] if isinstance(r, dict)]
    if verb == "smoke" and out.get("visuals"):
        pages: dict[str, list] = {}
        for r in out["visuals"]:
            pages.setdefault(str(r.get("page", "?")), []).append(r)
        return [(stage, "smoke:" + page,
                 {"page": page, "smoke": _counts(rows), "partial_filters": _smoke_partial(rows),
                  "coverage": "QUERY_ONLY", "outcome": (
                      "INCOMPLETE" if _smoke_partial(rows) and
                      _outcome([r.get("status", "UNKNOWN") for r in rows]) == "PASS" else
                      _outcome([r.get("status", "UNKNOWN") for r in rows])), "executed": True})
                for page, rows in pages.items()]
    if verb == "synth-test" and action == "run":
        # One scope per golden name so two tests on the same revision do not collapse together.
        return [(stage, "synth-test:" + str(out.get("name") or "?"), summary)]
    events = [(stage, verb + (":" + action if action else ""), summary)]
    if verb == "push" and out.get("result") == "PUSHED" and "reload" in out:
        ready = _reload_ready(out["reload"])
        events.append(("LOADED", "desktop:reload", {"action": "reload", "ready": ready,
                      "outcome": "PASS" if ready else _outcome([str(out["reload"])])}))
    return events


def record(ws, project, verb: str, action: str | None, out: dict) -> None:
    events = _scoped_summaries(verb, action, out)
    if not events or project is None:
        return
    try:
        shared = {"ts": dt.datetime.now().replace(microsecond=0).isoformat(), "project": project.name,
                  "verb": verb + (f" {action}" if action else ""), "mirror_rev": mirror_rev(ws, project),
                  "real_rev": real_rev(ws, project)}
    except (OSError, ValueError, PbzError):
        return
    lines = []
    for stage, scope, summary in events:
        lines.append({**shared, "stage": stage, "scope": scope, "summary": summary})
    _append_lines(ws, lines)


# ---------------------------------------------------------------- release classes

def classify(rec: dict, side: str) -> str:
    if side == "report":
        return "REPORT_ONLY"
    kind = rec.get("kind", "")
    if kind in ("role", "tablePermission", "columnPermission", "member") or "role" in str(rec.get("file", "")).lower():
        return "SECURITY_REVIEW"
    if kind in ("partition", "expression", "refreshPolicy", "dataSource", "parameter"):
        return "REFRESH_REQUIRED"
    parts = {p.get("part", "") for p in rec.get("expressions") or []}
    props = {p.get("prop", "").rsplit(".", 1)[-1] for p in rec.get("props") or []}
    if any("partition" in p or p.endswith("source") for p in parts) or "sourceColumn" in props or "refreshPolicy" in props:
        return "REFRESH_REQUIRED"
    if kind == "table" and rec.get("status") in ("added", "removed"):
        return "REFRESH_REQUIRED"
    if kind == "column" and rec.get("status") in ("added", "removed") and not parts:
        return "REFRESH_REQUIRED"
    if kind == "relationship" or (kind in ("column", "table") and parts) or kind in ("calculationGroup", "calculationItem"):
        return "RECALC_REQUIRED"
    if kind in ("measure", "function") and (parts or rec.get("status") in ("added", "removed", "renamed", "moved")):
        return "LOGIC"
    if props or rec.get("description") or rec.get("annotations") or rec.get("status") in ("renamed", "moved"):
        return "METADATA_ONLY"
    return "UNKNOWN"


NEXT = {
    "SECURITY_REVIEW": "a human reviews role, RLS and OLS changes; test roles in Desktop (View as)",
    "REFRESH_REQUIRED": "after push: refresh in Desktop (the user's Refresh button), then pbz health",
    "RECALC_REQUIRED": "after push: pbz desktop open, pbz refresh (recalculates), pbz health",
    "LOGIC": "pbz check (compiles, then equal / additive checks), pbz snapshot compare, pbz synth test run",
    "METADATA_ONLY": "pbz validate and push are enough",
    "REPORT_ONLY": "pbz push --reload, pbz smoke --page <page>, then the user's visual review (pbz handoff)",
    "UNKNOWN": "inspect the change by hand",
}


# ---------------------------------------------------------------- review

def _status(e: dict | None, cur_m: str, cur_r: str, pending: bool, stage: str) -> str:
    if e is None:
        return "NOT_RUN"
    if e.get("mirror_rev") != cur_m:
        return "STALE"
    if stage == "PARSED":
        return "CURRENT"
    if e.get("real_rev") != cur_r:
        return "STALE"
    if stage in ("LOADED", "EXECUTED", "HANDOFF", "HUMAN_ACCEPTED") and pending:
        return "STALE (pending edits not pushed)"
    return "CURRENT"


def _state_names(ws, dirname: str) -> tuple[str, list[str]]:
    """Anchored directory listing. A link directory is UNSAFE_PATH. Link or hardlinked leaves are omitted, unopened."""
    path = pbz_state_path(ws, dirname)
    if not os.path.lexists(path):
        return path, []
    if _reparse_link(path) or not os.path.isdir(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    names = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if _reparse_link(full):
            continue
        try:
            info = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            names.append(name)
    return path, names


def _suggested_tests(ws, project, logic: list[str]) -> list[str]:
    out = []
    if logic:
        out.append("pbz check with a compiles check per changed measure: " + ", ".join(logic[:10]))
    _snap, snap_names = _state_names(ws, "snapshots")
    for name in snap_names:
        if name.endswith(".json"):
            out.append(f"pbz snapshot compare {name[:-5]}")
    tdir, test_names = _state_names(ws, "synth-tests")
    names = {n.lower().strip("[]") for n in logic}
    for name in test_names:
        if not name.endswith(".json"):
            continue
        try:
            text = read_checked(os.path.join(tdir, name), root=_workspace_root(ws)).decode("utf-8").lower()
        except (OSError, UnicodeError, PbzError):
            continue
        if not names or any(f"[{n}]" in text for n in names):
            out.append(f"pbz synth test run {name[:-5]}")
    return out


def static_proof_ok(entries: list[dict], *, mirror_rev: str, spec_digest: str) -> bool:
    """True only for a successful static interaction contract on this revision and digest.

    A record of mismatches, unsupported native runs, or missing binding metadata is not proof.
    The result never counts as runtime execution or HUMAN_ACCEPTED.
    """
    if not isinstance(spec_digest, str) or not re.fullmatch(r"s[0-9a-f]{12}", spec_digest):
        return False
    if not isinstance(mirror_rev, str) or not _MREV.fullmatch(mirror_rev):
        return False
    for entry in entries:
        if entry.get("stage") != "STATIC_CONTRACT":
            continue
        if entry.get("stage") == "HUMAN_ACCEPTED":
            return False
        scope = entry.get("scope") or ""
        if not str(scope).startswith("interactions"):
            continue
        if entry.get("mirror_rev") != mirror_rev:
            continue
        summary = entry.get("summary") if isinstance(entry.get("summary"), dict) else None
        if not summary:
            continue
        if summary.get("spec_digest") != spec_digest:
            continue
        if summary.get("outcome") != "INCOMPLETE":
            return False
        if summary.get("executed") is not False or summary.get("native_executed") is not False:
            return False
        counts = summary.get("counts")
        if not isinstance(counts, dict) or not counts:
            return False
        if any(key not in _STATIC_RESULTS for key in counts):
            return False
        failing = counts.keys() - {"MATCH"}
        if failing or not counts.get("MATCH"):
            return False
        return True
    return False


def consume_static(entries: list[dict], *, mirror_rev: str, spec_digest: str | None) -> dict:
    """Evidence-consumption view of a static interaction record.

    Satisfaction is static-only. It cannot fill a runtime requirement or HUMAN_ACCEPTED.
    """
    ok = bool(spec_digest) and static_proof_ok(entries, mirror_rev=mirror_rev, spec_digest=spec_digest or "")
    return {"static_contract": ok, "runtime_satisfied": False, "human_accepted": False}


def review(ws, project, accept: bool = False, current_spec_digest: str | None = None) -> dict:
    from . import changes, consumers
    cur_m, cur_r = mirror_rev(ws, project), real_rev(ws, project)
    if accept:
        from .cli import _is_human
        if not _is_human():
            raise PbzError("HUMAN_ONLY", "acceptance is the reviewer's: run `pbz review --accept` yourself in a terminal")
    entries, skipped = _load_entries(ws, project)
    try:
        ch = changes.run(ws, project, "last", "mirror")
    except PbzError:
        ch = {"result": "NO_HISTORY"}
    model, report = ch.get("model") or [], ch.get("report") or []
    pending = ch.get("result") == "CHANGED"
    rows, by_class = [], {}
    for side, recs in (("model", model), ("report", report)):
        for rec in recs:
            cls = classify(rec, side)
            label = rec.get("name") or rec.get("id") or rec.get("kind")
            if side == "model" and rec.get("table") and rec.get("kind") in ("column", "measure", "hierarchy"):
                label = f"{rec['table']}[{rec['name']}]" if rec["kind"] == "column" else (
                    f"[{rec['name']}]" if rec["kind"] == "measure" else f"{rec['table']}.{rec['name']}")
            row = {"class": cls, "side": side, "kind": rec.get("kind"), "object": label, "status": rec.get("status")}
            if rec.get("expressions"):
                row["parts"] = [f"{p.get('part')}: {p.get('class')}" for p in rec["expressions"]][:6]
            if rec.get("props"):
                row["props"] = [p.get("prop") for p in rec["props"]][:8]
            if rec.get("impact"):
                row["impact"] = rec["impact"]
            if rec.get("page"):
                row["page"] = rec["page"]
            rows.append(row)
            by_class[cls] = by_class.get(cls, 0) + 1
    rows.sort(key=lambda r: (CLASS_ORDER.index(r["class"]), r["side"], str(r["object"]).lower()))
    logic = [r["object"] for r in rows if r["class"] == "LOGIC" and r["kind"] == "measure"]
    # Path checks before acceptance, handoff, or the packet. Render stays on the entries already validated.
    suggested = _suggested_tests(ws, project, logic)
    if accept:
        accepted = {"ts": dt.datetime.now().replace(microsecond=0).isoformat(), "project": project.name,
                    "stage": "HUMAN_ACCEPTED", "verb": "review --accept", "mirror_rev": cur_m, "real_rev": cur_r,
                    "summary": {"accepted": True}}
        batch = [accepted]
        _append_lines(ws, batch)
        # _stamp replaces the list element. The replacement is what was written.
        stamped = batch[0]
        mac = stamped.get("chain_mac") if isinstance(stamped, dict) else None
        if isinstance(mac, str) and mac in _accepted_macs(ws):
            from . import handoff
            handoff.accept_generated(ws, project, cur_m, cur_r)
            clean = _validate_record(stamped, project.name)
            if clean is not None:
                entries.append(clean)
    latest: dict = {}
    for e in entries:
        # Latest validated record wins per stage+scope. Superseded outcomes are dropped.
        latest[(e["stage"], e.get("scope") or e.get("verb", e["stage"]))] = e
    evidence = []
    for stage in STAGES:
        scoped = [e for (st, _scope), e in latest.items() if st == stage]
        items = [{"scope": e.get("scope") or e.get("verb"),
                  "status": _status(e, cur_m, cur_r, pending, stage), "verb": e["verb"],
                  "when": e["ts"], "summary": e.get("summary", {})} for e in scoped]
        statuses = {i["status"] for i in items}
        freshness = ("NOT_RUN" if not items else "CURRENT" if statuses == {"CURRENT"} else
                     "PARTIAL" if "CURRENT" in statuses else "STALE")
        newest = scoped[-1] if scoped else None
        current = [i for i in items if i["status"] == "CURRENT"]
        summary = (dict(items[0]["summary"]) if len(items) == 1 else
                   {"scopes": len(items), "current_scopes": len(current),
                    "outcomes": _counts([i["summary"] for i in current], "outcome")})
        evidence.append({"stage": stage, "status": freshness, "items": items,
                         **({"verb": newest["verb"], "when": newest["ts"], "summary": summary} if newest else {})})
    try:
        reg = consumers.load_registry(ws)["consumers"]
    except (OSError, ValueError):
        reg = []
    # The stored digest is not the current spec. Callers pass the digest of the spec they just loaded.
    static_view = consume_static(entries, mirror_rev=cur_m, spec_digest=current_spec_digest)
    coverage = {"external_consumers": "registered" if reg else "none registered",
                "human_visual_review": next((x["status"] for x in evidence if x["stage"] == "HUMAN_ACCEPTED"), "NOT_RUN"),
                "static_contract": static_view["static_contract"],
                "runtime_from_static": static_view["runtime_satisfied"],
                "human_from_static": static_view["human_accepted"]}
    out = {"verb": "review", "project": project.name, "revision": {"mirror": cur_m, "real": cur_r},
           "pending_edits": pending, "classes": {c: by_class[c] for c in CLASS_ORDER if c in by_class},
           "changes": rows[:300], "evidence": evidence, "coverage": coverage,
           "next": [f"{c}: {NEXT[c]}" for c in CLASS_ORDER if c in by_class],
           "suggested_tests": suggested}
    if skipped:
        out["result"] = "INCOMPLETE"
        out["skipped"] = skipped
    from . import handoff
    out["packet"] = handoff.write_packet(ws, "REVIEW.md", _markdown(out, ws, project, rows), "reviews")
    out["file"] = "pbz-workspace/REVIEW.md"
    return out


def _markdown(out: dict, ws, project, rows: list[dict]) -> str:
    L = [f"# Review packet: {project.name}", "",
         f"Revision: mirror `{out['revision']['mirror']}`, real files `{out['revision']['real']}`. "
         f"Pending edits: {'yes' if out['pending_edits'] else 'no'}.", ""]
    if rows:
        L += ["## Changes by release class", "", "| Class | Kind | Object | Change | Impact |", "|---|---|---|---|---|"]
        for r in rows:
            detail = "; ".join((r.get("parts") or []) + [f"prop {p}" for p in r.get("props") or []]) or r.get("status", "")
            imp = r.get("impact") or {}
            impact = (f"{imp.get('objects_total', 0)} dependents, {imp.get('visuals', 0)} visuals" if imp else "")
            L.append(f"| {r['class']} | {r['kind']} | {r['object']} | {r['status']}: {detail} | {impact} |")
        L += ["", "## Next steps", ""] + [f"- {n}" for n in out["next"]] + [""]
    else:
        L += ["No pending edits since the last pull or push.", ""]
    if out["suggested_tests"]:
        L += ["## Suggested verification", ""] + [f"- `{t}`" if t.startswith("pbz ") else f"- {t}" for t in out["suggested_tests"]] + [""]
    L += ["## Evidence", "", "| Stage | Status | Verb | When | Summary |", "|---|---|---|---|---|"]
    for e in out["evidence"]:
        L.append(f"| {e['stage']} | {e['status']} | {e.get('verb', '')} | {e.get('when', '')} | "
                 f"{json.dumps(e.get('summary'), ensure_ascii=False) if e.get('summary') else ''} |")
    L += ["", "### Evidence by scope", "", "| Stage | Scope | Freshness | Outcome |", "|---|---|---|---|"]
    for e in out["evidence"]:
        for item in e.get("items", []):
            L.append(f"| {e['stage']} | {item['scope']} | {item['status']} | {item['summary'].get('outcome', 'UNKNOWN')} |")
    L += ["", "## Coverage", "", f"- External consumers: {out['coverage']['external_consumers']}",
          f"- Human visual review: {out['coverage']['human_visual_review']} (`pbz review --accept` in a terminal)", ""]
    pages = sorted({r.get("page") for r in rows if r.get("side") == "report" and r.get("page")}
                   | {str(r["object"]) for r in rows if r.get("side") == "report" and r.get("kind") == "page"
                      and r.get("status") != "removed"})
    if pages:
        from . import layout
        L += ["## Changed pages (layout, no data)", ""]
        for pg in pages[:5]:
            try:
                lay = layout.run(ws, project, pg, show_map=True)
            except PbzError:
                continue
            for p in lay.get("pages", []):
                if p.get("map"):
                    L += [f"### {p['page']}", "", "```"] + p["map"] + ["```", ""]
    L.append("Real-data evidence above is verdicts only (counts and ids); values never enter this packet.")
    return "\n".join(L) + "\n"
