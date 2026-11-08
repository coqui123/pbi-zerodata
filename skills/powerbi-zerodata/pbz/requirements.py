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
from .common import PbzError, _reparse_link, anchor_mirror, read_checked


CLOSED = {"PASS", "FAIL", "ERROR", "EMPTY", "UNSUPPORTED", "BUDGET"}
_SYNC = "sync evidence is unusable"
_EVIDENCE = "requirement check evidence is unusable"


def _path(ws, project):
    name = hashlib.sha256(project.name.encode()).hexdigest()[:16] + ".json"
    return control.output_path(ws, "requirements", name)


def _load(ws, project):
    anchor_mirror(ws)
    path = _path(ws, project)
    if not os.path.lexists(path):
        raise PbzError("NOT_FOUND", "import a requirement specification first")
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    try:
        raw = read_checked(path, root=ws.mirror)
        ledger = json.loads(raw.decode("utf-8"))
    except PbzError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise PbzError("REFUSED", "requirement ledger is unreadable") from None
    if not isinstance(ledger, dict) or not isinstance(ledger.get("tasks"), dict):
        raise PbzError("REFUSED", "requirement ledger is malformed")
    return ledger


def _binding(ws, project, *, strict: bool, need_sync: bool):
    """Mirror plus saved-real identity. Engine checks refuse a missing manifest first."""
    from . import mirror
    manifest = mirror.load_manifest(ws, project)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        files = {}
    if need_sync and not files:
        if strict:
            raise PbzError("STALE_EVIDENCE", _SYNC)
        return None
    try:
        live = {rel.replace("\\", "/"): path for rel, path in mirror.project_files(project, ws)}
        # Project-scoped additions (and rematerialized paths) must appear in the manifest.
        if set(live) - set(files):
            raise PbzError("CONFLICT", "saved files changed; pull before verifying requirements")
        digests = {}
        if files:
            rows = []
            for rel, info in files.items():
                parts = rel.replace("\\", "/").split("/") if isinstance(rel, str) else []
                if (not isinstance(info, dict) or not parts or os.path.isabs(rel)
                        or any(part in ("", ".", "..") for part in parts)):
                    raise PbzError("STALE_EVIDENCE", _SYNC)
                target = os.path.join(ws.root, *parts)
                if _reparse_link(target):
                    raise PbzError("UNSAFE_PATH", "refusing to write through a link")
                rows.append((rel, target, info.get("real")))
            if need_sync and any(mirror.mirror_changes(ws, project, manifest).values()):
                raise PbzError("PENDING_PUSH", "engine checks cannot verify unpushed mirror edits")
            for rel, target, recorded in rows:
                digest = hashlib.sha256(read_checked(target, root=ws.root)).hexdigest()
                if digest != recorded:
                    raise PbzError("CONFLICT", "saved files changed; pull before verifying requirements")
                digests[rel] = digest
        # Binding is derived from the validated capture, not a second manifest reload.
        h = hashlib.sha256()
        for rel in sorted(digests):
            h.update(f"{rel}\0{digests[rel]}\n".encode("utf-8"))
        real = "r" + h.hexdigest()[:12]
        return {"mirror": evidence.mirror_rev(ws, project), "real": real}
    except PbzError:
        if strict:
            raise
        return None


def _bound(stored, live) -> bool:
    return (isinstance(live, dict) and isinstance(stored, dict)
            and isinstance(stored.get("mirror"), str) and isinstance(stored.get("real"), str)
            and stored["mirror"] == live["mirror"] and stored["real"] == live["real"])


def import_spec(ws, project, spec):
    data = control.load(spec, root=ws.root)
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
        if not isinstance(spec_checks, list):
            raise PbzError("BAD_REQUEST", "invalid registered acceptance checks")
        seen = []
        for check in spec_checks:
            if not isinstance(check, dict) or "id" not in check:
                raise PbzError("BAD_REQUEST", "invalid registered acceptance checks")
            seen.append(control.identifier(check["id"]))
        if len(seen) != len(set(seen)):
            raise PbzError("BAD_REQUEST", "duplicate registered check id")
        validation = checks.validate_spec({"checks": spec_checks}) if spec_checks else {"valid": True}
        if not validation.get("valid"):
            raise PbzError("BAD_REQUEST", "invalid registered acceptance checks")
        # Text is intentionally opaque to the agent. Do not echo descriptions or source captions.
        private[ident] = {"description": task.get("description", ""), "source": source, "checks": spec_checks}
        public[ident] = {"id": ident, "source_fingerprint": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
                         "cells": cells, "targets": targets, "depends_on": deps,
                         "required_checks": seen, "verification": None, "accepted_revision": None}
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
        visiting.remove(ident)
        done.add(ident)

    for ident in public:
        visit(ident)
    ledger = _path(ws, project)
    config = uuid.uuid4().hex
    location = os.path.join(ws.private, "requirements", config + ".bin")
    dpapi.write_protected(location, json.dumps(private, ensure_ascii=False).encode())
    control.save(ledger, {"version": 1, "config": config, "tasks": public}, root=ws.mirror)
    return {"verb": "requirements import", "result": "IMPORTED", "tasks": sorted(public),
            "note": "Descriptions and source captions remain private. Source workbook cells were not read."}


