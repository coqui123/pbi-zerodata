"""Validation of a staged (real, detokenized) project. Output is retokenized before it is shown."""
from __future__ import annotations

import json
import os
import re
import subprocess

from . import lint, pbir, pbircheck, tmdl
from .classify import PII_IN_TEXT
from .common import TOOLS_BIN, engine_call
from .tokens import TOKEN_RE

REPORT_AUTHOR = os.path.join(TOOLS_BIN, "powerbi-report-author.cmd")


def retokenizer(tm, extra_prefixes=()):
    pairs = []
    for tok, rec in tm.data.get("tokens", {}).items():
        for v in [rec.get("value")] + list((rec.get("raws") or {}).values()):
            if isinstance(v, (str, int, float)) and len(str(v).strip()) >= 3:
                pairs.append((str(v), tok))
    pairs.sort(key=lambda p: -len(p[0]))

    def scrub(msg: str) -> str:
        if not isinstance(msg, str):
            return ""
        for pre in extra_prefixes:
            msg = msg.replace(pre, "").replace(pre.replace("\\", "/"), "")
        for v, tok in pairs:
            if v in msg:
                msg = msg.replace(v, tok)
        msg = PII_IN_TEXT.sub("«pii»", msg)
        return msg[:400]

    return scrub


def _diagnostics(data) -> list[dict]:
    out = []

    def visit(node):
        if isinstance(node, dict):
            if "message" in node and isinstance(node["message"], str):
                out.append(node)
                return
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(data)
    return out


def report_author(stage: str, project, scrub) -> list[dict]:
    rdir = os.path.join(stage, project.name + ".Report")
    if not os.path.isdir(rdir):
        return []
    if not os.path.exists(REPORT_AUTHOR):
        return [{"severity": "info", "code": "REPORT_VALIDATOR_MISSING", "where": "report", "message": "skipped"}]
    for args in ([REPORT_AUTHOR, "validate", rdir, "--format", "json"],
                 [REPORT_AUTHOR, "validate", rdir, "--no-schema", "--format", "json"]):
        try:
            p = subprocess.run(args, capture_output=True, timeout=600)
        except subprocess.SubprocessError:
            continue
        try:
            data = json.loads(p.stdout.decode("utf-8", errors="replace") or "{}")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("error") and "--no-schema" not in args:
            continue  # e.g. schema download failed: retry offline
        res = []
        summary = data.get("data", {}) if isinstance(data, dict) else {}
        for d in _diagnostics(data):
            sev = str(d.get("severity") or d.get("level") or "error").lower()
            sev = "error" if sev.startswith("err") else ("warning" if sev.startswith("warn") else "info")
            where = d.get("path") or d.get("file") or d.get("location") or "report"
            res.append({"severity": sev, "code": str(d.get("code") or d.get("rule") or "PBIR"),
                        "where": scrub(str(where)), "message": scrub(d["message"])})
        if summary.get("result") and summary.get("result") != "succeeded" and not res:
            res.append({"severity": "error", "code": "PBIR_INVALID", "where": "report",
                        "message": f"{summary.get('errorCount', '?')} error(s)"})
        return res
    return [{"severity": "info", "code": "REPORT_VALIDATOR_FAILED", "where": "report", "message": "could not run"}]


def field_refs(stage: str, project, model, scrub) -> list[dict]:
    out = []
    rdir = os.path.join(stage, project.name + ".Report")
    ext_measures = set()
    ext = os.path.join(rdir, "definition", "reportExtensions.json")
    if os.path.exists(ext):
        try:
            with open(ext, encoding="utf-8-sig") as f:
                for e in json.load(f).get("entities", []):
                    for m in e.get("measures", []):
                        ext_measures.add(m.get("name", "").lower())
        except (OSError, ValueError):
            pass
    for path in pbir.report_files(rdir):
        rel = os.path.relpath(path, rdir).replace("\\", "/")
        try:
            with open(path, encoding="utf-8-sig") as f:
                root = pbir.parse_json(f.read())
        except (OSError, pbir.JsonError, UnicodeDecodeError):
            out.append({"severity": "error", "code": "BAD_JSON", "where": rel, "message": "not valid JSON"})
            continue
        for ref in pbir.walk_fields(root, model):
            if not ref.table or not ref.name:
                continue
            if ref.kind == "measure":
                if not model.measure(ref.name) and ref.name.lower() not in ext_measures:
                    out.append({"severity": "error", "code": "UNKNOWN_FIELD", "where": rel,
                                "message": scrub(f"measure {ref.table}[{ref.name}] not in model")})
            elif ref.kind == "hierarchy":
                t = model.tables.get(ref.table)
                if not t:
                    out.append({"severity": "error", "code": "UNKNOWN_FIELD", "where": rel,
                                "message": scrub(f"table {ref.table} not in model")})
            elif not model.column(ref.table, ref.name) and not model.measure(ref.name):
                out.append({"severity": "error", "code": "UNKNOWN_FIELD", "where": rel,
                            "message": scrub(f"column {ref.table}[{ref.name}] not in model")})
    return out


