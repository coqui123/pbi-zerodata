"""Publication/refresh provenance and metadata-only comparison eligibility."""
from __future__ import annotations

import datetime as dt
from . import control, desktop
from .common import PbzError, engine_call

CONTRACT = {"metric_id", "grain", "status_scope", "work_type", "date_basis", "definition_sha256"}
ENUMS = {"grain": {"project", "record", "role-month"}, "status_scope": {"open", "active", "all"},
         "work_type": {"Project", "BAU", "Admin", "All"},
         "date_basis": {"observed", "planned", "current", "snapshot"}}


REFRESH = {"FRESH", "STALE", "UNKNOWN"}
COVERAGE = {"COMPLETE", "PARTIAL"}
_ENGINE_CLASS = {"ENGINE_UNAVAILABLE", "TIMEOUT", "NOT_READY", "BAD_REQUEST", "OTHER"}
_NOTE = "Refresh does not certify source publication. Raw timestamps remain in protected storage."


def _aware(instant: dt.datetime) -> dt.datetime:
    if instant.tzinfo is None:
        return instant.replace(tzinfo=dt.timezone.utc)
    return instant.astimezone(dt.timezone.utc)


def classify_partitions(partitions, *, now, max_age_hours) -> dict:
    """Closed labels for partition refresh times. Raw timestamps are not returned.

    Matches the engine rule: age <= max_age_hours is FRESH, older is STALE, missing or future is UNKNOWN.
    No partitions is UNKNOWN / PARTIAL. Source publication is never inferred from a refresh time.
    """
    stale = unknown = any_part = False
    now = _aware(now)
    limit = dt.timedelta(hours=max_age_hours)
    for part in partitions if isinstance(partitions, list) else []:
        any_part = True
        raw = part.get("refreshed") if isinstance(part, dict) else None
        state = "UNKNOWN"
        if isinstance(raw, str):
            try:
                instant = _aware(dt.datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                instant = None
            if instant is not None:
                age = now - instant
                if age >= dt.timedelta(0):
                    state = "FRESH" if age <= limit else "STALE"
        stale = stale or state == "STALE"
        unknown = unknown or state == "UNKNOWN"
    refresh = "STALE" if stale else "UNKNOWN" if unknown or not any_part else "FRESH"
    coverage = "PARTIAL" if unknown or not any_part else "COMPLETE"
    return {"refresh": refresh, "source_publication": "UNKNOWN", "coverage": coverage}


def _closed(refresh, coverage) -> dict:
    if not isinstance(refresh, str) or refresh not in REFRESH:
        refresh, coverage = "UNKNOWN", "PARTIAL"
    if not isinstance(coverage, str) or coverage not in COVERAGE:
        coverage = "PARTIAL"
    return {"ok": True, "verb": "provenance", "refresh": refresh, "source_publication": "UNKNOWN",
            "coverage": coverage, "note": _NOTE}


def capture(ws, project, max_age_hours=24, now=None):
    if type(max_age_hours) is not int or not 1 <= max_age_hours <= 8760:
        raise PbzError("BAD_REQUEST", "max age must be 1..8760 hours")
    result = engine_call("provenance", {"private_dir": ws.private, "port": desktop.require_port(project),
                                      "max_age_hours": max_age_hours})
    if not result.get("ok"):
        klass = result.get("class")
        if not isinstance(klass, str) or klass not in _ENGINE_CLASS:
            raise PbzError("PROTOCOL_ERROR", "engine provenance failure class is not in the closed vocabulary")
        raise PbzError(klass)
    if "partitions" in result:
        labels = classify_partitions(result.get("partitions"), now=now or dt.datetime.now(dt.timezone.utc),
                                     max_age_hours=max_age_hours)
        return _closed(labels["refresh"], labels["coverage"])
    return _closed(result.get("refresh"), result.get("coverage"))


def _instant(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise PbzError("BAD_REQUEST", "cutoffs must be timezone-aware ISO timestamps")
    try:
        instant = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError()
        return instant.astimezone(dt.timezone.utc)
    except ValueError:
        raise PbzError("BAD_REQUEST", "cutoffs must be timezone-aware ISO timestamps") from None


def compare(spec):
    data = control.load(spec)
    control.keys(data, {"version", "left", "right"}, {"left", "right"})
    sides = []
    for side in [data["left"], data["right"]]:
        control.keys(side, {"contract", "publication_cutoff", "refresh_cutoff"}, {"contract"})
        contract = side["contract"]
        control.keys(contract, CONTRACT, CONTRACT)
        control.identifier(contract["metric_id"])
        for key, allowed in ENUMS.items():
            if not isinstance(contract[key], str) or contract[key] not in allowed:
                raise PbzError("BAD_REQUEST", "invalid counting contract category")
        digest = contract["definition_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise PbzError("BAD_REQUEST", "definition fingerprint must be sha256")
        sides.append((contract, _instant(side.get("publication_cutoff")), _instant(side.get("refresh_cutoff"))))
    a, b = sides
    mismatches = sorted(k for k in CONTRACT if a[0][k] != b[0][k])
    missing = [k for i, k in [(1, "publication_cutoff"), (2, "refresh_cutoff")] if a[i] is None or b[i] is None]
    for i, key in [(1, "publication_cutoff"), (2, "refresh_cutoff")]:
        if a[i] is not None and b[i] is not None and a[i] != b[i]:
            mismatches.append(key)
    return {"verb": "freshness compare", "result": "COMPARABLE" if not mismatches and not missing else "INCOMPARABLE",
            "mismatched_dimensions": mismatches, "unknown_dimensions": missing,
            "note": "Eligibility only; no business totals were queried or compared."}
