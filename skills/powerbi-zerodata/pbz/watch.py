"""Re-run mirror validation and best-practice analysis when asked.

`--once` runs a single pass so tests and CI do not loop. A human can omit it and poll.
"""
from __future__ import annotations

import time

from . import bpa, validate


def run(ws, project, once: bool = False, seconds: int = 5) -> dict:
    passes = []

    def _once():
        findings = validate.run_mirror(ws, project)
        rules = bpa.run(ws, project, "all")
        count = (rules.get("model") or {}).get("findings", 0)
        report = rules.get("report")
        if isinstance(report, dict):
            count += report.get("findings", 0)
        return {"validate": len(findings),
                "validate_errors": sum(1 for f in findings if f.get("severity") == "error"),
                "bpa": count}

    passes.append(_once())
    if not once:
        # One extra quiet pass is enough to show the poll; callers that want a loop use a terminal.
        time.sleep(min(max(int(seconds), 1), 30))
        passes.append(_once())
    return {"verb": "watch", "once": once, "passes": passes}
