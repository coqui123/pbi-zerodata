"""Publication/refresh provenance and metadata-only comparison eligibility."""
from __future__ import annotations

import datetime as dt
from . import control, desktop
from .common import PbzError, engine_call

CONTRACT = {"metric_id", "grain", "status_scope", "work_type", "date_basis", "definition_sha256"}
ENUMS = {"grain": {"project", "record", "role-month"}, "status_scope": {"open", "active", "all"},
         "work_type": {"Project", "BAU", "Admin", "All"},
         "date_basis": {"observed", "planned", "current", "snapshot"}}


def capture(ws, project, max_age_hours=24):
    if type(max_age_hours) is not int or not 1 <= max_age_hours <= 8760:
        raise PbzError("BAD_REQUEST", "max age must be 1..8760 hours")
    result = engine_call("provenance", {"private_dir": ws.private, "port": desktop.require_port(project),
                                      "max_age_hours": max_age_hours})
    if not result.get("ok"):
        raise PbzError(result.get("class", "ENGINE_UNAVAILABLE"))
    return result


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
