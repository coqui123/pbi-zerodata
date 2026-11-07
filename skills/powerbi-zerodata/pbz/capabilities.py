"""`pbz capabilities`: a closed matrix of what pbz can certify. Status words are fixed."""
from __future__ import annotations

import os

from .validate import REPORT_AUTHOR

# Every feature status is one of these. Do not invent nearby words.
STATUSES = ("supported", "static_only", "approximate", "unavailable", "unknown", "checklist_only", "available")


def matrix() -> dict:
    """Feature statuses only. `interaction` (a metadata edit) is not `interactions` (a contract check)."""
    validator = "available" if os.path.exists(REPORT_AUTHOR) else "unavailable"
    features = {
        "native_interactions": {"status": "unavailable", "source": "interactiontest"},
        "interactions": {"status": "static_only", "source": "interactiontest"},
        "interaction": {"status": "supported"},
        "smoke": {"status": "approximate"},
        "preview": {"status": "approximate"},
        "freshness_publication": {"status": "unknown"},
        "report_validator": {"status": validator},
        "doctor": {"status": "checklist_only"},
        "isolation": {"status": "checklist_only"},
    }
    bad = [name for name, rec in features.items() if rec["status"] not in STATUSES]
    if bad:
        raise RuntimeError("capability status outside the closed vocabulary: " + ", ".join(bad))
    buckets = {status: [] for status in STATUSES}
    for name, rec in features.items():
        buckets[rec["status"]].append(name)
    buckets = {status: names for status, names in buckets.items() if names}
    return {"verb": "capabilities", "statuses": list(STATUSES), "features": features, "buckets": buckets}