def status(ws, project):
    ledger = _load(ws, project)
    live = _binding(ws, project, strict=False, need_sync=False)
    result = []
    for ident, task in ledger["tasks"].items():
        proof = task.get("verification") or {}
        state = "OPEN"
        if proof:
            if _bound(proof.get("revision"), live):
                state = "VERIFIED" if proof.get("passed") else "INCOMPLETE"
            else:
                state = "STALE"
        if task.get("accepted_revision"):
            state = "ACCEPTED" if _bound(task.get("accepted_revision"), live) else "STALE"
        result.append({"id": ident, "status": state, "cells": task["cells"], "required_checks": task["required_checks"],
                       "depends_on": task["depends_on"], "human_review": "ACCEPTED" if state == "ACCEPTED" else "PENDING"})
    return {"verb": "requirements status", "revision": None if live is None else live["mirror"], "tasks": result}


def _check_rows(registered, output):
    ids = []
    for check in registered:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str):
            raise PbzError("BAD_REQUEST", _EVIDENCE)
        ids.append(check["id"])
    if len(ids) != len(set(ids)):
        raise PbzError("BAD_REQUEST", _EVIDENCE)
    rows = output.get("results") if isinstance(output, dict) else None
    if not isinstance(rows, list):
        raise PbzError("BAD_REQUEST", _EVIDENCE)
    found = {}
    allowed = set(ids)
    for row in rows:
        if not isinstance(row, dict):
            raise PbzError("BAD_REQUEST", _EVIDENCE)
        ident = row.get("id")
        status = row.get("status")
        if not isinstance(ident, str) or not isinstance(status, str):
            raise PbzError("BAD_REQUEST", _EVIDENCE)
        if ident not in allowed or ident in found or status not in CLOSED:
            raise PbzError("BAD_REQUEST", _EVIDENCE)
        found[ident] = status
    if set(found) != allowed:
        raise PbzError("BAD_REQUEST", _EVIDENCE)
    return [{"id": ident, "status": found[ident]} for ident in ids]


def verify(ws, project, ident):
    control.identifier(ident)
    ledger = _load(ws, project)
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
    before = _binding(ws, project, strict=True, need_sync=True)
    output = checks.run_checks(ws, project, {"checks": registered})
    after = _binding(ws, project, strict=True, need_sync=True)
    if before != after:
        raise PbzError("STALE_EVIDENCE", "revision changed during verification")
    statuses = _check_rows(registered, output)
    passed = all(row["status"] == "PASS" for row in statuses)
    task["verification"] = {"revision": after, "passed": passed, "checks": statuses}
    task["accepted_revision"] = None
    control.save(_path(ws, project), ledger, root=ws.mirror)
    return {"verb": "requirements verify", "id": ident, "result": "VERIFIED" if passed else "INCOMPLETE",
            "results": statuses, "human_review": "PENDING"}


def _human_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and not any(os.environ.get(k) for k in
        ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM"))


def accept(ws, project, ident, human=False):
    if human is not True or not _human_terminal():
        raise PbzError("HUMAN_ONLY", "human acceptance must run in a local terminal")
    ledger = _load(ws, project)
    control.identifier(ident)
    if ident not in ledger["tasks"]:
        raise PbzError("NOT_FOUND", "requirement id")
    task = ledger["tasks"][ident]
    live = _binding(ws, project, strict=True, need_sync=bool(task["required_checks"]))
    proof = task.get("verification") or {}
    if task["required_checks"] and not (_bound(proof.get("revision"), live) and proof.get("passed") is True):
        raise PbzError("STALE_EVIDENCE", "current passing registered checks are required")
    if any(not _bound(ledger["tasks"][dep].get("accepted_revision"), live) for dep in task["depends_on"]):
        raise PbzError("DEPENDENCY_PENDING", "accept dependencies first")
    task["accepted_revision"] = live
    control.save(_path(ws, project), ledger, root=ws.mirror)
    return {"verb": "requirements accept", "id": ident, "result": "ACCEPTED", "revision": live["mirror"]}
