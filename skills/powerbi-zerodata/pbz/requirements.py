"""Requirement coverage tied to registered checks, exact revisions and human acceptance.

Intake is prepared JSON with Excel cell locators, not raw workbook data extraction.
Descriptions and check expressions remain in protected storage; public ledgers contain IDs only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from . import control, dpapi, evidence, checks
from .common import PbzError


def _path(ws, project):
    name = hashlib.sha256(project.name.encode()).hexdigest()[:16] + ".json"
    return control.output_path(ws, "requirements", name)


def _load(ws, project):
    try:
        with open(_path(ws, project), encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise PbzError("NOT_FOUND", "import a requirement specification first") from None


def import_spec(ws, project, spec):
    data = control.load(spec)
    control.keys(data, {"version", "tasks"}, {"tasks"})
    tasks = data["tasks"]
    if not isinstance(tasks, list) or not tasks or len(tasks) > 500:
        raise PbzError("BAD_REQUEST", "tasks must be a bounded nonempty list")
    public, private = {}, {}
    for task in tasks:
        control.keys(task, {"id", "description", "source", "targets", "depends_on", "checks"}, {"id", "source"})
        ident = control.identifier(task["id"])
        if ident in public:
            raise PbzError("BAD_REQUEST", "duplicate requirement id")
        source = task["source"]
        control.keys(source, {"file", "sheet", "cells"}, {"file", "sheet", "cells"})
        if not isinstance(source["file"], str) or os.path.basename(source["file"]) != source["file"] or not source["file"].lower().endswith((".xlsx", ".json")):
            raise PbzError("BAD_REQUEST", "source must be a workbook/JSON basename, not a path")
        if not isinstance(source["sheet"], str) or not 1 <= len(source["sheet"]) <= 100:
            raise PbzError("BAD_REQUEST", "invalid source sheet locator")
        cells = source["cells"]
        if not isinstance(cells, list) or not cells or not all(isinstance(c, str) and re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}(?::[A-Z]{1,3}[1-9][0-9]{0,6})?", c) for c in cells):
            raise PbzError("BAD_REQUEST", "source cells must be Excel locators")
        targets = task.get("targets", [])
        deps = task.get("depends_on", [])
        if not isinstance(targets, list) or not all(isinstance(p, str) and not os.path.isabs(p) and ".." not in p.replace("\\", "/").split("/") for p in targets):
            raise PbzError("BAD_REQUEST", "targets must be mirror-relative paths")
        if not isinstance(deps, list):
            raise PbzError("BAD_REQUEST", "depends_on must be a list")
        for dep in deps:
            control.identifier(dep)
        spec_checks = task.get("checks", [])
        validation = checks.validate_spec({"checks": spec_checks}) if spec_checks else {"valid": True}
        if not validation.get("valid"):
            raise PbzError("BAD_REQUEST", "invalid registered acceptance checks")
        for check in spec_checks:
            control.identifier(check["id"])
        # Text is intentionally opaque to the agent. Do not echo descriptions or source captions.
        private[ident] = {"description": task.get("description", ""), "source": source, "checks": spec_checks}
        public[ident] = {"id": ident, "source_fingerprint": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
                         "cells": cells, "targets": targets, "depends_on": deps,
                         "required_checks": [c["id"] for c in spec_checks], "verification": None, "accepted_revision": None}
    visiting, done = set(), set()
    def visit(ident):
        if ident in visiting:
            raise PbzError("BAD_REQUEST", "cyclic requirement dependencies")
        if ident in done:
            return
        visiting.add(ident)
        for dep in public[ident]["depends_on"]:
            if dep not in public:
                raise PbzError("BAD_REQUEST", "unknown requirement dependency")
            visit(dep)
        visiting.remove(ident); done.add(ident)
    for ident in public:
        visit(ident)
    config = uuid.uuid4().hex
    location = os.path.join(ws.private, "requirements", config + ".bin")
    dpapi.write_protected(location, json.dumps(private, ensure_ascii=False).encode())
    control.save(_path(ws, project), {"version": 1, "config": config, "tasks": public})
    return {"verb": "requirements import", "result": "IMPORTED", "tasks": sorted(public),
            "note": "Descriptions and source captions remain private. Source workbook cells were not read."}


def status(ws, project):
    ledger = _load(ws, project); revision = evidence.mirror_rev(ws, project)
    result = []
    for ident, task in ledger["tasks"].items():
        proof = task.get("verification") or {}
        state = "OPEN"
        if proof:
            state = "STALE" if proof.get("revision") != revision else "VERIFIED" if proof.get("passed") else "INCOMPLETE"
        if task.get("accepted_revision"):
            state = "ACCEPTED" if task["accepted_revision"] == revision else "STALE"
        result.append({"id": ident, "status": state, "cells": task["cells"], "required_checks": task["required_checks"],
                       "depends_on": task["depends_on"], "human_review": "ACCEPTED" if state == "ACCEPTED" else "PENDING"})
    return {"verb": "requirements status", "revision": revision, "tasks": result}


def verify(ws, project, ident):
    control.identifier(ident); ledger = _load(ws, project)
    if ident not in ledger["tasks"]:
        raise PbzError("NOT_FOUND", "requirement id")
    task = ledger["tasks"][ident]
    config = ledger.get("config", "")
    if not re.fullmatch(r"[a-f0-9]{32}", config):
        raise PbzError("BAD_REQUEST", "invalid requirement configuration")
    payload = dpapi.read_protected(os.path.join(ws.private, "requirements", config + ".bin"))
    private = json.loads(payload.decode())
    registered = private[ident]["checks"]
    if not registered:
        return {"verb": "requirements verify", "result": "HUMAN_REVIEW_REQUIRED", "id": ident, "checks": []}
    from . import mirror
    manifest = mirror.load_manifest(ws, project) or {}
    if any(mirror.mirror_changes(ws, project, manifest).values()):
        raise PbzError("PENDING_PUSH", "engine checks cannot verify unpushed mirror edits")
    from .common import file_sha
    if any(file_sha(os.path.join(ws.root, *rel.split("/"))) != info.get("real")
           for rel, info in manifest.get("files", {}).items()):
        raise PbzError("CONFLICT", "saved files changed; pull before verifying requirements")
    output = checks.run_checks(ws, project, {"checks": registered})
    statuses = [{"id": c["id"], "status": c["status"]} for c in output.get("results", [])]
    passed = len(statuses) == len(registered) and all(c["status"] == "PASS" for c in statuses)
    task["verification"] = {"revision": evidence.mirror_rev(ws, project), "passed": passed, "checks": statuses}
    task["accepted_revision"] = None
    control.save(_path(ws, project), ledger)
    return {"verb": "requirements verify", "id": ident, "result": "VERIFIED" if passed else "INCOMPLETE",
            "results": statuses, "human_review": "PENDING"}


def _human_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and not any(os.environ.get(k) for k in
        ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM"))


def accept(ws, project, ident, human=False):
    if human is not True or not _human_terminal():
        raise PbzError("HUMAN_ONLY", "human acceptance must run in a local terminal")
    ledger = _load(ws, project); control.identifier(ident)
    if ident not in ledger["tasks"]:
        raise PbzError("NOT_FOUND", "requirement id")
    task = ledger["tasks"][ident]; rev = evidence.mirror_rev(ws, project)
    if task["required_checks"] and (not task.get("verification") or not task["verification"].get("passed") or task["verification"]["revision"] != rev):
        raise PbzError("STALE_EVIDENCE", "current passing registered checks are required")
    if any(ledger["tasks"][dep].get("accepted_revision") != rev for dep in task["depends_on"]):
        raise PbzError("DEPENDENCY_PENDING", "accept dependencies first")
    task["accepted_revision"] = rev; control.save(_path(ws, project), ledger)
    return {"verb": "requirements accept", "id": ident, "result": "ACCEPTED", "revision": rev}
