"""Thin wrapper around pbz-engine.exe, the only component allowed to touch a live model.

call(verb, request) sends one JSON request on stdin and returns the engine's single JSON response.
The response is already a closed vocabulary (see engine/CONTRACT.md); this wrapper never adds engine
text of its own. Failures to run the engine collapse to a fixed envelope:

  missing exe / unparseable output -> {"ok": False, "verb": verb, "class": "ENGINE_UNAVAILABLE"}
  wall-clock timeout               -> {"ok": False, "verb": verb, "class": "TIMEOUT"}
  request not JSON-serializable    -> {"ok": False, "verb": verb, "class": "BAD_REQUEST"}

Callers pass private_dir (and port for live verbs) in every request themselves.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ENGINE_EXE = Path(__file__).resolve().parent.parent / "engine" / "bin" / "pbz-engine.exe"

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _fail(verb: str, cls: str) -> dict:
    return {"ok": False, "verb": verb, "class": cls}


# Top-level keys production callers read. Nested values stay with the kept key.
# Owned callers (checks, desktop): ok, class, verb, checks, results, errors, status, tables.
# Other callers, kept so this filter does not break them:
#   cli health: objects
#   evidence health: unprocessed_tables, objects_in_error
#   validate / synth tmdl: missing_refs, orphan_files
#   compiles (daxcatalog, modeling, checks): items
#   memory: model, relationships, other, auto_datetime, coverage, predicates
#   perf: timing, duration_band, fe_share, se_queries, dq_events, cache_hit, callback,
#         agg_rewrite, trace, verdict, baseline, plan
#   synth-query: columns, rows, truncated
#   smoke: visuals
#   freshness: partitions, refresh
#   deps: edges
#   snapshot / probe pass-through: queries, name, table, rows_bucket
_RESPONSE_FIELDS = frozenset({
    "ok", "class", "verb",
    "checks", "results", "errors", "status", "tables", "objects", "unprocessed_tables",
    "objects_in_error", "missing_refs", "orphan_files", "items",
    "model", "relationships", "other", "auto_datetime", "coverage", "predicates",
    "timing", "duration_band", "fe_share", "se_queries", "dq_events", "cache_hit",
    "callback", "agg_rewrite", "trace", "verdict", "baseline", "plan",
    "columns", "rows", "truncated", "visuals", "partitions", "refresh", "edges",
    "queries", "name", "table", "rows_bucket",
})


def _project(verb: str, response: dict) -> dict:
    """ok, class, verb, and the fields callers actually read. Anything else is dropped."""
    out = {key: value for key, value in response.items() if key in _RESPONSE_FIELDS}
    if verb != "synth-query":
        for key in ("columns", "rows", "truncated"):
            out.pop(key, None)
    out["ok"] = response["ok"]
    if not isinstance(out.get("verb"), str):
        out["verb"] = verb
    return out


def call(verb: str, request: dict, timeout: float = 120) -> dict:
    """Run `pbz-engine.exe <verb>` with `request` as JSON on stdin and return the parsed response."""
    exe = Path(ENGINE_EXE)
    if not exe.is_file():
        return _fail(verb, "ENGINE_UNAVAILABLE")
    try:
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return _fail(verb, "BAD_REQUEST")
    try:
        proc = subprocess.run(
            [str(exe), verb],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(exe.parent),
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return _fail(verb, "TIMEOUT")
    except OSError:
        return _fail(verb, "ENGINE_UNAVAILABLE")
    try:
        response = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _fail(verb, "ENGINE_UNAVAILABLE")
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        return _fail(verb, "ENGINE_UNAVAILABLE")
    return _project(verb, response)
