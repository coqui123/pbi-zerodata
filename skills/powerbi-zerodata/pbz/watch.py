"""Re-run mirror validation and best-practice analysis when asked.

`--once` runs a single pass so tests, CI, and MCP do not loop. A human CLI session
that omits it polls until interrupted.
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

    while True:
        passes.append(_once())
        if once:
            break
        try:
            time.sleep(min(max(int(seconds), 1), 30))
        except KeyboardInterrupt:
            break
    return {"verb": "watch", "once": once, "passes": passes}
