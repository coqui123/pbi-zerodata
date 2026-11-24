"""CI annotations and machine-readable reports for validate and bpa.

Annotations carry object names, rule ids and paths. Query text and data values are not written.
`--fail-on new` compares fingerprints with a saved baseline. A high finding is severity 3 or
a validate severity of error.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

from .common import PbzError, pbz_state_path, read_checked, write_text, _workspace_root

MAX_BASELINE_BYTES = 1_000_000
FINGERPRINT_VERSION = 1

# XML 1.0 (5th ed.) valid characters:
# #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
_XML_INVALID = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_V1_TOKEN_PATTERN = re.compile(r"^v1:[0-9a-f]{64}\Z")


def _fp_v0(finding: dict) -> str:
    """Legacy v0 delimiter fingerprint (used for backwards-compatible legacy baseline comparisons)."""
    return "|".join(str(finding.get(k) or "") for k in ("rule", "code", "object", "where"))[:300]


def _fp_v1(finding: dict) -> str:
    """Versioned v1 fingerprint: SHA-256 over canonical JSON array of identity fields."""
    parts = [str(finding.get(k) or "") for k in ("rule", "code", "object", "where")]
    blob = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return "v1:" + hashlib.sha256(blob).hexdigest()


def _fp(finding: dict) -> str:
    return _fp_v1(finding)


def _xml_safe(val: str) -> str:
    """Filter out characters invalid in XML 1.0."""
    return _XML_INVALID.sub("", str(val or ""))


def _high(finding: dict) -> bool:
    sev = finding.get("severity")
    return sev in (3, "3", "error", "high")


def _selected(findings, fail_on: str | None, baseline: set[str] | None, version: int = 1):
    if fail_on == "new":
        if baseline is None:
            raise PbzError("BAD_REQUEST", "--fail-on new requires --since")
        fp_fn = _fp_v0 if version == 0 else _fp_v1
        return [f for f in findings if fp_fn(f) not in baseline]
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


def load_baseline(ws, name: str, return_version: bool = False) -> set[str] | tuple[int, set[str]]:
    path = _baseline_path(ws, name)
    if not os.path.lexists(path):
        raise PbzError("NOT_FOUND", "baseline")
    try:
        doc = json.loads(read_checked(path, root=_workspace_root(ws), max_bytes=MAX_BASELINE_BYTES).decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as err:
        raise PbzError("REFUSED", "baseline is unusable") from err
    if not isinstance(doc, dict):
        raise PbzError("REFUSED", "baseline is unusable")
    raw_ids = doc.get("ids")
    if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
        raise PbzError("REFUSED", "baseline is unusable")

    if "fingerprint_version" not in doc:
        # Legacy baseline (version 0)
        version = 0
    else:
        v = doc["fingerprint_version"]
        if isinstance(v, bool) or not isinstance(v, int) or v != 1:
            raise PbzError("REFUSED", "unsupported baseline fingerprint version")
        version = 1
        if any(not _V1_TOKEN_PATTERN.fullmatch(item) for item in raw_ids):
            raise PbzError("REFUSED", "malformed v1 baseline token")

    ids = set(raw_ids)
    return (version, ids) if return_version else ids


def save_baseline(ws, name: str, findings) -> dict:
    path = _baseline_path(ws, name)
    ids = sorted({_fp_v1(f) for f in findings})
    body = json.dumps({"fingerprint_version": 1, "ids": ids}) + "\n"
    if len(body.encode("utf-8")) > MAX_BASELINE_BYTES:
        raise PbzError("REFUSED", f"baseline exceeds maximum size of {MAX_BASELINE_BYTES} bytes")
    write_text(path, body, root=_workspace_root(ws))
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


def _github_data(value: str) -> str:
    """GitHub workflow command data: percent-encode %, CR and LF."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _github_property(value: str) -> str:
    """GitHub workflow property value: data escapes plus colon and comma."""
    return (value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            .replace(":", "%3A").replace(",", "%2C"))


def _azdo_value(value: str) -> str:
    """Azure DevOps logging command value: percent-encode %, CR, LF, semicolon and ]."""
    return (value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            .replace(";", "%3B").replace("]", "%5D"))


def _ci_lines(kind: str, findings) -> list[str]:
    lines = []
    for ann in _annotations(findings):
        title = ann["rule"] or "pbz"
        # Rule and object only. Measure expressions are not annotation fields and stay out.
        message = f"{title} {ann['object']}".strip()
        if kind == "github":
            lines.append("::error file=" + _github_property(ann["path"])
                         + ",title=" + _github_property(title)
                         + "::" + _github_data(message))
        elif kind == "azdo":
            lines.append("##vso[task.logissue type=error;sourcepath="
                         + _azdo_value(ann["path"]) + "]" + _azdo_value(message))
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
        total = len(findings)
        truncated = total > 500
        emitted_findings = findings[:500]
        omitted = total - 500 if truncated else 0

        if total == 0:
            suite_tests = 1
            suite_failures = 0
        elif truncated:
            suite_tests = 501
            suite_failures = 501
        else:
            suite_tests = total
            suite_failures = total

        suite = ET.Element("testsuite", name="pbz", tests=str(suite_tests), failures=str(suite_failures),
                           errors="0", skipped="0")

        # Suite-level properties
        suite_props = ET.SubElement(suite, "properties")
        ET.SubElement(suite_props, "property", name="pbz.fingerprint_version", value="1")
        ET.SubElement(suite_props, "property", name="pbz.total_findings", value=str(total))
        ET.SubElement(suite_props, "property", name="pbz.emitted_findings", value=str(len(emitted_findings)))
        ET.SubElement(suite_props, "property", name="pbz.omitted_findings", value=str(omitted))
        ET.SubElement(suite_props, "property", name="pbz.truncated", value="true" if truncated else "false")

        if total == 0:
            ET.SubElement(suite, "testcase", name="pbz")
        else:
            for finding in emitted_findings:
                rule_name = _xml_safe(finding.get("rule") or finding.get("code") or "pbz") or "pbz"
                obj_name = _xml_safe(finding.get("object") or "")
                case = ET.SubElement(suite, "testcase", name=rule_name, classname=obj_name)
                props = ET.SubElement(case, "properties")
                ET.SubElement(props, "property", name="pbz.fingerprint", value=_fp_v1(finding))
                ET.SubElement(props, "property", name="pbz.fingerprint_version", value="1")
                ET.SubElement(case, "failure", message=obj_name)

            if truncated:
                sentinel = ET.SubElement(suite, "testcase", name="pbz.truncated", classname="pbz.ciout")
                sentinel_props = ET.SubElement(sentinel, "properties")
                ET.SubElement(sentinel_props, "property", name="pbz.truncated", value="true")
                ET.SubElement(sentinel_props, "property", name="pbz.omitted_findings", value=str(omitted))
                ET.SubElement(sentinel, "failure", type="TRUNCATED",
                              message=f"Report truncated: 500 of {total} findings shown ({omitted} omitted)")

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
    if since:
        version, baseline = load_baseline(ws, since, return_version=True)
        if version == 0:
            out["baseline_legacy"] = True
    else:
        version, baseline = 1, None
    if save:
        out["baseline_saved"] = save_baseline(ws, save, findings)
    gated = _selected(findings, fail_on, baseline, version=version)
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
