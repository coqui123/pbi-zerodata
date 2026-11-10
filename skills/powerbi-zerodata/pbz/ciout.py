"""CI annotations and machine-readable reports for validate and bpa.

Annotations carry object names, rule ids and paths. Query text and data values are not written.
`--fail-on new` compares fingerprints with a saved baseline. A high finding is severity 3 or
a validate severity of error.
"""
from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET

from .common import PbzError, pbz_state_path, read_checked, write_text, _workspace_root


def _fp(finding: dict) -> str:
    return "|".join(str(finding.get(k) or "") for k in ("rule", "code", "object", "where"))[:300]


def _high(finding: dict) -> bool:
    sev = finding.get("severity")
    return sev in (3, "3", "error", "high")


def _selected(findings, fail_on: str | None, baseline: set[str] | None):
    if fail_on == "new":
        if baseline is None:
            raise PbzError("BAD_REQUEST", "--fail-on new requires --since")
        return [f for f in findings if _fp(f) not in baseline]
    if fail_on == "high":
        return [f for f in findings if _high(f)]
    if fail_on == "any":
        return list(findings)
    return []


def _baseline_path(ws, name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name)[:80]
    if not safe:
        raise PbzError("BAD_REQUEST", "baseline name is empty")
    return pbz_state_path(ws, "baselines", safe + ".json")


def load_baseline(ws, name: str) -> set[str]:
    path = _baseline_path(ws, name)
    if not os.path.lexists(path):
        raise PbzError("NOT_FOUND", "baseline")
    try:
        doc = json.loads(read_checked(path, root=_workspace_root(ws), max_bytes=1_000_000).decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as err:
        raise PbzError("REFUSED", "baseline is unusable") from err
    return set(doc.get("ids") or [])


def save_baseline(ws, name: str, findings) -> dict:
    path = _baseline_path(ws, name)
    ids = sorted({_fp(f) for f in findings})
    write_text(path, json.dumps({"ids": ids}) + "\n", root=_workspace_root(ws))
    return {"baseline": name, "fingerprints": len(ids)}


def _annotations(findings) -> list[dict]:
    out = []
    for finding in findings[:200]:
        severity = finding.get("severity")
        if severity not in (1, 2, 3, "informational", "warning", "error"):
            severity = None
        out.append({"rule": str(finding.get("rule") or finding.get("code") or "")[:80],
                    "object": str(finding.get("object") or "")[:180],
                    "path": str(finding.get("where") or finding.get("file") or "")[:260],
                    "severity": severity})
    return out


def _stderr(lines: list[str]) -> None:
    if lines:
        sys.stderr.write("\n".join(lines) + "\n")


def _ci_lines(kind: str, findings) -> list[str]:
    lines = []
    for ann in _annotations(findings):
        title = ann["rule"] or "pbz"
        path = ann["path"].replace(",", "_")
        message = f"{title} {ann['object']}".strip()
        if kind == "github":
            lines.append(f"::error file={path},title={title}::{message}")
        elif kind == "azdo":
            lines.append(f"##vso[task.logissue type=error;sourcepath={path}]{message}")
    return lines


def _write_report(fmt: str, path: str, findings, root: str) -> None:
    if fmt == "sarif":
        body = json.dumps({
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "pbz"}},
                      "results": [{"ruleId": str(f.get("rule") or f.get("code") or "pbz"),
                                   "message": {"text": str(f.get("object") or f.get("rule") or "pbz")[:180]},
                                   "locations": [{"physicalLocation": {"artifactLocation": {
                                       "uri": str(f.get("where") or f.get("file") or "")}}}]}
                                  for f in findings[:500]]}],
        })
    elif fmt == "junit":
        suite = ET.Element("testsuite", name="pbz", tests=str(max(len(findings), 1)))
        if not findings:
            ET.SubElement(suite, "testcase", name="pbz")
        for finding in findings[:500]:
            case = ET.SubElement(suite, "testcase", name=str(finding.get("rule") or finding.get("code") or "pbz"),
                                 classname=str(finding.get("object") or ""))
            ET.SubElement(case, "failure", message=str(finding.get("object") or ""))
        body = ET.tostring(suite, encoding="unicode")
    elif fmt == "trx":
        suite = ET.Element("TestRun")
        results = ET.SubElement(suite, "Results")
        for finding in findings[:500]:
            ET.SubElement(results, "UnitTestResult",
                          testName=str(finding.get("rule") or finding.get("code") or "pbz"),
                          outcome="Failed")
        body = ET.tostring(suite, encoding="unicode")
    else:
        raise PbzError("BAD_REQUEST", "format must be sarif, trx or junit")
    write_text(path, body, root=root)


def attach(ws, out: dict, findings, *, fail_on: str | None = None, since: str | None = None,
           save: str | None = None, ci: str | None = None, fmt: str | None = None, report: str | None = None) -> dict:
    baseline = load_baseline(ws, since) if since else None
    if save:
        out["baseline_saved"] = save_baseline(ws, save, findings)
    gated = _selected(findings, fail_on, baseline)
    if fail_on:
        out["gate"] = "FAIL" if gated else "PASS"
        out["gate_on"] = fail_on
        out["gate_count"] = len(gated)
    if ci:
        if ci not in ("github", "azdo"):
            raise PbzError("BAD_REQUEST", "ci must be github or azdo")
        anns = _annotations(gated if fail_on else findings)
        out["annotations"] = anns
        _stderr(_ci_lines(ci, gated if fail_on else findings))
    if fmt:
        if not report:
            raise PbzError("BAD_REQUEST", "--format requires --out")
        _write_report(fmt, report, findings, _workspace_root(ws))
        out["report"] = os.path.basename(report)
        out["report_format"] = fmt
    return out
