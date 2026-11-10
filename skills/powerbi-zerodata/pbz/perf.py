"""Performance bands from pbz-engine.

Durations stay in the engine. This module charges the probe budget and keeps the same 1.25
ratio the engine uses when it decides FASTER, SAME, SLOWER or NOISY. Python never sees milliseconds.
"""
from __future__ import annotations

import re

from . import checks, desktop, policy as policy_mod
from .common import PbzError, engine_call

RATIO = 1.25
DURATION = ("LT_100MS", "100_500MS", "500MS_2S", "2_10S", "GT_10S")
SHARE = ("LOW", "MID", "HIGH")
COUNT = ("0", "1_5", "6_20", "21_100", "GT_100")
PLAN = ("0", "1", "2_5", "6_20", "GT_20")
CACHE = ("NONE", "SOME", "MOST")
REWRITE = ("HIT", "MISS", "NONE")
VERDICT = ("FASTER", "SAME", "SLOWER", "NOISY")


def verdict(old: float, new: float, spread: float) -> str:
    """Same rule as engine/Perf.cs. Used by tests; live runs trust the engine's verdict."""
    if old <= 0:
        return "NOISY"
    ratio = new / old
    delta = abs(new - old)
    if ratio > RATIO or ratio < 1 / RATIO:
        if delta <= spread:
            return "NOISY"
        return "SLOWER" if ratio > RATIO else "FASTER"
    return "SAME"


def _disclosure(ws) -> str:
    pol = policy_mod.load(ws)
    return "shape" if pol.get("disclosure", "shape") == "shape" else "strict"


def _closed(res: dict, ws=None) -> dict:
    """Closed perf fields. Outside the shape tier, magnitude bands are omitted.

    timing, trace, callback, agg_rewrite, verdict and baseline stay when they are
    already closed enums. NO_BASELINE is not a status this function adds.
    """
    shape = _disclosure(ws) == "shape" if ws is not None else True
    out = {"verb": "perf"}
    if res.get("timing") in ("BANDED", "UNAVAILABLE"):
        out["timing"] = res["timing"]
    if shape and res.get("duration_band") in DURATION:
        out["duration_band"] = res["duration_band"]
    if shape and res.get("fe_share") in SHARE:
        out["fe_share"] = res["fe_share"]
    for key in ("se_queries", "dq_events"):
        if shape and res.get(key) in COUNT:
            out[key] = res[key]
    if shape and res.get("cache_hit") in CACHE:
        out["cache_hit"] = res["cache_hit"]
    if isinstance(res.get("callback"), bool):
        out["callback"] = res["callback"]
    if res.get("agg_rewrite") in REWRITE:
        out["agg_rewrite"] = res["agg_rewrite"]
    if res.get("trace") in ("CAPTURED", "UNAVAILABLE"):
        out["trace"] = res["trace"]
    if res.get("verdict") in VERDICT:
        out["verdict"] = res["verdict"]
    if res.get("baseline") in ("SAVED", "MISSING"):
        out["baseline"] = res["baseline"]
    if shape:
        plan = res.get("plan")
        if isinstance(plan, dict):
            kept = {k: plan[k] for k in ("CallbackDataID", "Spool_Iterator", "CrossApply", "LargeMaterialization")
                    if plan.get(k) in PLAN}
            if kept:
                out["plan"] = kept
    return out


def run(ws, project, query: str, runs: int = 1, baseline: str | None = None, action: str | None = None) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise PbzError("BAD_REQUEST", "a DAX query is required")
    if not 1 <= int(runs) <= 20:
        raise PbzError("BAD_REQUEST", "runs must be from 1 to 20")
    port = desktop.require_port(project)
    if not checks.spend(ws, "probes"):
        return {"verb": "perf", "status": "BUDGET", "note": "daily probe budget reached; the user can reset it"}
    res = engine_call("perf", {"private_dir": ws.private, "port": port, "dax": query, "runs": int(runs),
                               "baseline": baseline, "action": action}, timeout=600)
    if not isinstance(res, dict) or res.get("ok") is not True:
        klass = res.get("class") if isinstance(res, dict) else None
        if klass not in {"SYNTAX", "UNKNOWN_OBJECT", "TYPE_MISMATCH", "CONVERSION", "CIRCULAR", "DUPLICATE_KEY",
                         "TIMEOUT", "ENGINE_UNAVAILABLE", "BAD_REQUEST", "OTHER"}:
            klass = "ENGINE_UNAVAILABLE"
        raise PbzError(klass)
    return _closed(res, ws)


def expect(ws, project, query: str, expect_verdict: str = "NOT_SLOWER") -> dict:
    """Check kind `perf`: not slower than the saved baseline. The query text is not returned."""
    if expect_verdict != "NOT_SLOWER":
        raise PbzError("BAD_REQUEST", "perf expect is NOT_SLOWER")
    result = run(ws, project, query, runs=3, baseline="check", action="compare")
    if result.get("status") == "BUDGET":
        return {"status": "BUDGET", "verdict": "UNAVAILABLE"}
    verdict_ = result.get("verdict")
    status = "PASS" if verdict_ in ("FASTER", "SAME", "NOISY") else "FAIL" if verdict_ == "SLOWER" else "ERROR"
    return {"status": status, "verdict": verdict_ or "UNAVAILABLE"}
