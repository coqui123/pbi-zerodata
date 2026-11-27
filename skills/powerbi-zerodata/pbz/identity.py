"""Manage workspace project identity anchors and the registry.
Resolve identities for ordinary snapshot operations and account for namespaces.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

from .common import PbzError, _contained, _reparse_link, pbz_state_path, read_checked

ANCHOR_NAME = "pbz-project-id.json"
REGISTRY_NAME = "project-identity.json"
REPAIR_INTENT_NAME = "repair-intent.json"
UUID_RX = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_RECORD_FIELDS = {"id", "name", "previous_names", "status"}
_STATUSES = ("bound", "orphan", "quarantined")


def is_uuid(text) -> bool:
    return isinstance(text, str) and UUID_RX.fullmatch(text) is not None


def _durable_replace(path, data: bytes, *, root) -> None:
    tmp = None
    try:
        if _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        _contained(path, root)
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        if _reparse_link(parent) or _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")

        fd, tmp = tempfile.mkstemp(prefix=".pbz-", suffix=".pbz-tmp", dir=parent)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("write made no progress")
                view = view[written:]
            os.fsync(fd)
            opened = os.fstat(fd)
        finally:
            os.close(fd)

        current = os.lstat(tmp)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or _reparse_link(tmp):
            os.unlink(tmp)
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        os.replace(tmp, path)
        tmp = None

        if os.name != "nt":
            try:
                fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                if hasattr(os, "O_DIRECTORY"):
                    raise
                return
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as exc:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise PbzError("LOCAL_STATE_UNAVAILABLE", "identity state write failed") from exc


def anchor_path(project) -> str:
    return os.path.join(project.model_dir, ANCHOR_NAME)


def read_anchor(ws, project) -> str | None:
    path = anchor_path(project)
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to read through a link")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"project anchor cannot be inspected: {error}") from error
    if not stat.S_ISREG(info.st_mode):
        raise PbzError("CORRUPT_STATE", "project anchor is not a file")
    try:
        raw = read_checked(path, root=ws.root)
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"project anchor cannot be read: {error}") from error
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PbzError("CORRUPT_STATE", "project anchor is unparseable") from exc
    if (
        not isinstance(doc, dict)
        or not is_uuid(doc.get("id"))
        or type(doc.get("version")) is not int
        or doc["version"] != 1
    ):
        raise PbzError("CORRUPT_STATE", "project anchor is malformed")
    return doc["id"]


def write_anchor(ws, project, project_id) -> None:
    data = json.dumps({"id": project_id, "version": 1}).encode("utf-8") + b"\n"
    _durable_replace(anchor_path(project), data, root=ws.root)


def registry_path(ws) -> str:
    return pbz_state_path(ws, REGISTRY_NAME)


def repair_intent_path(ws) -> str:
    return pbz_state_path(ws, REPAIR_INTENT_NAME)


def load_registry(ws) -> dict | None:
    path = registry_path(ws)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"project identity registry cannot be inspected: {error}") from error
    if not stat.S_ISREG(info.st_mode):
        raise PbzError("CORRUPT_STATE", "project identity registry is not a file")
    try:
        raw = read_checked(path, root=ws.root)
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"project identity registry cannot be read: {error}") from error
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PbzError("CORRUPT_STATE", "project identity registry is unparseable") from exc
    if not isinstance(doc, dict):
        raise PbzError("CORRUPT_STATE", "project identity registry is malformed")
    for project_id, record in doc.items():
        if (
            not is_uuid(project_id)
            or not isinstance(record, dict)
            or record.keys() != _RECORD_FIELDS
            or record["id"] != project_id
            or record["status"] not in _STATUSES
            or not isinstance(record["previous_names"], list)
            or not all(isinstance(name, str) for name in record["previous_names"])
        ):
            raise PbzError("CORRUPT_STATE", "project identity registry is malformed")
        name = record["name"]
        if record["status"] == "quarantined":
            valid_name = name is None
        else:
            valid_name = isinstance(name, str) and bool(name)
        if not valid_name:
            raise PbzError("CORRUPT_STATE", "project identity registry is malformed")
    return doc


def write_registry(ws, doc) -> None:
    data = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _durable_replace(registry_path(ws), data, root=ws.root)


_lock_local = threading.local()


@contextmanager
def identity_lock(ws):
    from .checks import _StateLock

    key = os.path.normcase(os.path.abspath(ws.root))
    counts = getattr(_lock_local, "counts", None)
    if counts is None:
        counts = _lock_local.counts = {}
    if counts.get(key):
        # Reentrant for the holding thread: resolve() re-enters to re-observe
        # state while an operation already holds the lock.
        counts[key] += 1
        try:
            yield
        finally:
            counts[key] -= 1
        return
    directory = os.path.join(ws.root, ".pbz")
    with _StateLock(SimpleNamespace(private=directory), "project-identity.lock"):
        counts[key] = 1
        try:
            yield
        finally:
            counts.pop(key, None)


def discover_anchors(ws) -> dict[str, str | None]:
    return {project.name.lower(): read_anchor(ws, project) for project in ws.projects}


def bound_record_for_name(registry, name) -> dict | None:
    if registry is not None:
        for record in registry.values():
            if record["status"] == "bound" and record["name"].lower() == name.lower():
                return record
    return None


def record_for_name_any_status(registry, name) -> dict | None:
    if registry is not None:
        for record in registry.values():
            if record["status"] != "quarantined" and record["name"].lower() == name.lower():
                return record
    return None


def _namespace_entries(directory: str) -> set[str]:
    # Explicit stats, never os.path.isdir: isdir reports False both for an
    # absent entry and for one the process may not inspect, and an
    # uninspectable namespace must refuse rather than read as accounted for.
    try:
        root_stat = os.stat(directory)
    except FileNotFoundError:
        return set()
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"snapshot namespaces cannot be inspected: {error}") from error
    if not stat.S_ISDIR(root_stat.st_mode):
        return set()
    try:
        entries = os.listdir(directory)
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"snapshot namespaces cannot be listed: {error}") from error
    found = set()
    for entry in entries:
        if not is_uuid(entry):
            continue
        try:
            child = os.stat(os.path.join(directory, entry))
        except FileNotFoundError:
            continue
        except OSError as error:
            raise PbzError("LOCAL_STATE_UNAVAILABLE", f"snapshot namespaces cannot be inspected: {error}") from error
        if stat.S_ISDIR(child.st_mode):
            found.add(entry)
    return found


def namespace_ids(ws) -> set[str]:
    return _namespace_entries(os.path.join(ws.private, "snapshots", "projects"))


def quarantine_ids(ws) -> set[str]:
    return _namespace_entries(os.path.join(ws.private, "snapshots", "quarantine"))


def unaccounted_namespaces(ws, registry, anchors) -> list[str]:
    accounted = set(anchors.values())
    if registry is not None:
        accounted.update(registry)
    return sorted(namespace_ids(ws) - accounted)


class Resolution:
    def __init__(self, project_id, action, renamed_from=None, orphan_marked=None):
        self.project_id = project_id
        self.action = action
        self.renamed_from = renamed_from
        self.orphan_marked = orphan_marked


def resolve(ws, project, *, apply: bool) -> Resolution:
    try:
        return _resolve_inner(ws, project, apply=apply)
    except PbzError:
        if apply:
            raise
        # The unlocked observation can straddle a concurrent first use: the
        # anchor, the registry and the namespace set are published at
        # different moments under the identity lock, so a refusal may rest on
        # a stale half of a mint that has since completed. Re-observe under
        # the identity lock and report the locked outcome instead; an
        # inconsistency that persists under the lock is genuine.
        with identity_lock(ws):
            return _resolve_inner(ws, project, apply=False)


def _resolve_inner(ws, project, *, apply: bool) -> Resolution:
    anchors = discover_anchors(ws)
    anchor_id = anchors.get(project.name.lower())
    registry = load_registry(ws)
    projects_by_anchor = {}
    for name, project_id in anchors.items():
        if project_id is not None:
            projects_by_anchor.setdefault(project_id, []).append(name)

    if anchor_id is not None:
        if registry is None or anchor_id not in registry:
            raise PbzError("REGISTRY_MISSING", f"project {project.name} has an identity anchor ({anchor_id}) with no registry record; run pbz snapshot rebind-registry")
        record = registry[anchor_id]
        if len(projects_by_anchor.get(anchor_id, ())) > 1:
            raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"identity {anchor_id} is anchored by more than one project; run pbz snapshot reidentify --project <holder> for each extra holder")
        if record["status"] == "orphan":
            raise PbzError("CORRUPT_STATE", f"identity {anchor_id} is marked orphan while its anchor is present; run pbz snapshot rebind-registry")
        if record["status"] == "quarantined":
            raise PbzError("PROJECT_IDENTITY_UNBOUND", f"identity {anchor_id} is quarantined; restore it with pbz snapshot bind --project {project.name} --identity {anchor_id}")

        holder = bound_record_for_name(registry, project.name)
        ghost = None
        if holder is not None and holder["id"] != anchor_id:
            if holder["id"] in projects_by_anchor:
                raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"the name {project.name} is recorded for identity {holder['id']}, which is anchored by another live project; run pbz snapshot resolve-identity --project {project.name}")
            ghost = holder
        if record["name"] != project.name or ghost is not None:
            renamed_from = record["name"]
            orphan_marked = None
            if apply:
                if renamed_from != project.name:
                    if renamed_from is not None and renamed_from not in record["previous_names"]:
                        record["previous_names"] = [*record["previous_names"], renamed_from]
                    record["name"] = project.name
                if ghost is not None:
                    ghost["status"] = "orphan"
                    orphan_marked = (ghost["id"], ghost["name"])
                write_registry(ws, registry)
            elif ghost is not None:
                orphan_marked = (ghost["id"], ghost["name"])
            return Resolution(anchor_id, "refresh", renamed_from if renamed_from != project.name else None, orphan_marked)
        return Resolution(anchor_id, "bound")

    holder = bound_record_for_name(registry, project.name) if registry else None
    if holder is not None:
        if holder["id"] in projects_by_anchor:
            raise PbzError("PROJECT_IDENTITY_UNBOUND", f"the name {project.name} is recorded for identity {holder['id']}, which is anchored by another live project; if this is a genuinely new project, run pbz snapshot mint-fresh --project {project.name}")
        raise PbzError("PROJECT_IDENTITY_UNBOUND", f"project {project.name} has no identity anchor but identity {holder['id']} is recorded under its name and anchored nowhere; run pbz snapshot bind --project {project.name}")

    orphan = record_for_name_any_status(registry, project.name) if registry else None
    if orphan is not None and orphan["status"] == "orphan" and orphan["id"] not in projects_by_anchor:
        raise PbzError("PROJECT_IDENTITY_UNBOUND", f"project {project.name} has no identity anchor; orphaned identity {orphan['id']} is recorded under its name; run pbz snapshot bind --project {project.name} or pbz snapshot mint-fresh --project {project.name}")

    unaccounted = unaccounted_namespaces(ws, registry, anchors)
    if unaccounted:
        raise PbzError("CORRUPT_STATE", f"snapshot namespace {unaccounted[0]} is not accounted for by any anchor or registry record; account for it with pbz snapshot bind --identity, pbz snapshot quarantine-namespace --identity {unaccounted[0]}, or pbz snapshot rebind-registry before minting a new identity")
    if not apply:
        return Resolution(None, "mint")

    new_id = str(uuid.uuid4())
    write_anchor(ws, project, new_id)
    doc = dict(registry) if registry else {}
    doc[new_id] = {"id": new_id, "name": project.name, "previous_names": [], "status": "bound"}
    write_registry(ws, doc)
    return Resolution(new_id, "mint")
