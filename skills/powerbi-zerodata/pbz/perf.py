"""Performance bands from pbz-engine.

Durations stay in the engine. This module charges the probe budget and keeps the same 1.25
ratio the engine uses when it decides FASTER, SAME, SLOWER or NOISY. Python never sees milliseconds.
"""
from __future__ import annotations

import hashlib
import os

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
    if res.get("ratio_verdict") in ("WITHIN_LIMIT", "OVER_LIMIT"):
        out["ratio_verdict"] = res["ratio_verdict"]
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


def _baseline_store_state(ws) -> dict | None:
    """Content digests of the on-disk perf baseline store, or None when the
    store cannot be observed completely. A missing store directory is a
    complete empty observation; any listing or read failure is not."""
    store = os.path.join(ws.private, "perf")
    try:
        names = os.listdir(store)
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    state = {}
    for name in names:
        if not name.endswith(".bin"):
            continue
        try:
            with open(os.path.join(store, name), "rb") as handle:
                state[name] = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return None
    return state


def _save_confirmed(before: dict | None, after: dict | None) -> bool:
    """A claimed save is confirmed only by a qualifying observation between two
    complete snapshots: a created baseline file, or an existing file whose
    contents were replaced. Deletions and metadata-only changes never qualify,
    and an incomplete observation confirms nothing."""
    if before is None or after is None:
        return False
    return any(name not in before or before[name] != digest for name, digest in after.items())


def run(ws, project, query: str, runs: int = 1, baseline: str | None = None, action: str | None = None) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise PbzError("BAD_REQUEST", "a DAX query is required")
    if not 1 <= int(runs) <= 20:
        raise PbzError("BAD_REQUEST", "runs must be from 1 to 20")
    port = desktop.require_port(project)
    if not checks.spend(ws, "probes"):
        return {"verb": "perf", "status": "BUDGET", "note": "daily probe budget reached; the user can reset it"}
    before = _baseline_store_state(ws) if action == "save" else None
    res = engine_call("perf", {"private_dir": ws.private, "port": port, "dax": query, "runs": int(runs),
                               "baseline": baseline, "action": action,
                               "project_identity": hashlib.sha256(os.path.normcase(os.path.abspath(os.path.join(
                                   ws.root, getattr(project, "name", "") + ".pbip"))).encode("utf-8")).hexdigest()}, timeout=600)
    if not isinstance(res, dict) or res.get("ok") is not True:
        klass = res.get("class") if isinstance(res, dict) else None
        if klass not in {"SYNTAX", "UNKNOWN_OBJECT", "TYPE_MISMATCH", "CONVERSION", "CIRCULAR", "DUPLICATE_KEY",
                         "TIMEOUT", "ENGINE_UNAVAILABLE", "BAD_REQUEST", "OTHER"}:
            klass = "ENGINE_UNAVAILABLE"
        raise PbzError(klass)
    out = _closed(res, ws)
    if action == "save":
        if out.get("baseline") != "SAVED":
            out["status"] = "INCOMPLETE"
            out["note"] = ("baseline not saved: the measurement evidence was incomplete or unusable, "
                           "so nothing was written; the measurement above is informational only")
        elif not _save_confirmed(before, _baseline_store_state(ws)):
            out.pop("baseline", None)
            out["status"] = "INCOMPLETE"
            out["note"] = ("baseline save not confirmed: the engine reported SAVED, but the on-disk check "
                           "could not verify a baseline creation or replacement")
    elif action == "compare" and "verdict" not in out:
        out["status"] = "INCOMPLETE"
        out["note"] = ("comparison not completed: no verdict was produced because the saved baseline is "
                       "missing, invalid, incompatible or unreadable, or the measurement evidence was incomplete")
    return out


def expect(ws, project, query: str, expect_verdict: str = "NOT_SLOWER", check_id: str = "check") -> dict:
    """Check kind `perf`: not slower than the saved baseline. The query text is not returned."""
    if expect_verdict != "NOT_SLOWER":
        raise PbzError("BAD_REQUEST", "perf expect is NOT_SLOWER")
    result = run(ws, project, query, runs=3, baseline=check_id, action="compare")
    if result.get("status") == "BUDGET":
        return {"status": "BUDGET", "verdict": "UNAVAILABLE"}
    verdict_ = result.get("verdict")
    status = ("FAIL" if result.get("ratio_verdict") == "OVER_LIMIT" or verdict_ == "SLOWER" else
              "PASS" if verdict_ in ("FASTER", "SAME", "NOISY") and result.get("ratio_verdict") == "WITHIN_LIMIT" else "ERROR")
    return {"status": status, "verdict": verdict_ or "UNAVAILABLE"}
