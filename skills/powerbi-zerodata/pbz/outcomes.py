"""Versioned operational outcomes shared by CLI and MCP; never include raw errors."""
from __future__ import annotations

BLOCKED = {"DESKTOP_OPEN", "DESKTOP_DIRTY", "DESKTOP_STATE_UNKNOWN", "WAITING_ON_DIALOG", "CONFLICT",
           "CONFLICT_RISK", "PENDING_PQ_CHANGES", "BUDGET", "RECOVERY_REQUIRED", "INCOMPARABLE",
           "PENDING_PUSH", "INCOMPLETE", "HUMAN_REVIEW_REQUIRED", "DEPENDENCY_PENDING", "STALE_EVIDENCE",
           "RECOVERY_CONFLICT", "COMMIT_BUSY", "AMBIGUOUS_DESKTOP"}
UNAVAILABLE = {"ENGINE_UNAVAILABLE", "NOT_AVAILABLE", "UNSUPPORTED", "TIMEOUT", "NOT_READY", "CLI_UNAVAILABLE"}
INVALID = {"INVALID", "BAD_REQUEST", "BAD_ARGUMENTS", "UNKNOWN_TOKEN", "SPEC_INVALID"}


def envelope(out: dict, diagnostic: bool = False) -> tuple[dict, int]:
    """Keep existing fields while giving callers one canonical status and exit code."""
    out = dict(out)
    candidates = [out.get(k) for k in ("result", "status", "action", "error")]
    known = BLOCKED | UNAVAILABLE | INVALID | {"ROLLED_BACK"}
    code = next((c for c in candidates if isinstance(c, str) and c in known),
                str(out.get("error") or out.get("result") or out.get("status") or out.get("action") or "OK"))
    query_rows = [r for r in out.get("results", []) + out.get("queries", []) if isinstance(r, dict)]
    visual_rows = [r for r in out.get("visuals") or [] if isinstance(r, dict)]
    status, rc = "SUCCEEDED", 0
    reload = out.get("reload")
    if out.get("result") == "PUSHED" and reload and reload != "RELOADED":
        status, code, rc = "PARTIAL", "APPLIED_RELOAD_FAILED", 5
    elif code in BLOCKED:
        status, rc = "BLOCKED", 3
    elif code in UNAVAILABLE:
        status, rc = "UNAVAILABLE", 4
    elif code in INVALID or out.get("spec_valid") is False:
        status, rc = "INVALID", 2
    elif code == "ROLLED_BACK":
        status, rc = "FAILED", 1
    elif out.get("ok") is False or out.get("error"):
        status, rc = "FAILED", 2
    elif out.get("gate") == "FAIL":
        status, code, rc = "FAILED", "GATE_FAILED", 1
    elif any(f.get("severity") == "error" for f in out.get("findings", []) if isinstance(f, dict)):
        status, code, rc = "INVALID", "VALIDATION_FAILED", 2
    elif any(r.get("status") in {"FAIL", "ERROR"} or r.get("result") in {"DIFF", "ERROR"} for r in query_rows) \
            or any(r.get("status") in {"ERROR", "PROTOCOL_ERROR", "FAIL"} for r in visual_rows):
        status, code, rc = ("FINDINGS", "DIAGNOSTIC_FINDINGS", 0) if diagnostic else ("FAILED", "ASSERTION_FAILED", 1)
    elif any(r.get("status") in {"EMPTY", "UNSUPPORTED", "BUDGET"} or r.get("result") in {"STALE_DATA", "ERROR"}
             for r in query_rows):
        status, code, rc = "BLOCKED", "INCONCLUSIVE_EVIDENCE", 3
    elif out.get("status") == "BUDGET":
        status, code, rc = "BLOCKED", "BUDGET", 3
    out["outcome"] = {"version": 1, "status": status, "code": code, "exit_code": rc}
    return out, rc