def run_stage(ws, project, stage: str, tm) -> list[dict]:
    scrub = retokenizer(tm, extra_prefixes=(stage + os.sep, stage))
    findings: list[dict] = []
    mdef = os.path.join(stage, project.name + ".SemanticModel", "definition")
    if os.path.isdir(mdef):
        res = engine_call("tmdl-validate", {"private_dir": ws.private, "folder": mdef}, timeout=300)
        if res.get("ok"):
            for e in res.get("errors", []):
                findings.append({"severity": "error", "code": "TMDL_" + str(e.get("class", "OTHER")),
                                 "where": f"{e.get('file', '?')}:{e.get('line', '?')}", "message": scrub(e.get("message", ""))})
            for f in res.get("missing_refs", []):
                findings.append({"severity": "error", "code": "TMDL_MISSING_FILE", "where": f, "message": "ref has no file"})
            for f in res.get("orphan_files", []):
                findings.append({"severity": "warning", "code": "TMDL_ORPHAN_FILE", "where": f, "message": "file not referenced"})
        else:
            findings.append({"severity": "info", "code": "TOM_" + str(res.get("class", "UNAVAILABLE")),
                             "where": "model", "message": "TMDL not checked by TOM"})
        model = tmdl.load_model(mdef)
        for f in lint.run(model):
            f["message"] = scrub(f["message"])
            findings.append(f)
        findings += synthetic_marker(model)
    else:
        model = tmdl.Model()
    findings += report_author(stage, project, scrub)
    rdir = os.path.join(stage, project.name + ".Report")
    if os.path.isdir(rdir):
        findings += [dict(f, message=scrub(f["message"])) for f in pbircheck.run(rdir)]
    findings += field_refs(stage, project, model, scrub)
    for cur, _dirs, files in os.walk(stage):
        for name in files:
            if name.lower().endswith((".tmdl", ".json", ".pbir", ".pbism")):
                p = os.path.join(cur, name)
                with open(p, encoding="utf-8", errors="replace") as f:
                    if TOKEN_RE.search(f.read()):
                        findings.append({"severity": "error", "code": "UNRESOLVED_TOKEN",
                                         "where": os.path.relpath(p, stage).replace("\\", "/"), "message": "token left"})
    return findings


def synthetic_marker(model) -> list[dict]:
    """The PBZ_SYNTHETIC marker unlocks `pbz synth query`; it must never reach a real model."""
    return [{"severity": "error", "code": "SYNTHETIC_MARKER_IN_REAL_MODEL", "where": doc.path,
             "message": "the PBZ_SYNTHETIC annotation belongs to pbz synth copies only; remove it"}
            for doc in model.docs.values() for node in doc.nodes
            if node.kind == "annotation" and node.name.strip("'").upper() == "PBZ_SYNTHETIC"]


def run_mirror(ws, project) -> list[dict]:
    """Metadata-only checks on the mirror itself (no engine): lint + field references."""
    mdef = os.path.join(project.mirror_model, "definition")
    model = tmdl.load_model(mdef) if os.path.isdir(mdef) else tmdl.Model()
    ident = lambda s: s  # mirror text is already sanitized
    out = lint.run(model) + synthetic_marker(model)
    out += field_refs(ws.mirror, project, model, ident)
    if os.path.isdir(project.mirror_report):
        out += pbircheck.run(project.mirror_report)
    return out
