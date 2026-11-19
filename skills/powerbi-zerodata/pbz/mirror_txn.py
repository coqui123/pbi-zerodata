"""Durable, per-path undo for mirror edits under the project's commit lock."""
from __future__ import annotations

import base64
from contextvars import ContextVar
import json
import os
import shutil
import uuid

from .common import (PbzError, _contained, _path_inside, read_checked, remove_file, replace_bytes, sha)

_active = ContextVar("mirror_edit", default=None)


def _save(directory, journal):
    path = os.path.join(directory, "journal.json")
    replace_bytes(path, json.dumps(journal).encode("utf-8"), root=directory)
    # The intent and before-image must reach disk before the mirror write.
    with open(path, "r+b") as handle:
        os.fsync(handle.fileno())
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _raw(path, root):
    _contained(path, root)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    return read_checked(path, root=root)


def _hash(data):
    return None if data is None else sha(data)


def enlist(path, before, after):
    """Record first-touch before-image and next expected version before the writer applies it."""
    active = _active.get()
    if active is None:
        return
    ws, project, directory, journal = active
    path = os.path.abspath(path)
    rel = os.path.relpath(path, ws.mirror).replace("\\", "/")
    if not any(_path_inside(path, root)
               for root in (project.mirror_model, project.mirror_report)):
        raise PbzError("RECOVERY_REQUIRED", rel)
    entry = next((e for e in journal["entries"] if e["path"] == rel), None)
    if entry is None:
        entry = {"scope": "mirror", "path": rel, "before": _hash(before),
                 "data": None if before is None else base64.b64encode(before).decode("ascii"),
                 "after": _hash(before), "applied": _hash(before)}
        journal["entries"].append(entry)
    if entry["after"] != _hash(before):
        raise PbzError("RECOVERY_REQUIRED", rel)
    entry["after"] = _hash(after)
    _save(directory, journal)


def mark_applied(path, after):
    """Record the version this operation successfully wrote."""
    active = _active.get()
    if active is None:
        return
    ws, _project, directory, journal = active
    rel = os.path.relpath(os.path.abspath(path), ws.mirror).replace("\\", "/")
    entry = next(e for e in journal["entries"] if e["path"] == rel)
    entry["applied"] = _hash(after)
    _save(directory, journal)


def restore(ws, project, directory, journal):
    """Undo only owned versions. A newer version is preserved and requires recovery."""
    if journal.get("state") not in ("APPLYING", "RECOVERY_REQUIRED"):
        return {"result": "RECOVERY_REQUIRED"}
    failed = False
    for entry in reversed(journal["entries"]):
        try:
            rel = entry["path"]
            if (entry.get("scope") != "mirror" or ".." in rel.replace("\\", "/").split("/")
                    or not rel.startswith((project.name + ".SemanticModel/", project.name + ".Report/"))):
                raise ValueError()
            path = os.path.abspath(os.path.join(ws.mirror, *rel.split("/")))
            current = _hash(_raw(path, ws.mirror))
            if current == entry["before"]:
                continue
            if current != entry.get("applied", entry["after"]):
                raise ValueError()
            data = None if entry["data"] is None else base64.b64decode(entry["data"], validate=True)
            if _hash(data) != entry["before"]:
                raise ValueError()
            if data is None:
                remove_file(path, root=ws.mirror)
            else:
                replace_bytes(path, data, root=ws.mirror)
        except (OSError, PbzError, ValueError, KeyError, TypeError):
            failed = True
    journal["state"] = "RECOVERY_REQUIRED" if failed else "ROLLED_BACK"
    _save(directory, journal)
    return {"result": journal["state"]}


def transaction(root, fn, *, ws=None, project=None):
    """Run writer-mediated edits, keeping durable undo until the entire operation succeeds."""
    from . import mirror
    if ws is None or project is None:
        raise TypeError("transaction requires ws and project")
    mirror.anchor_mirror(ws)
    parent = mirror._transaction_dir(ws, project)
    lock = os.path.join(parent, ".commit-lock")
    _contained(lock, ws.private)
    os.makedirs(parent, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise PbzError("COMMIT_BUSY", "project commit lock is held") from None
    token = None
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        if mirror.pending_recovery(ws, project):
            raise PbzError("RECOVERY_REQUIRED", project.name)
        directory = os.path.join(parent, "edit-" + uuid.uuid4().hex)
        os.makedirs(directory)
        journal = {"version": 1, "kind": "mirror_edit", "state": "APPLYING", "entries": []}
        _save(directory, journal)
        token = _active.set((ws, project, directory, journal))
        try:
            result = fn()
            journal["state"] = "COMMITTED"
            _save(directory, journal)
            shutil.rmtree(directory, ignore_errors=True)
            return result
        except BaseException:
            try:
                restored = restore(ws, project, directory, journal)
            except (OSError, PbzError):
                raise PbzError("RECOVERY_REQUIRED", project.name) from None
            if restored["result"] != "ROLLED_BACK":
                raise PbzError("RECOVERY_REQUIRED", project.name) from None
            shutil.rmtree(directory, ignore_errors=True)
            raise
    finally:
        if token is not None:
            _active.reset(token)
        remove_file(lock, root=ws.private)
