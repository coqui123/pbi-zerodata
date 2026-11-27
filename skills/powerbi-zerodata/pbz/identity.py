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
    if apply:
        # Identity writes (minting, rename refresh, ghost marking) are
        # serialized by the workspace identity lock, so two first uses
        # under different private roots cannot lose each other's registry
        # records. Callers that already hold the lock re-enter it.
        with identity_lock(ws):
            return _resolve_inner(ws, project, apply=True)
    try:
        return _resolve_inner(ws, project, apply=False)
    except PbzError:
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


# ---------------------------------------------------------------------------
# Repair commands (S1 C3). Every function here is a library entry point for
# one human-only CLI command: it takes the workspace identity lock, presents
# its plan through confirm_fn (which must return True before any write), and
# refuses before printing or changing anything when its state row does not
# hold. The CLI layer owns the HUMAN_ONLY gate and the typed confirmation.
# ---------------------------------------------------------------------------

import hashlib
import hmac as _hmac


def chain_key(ws, *, create: bool):
    """The workspace evidence chain key, or None when none exists.

    With create=True the key is created in the private store (the caller
    holds the identity lock); failure to create or load refuses with
    ENGINE_UNAVAILABLE, per the approved design.
    """
    from . import dpapi

    try:
        return dpapi.load_key(ws.private, create=create)
    except FileNotFoundError:
        return None
    except PbzError:
        raise
    except Exception as error:
        raise PbzError("ENGINE_UNAVAILABLE", f"workspace evidence key is unavailable: {error}") from error


def sign_doc(ws, doc) -> dict:
    """A copy of doc carrying an HMAC-SHA256 over its canonical contents,
    keyed by the workspace evidence chain key (created on first use)."""
    key = chain_key(ws, create=True)
    if key is None:
        raise PbzError("ENGINE_UNAVAILABLE", "workspace evidence key could not be created")
    body = {field: value for field, value in doc.items() if field != "hmac"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signed = dict(body)
    signed["hmac"] = _hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return signed


def verify_doc(ws, doc) -> bool:
    if not isinstance(doc, dict) or not isinstance(doc.get("hmac"), str):
        return False
    key = chain_key(ws, create=False)
    if key is None:
        return False
    body = {field: value for field, value in doc.items() if field != "hmac"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = _hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected.encode("ascii"), doc["hmac"].encode("utf-8", "replace"))


def mint_with_id(ws, project, new_id) -> None:
    """Mint a specific identity for a genuinely new project: anchor first,
    registry record second, under the caller's identity lock."""
    write_anchor(ws, project, new_id)
    registry = load_registry(ws)
    doc = dict(registry) if registry else {}
    doc[new_id] = {"id": new_id, "name": project.name, "previous_names": [], "status": "bound"}
    write_registry(ws, doc)


def _authenticated_write(ws, path, doc) -> None:
    signed = sign_doc(ws, doc)
    data = (json.dumps(signed, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _durable_replace(path, data, root=ws.root)


def _authenticated_read(ws, path):
    """(status, doc): status is 'absent', 'ok', or 'unverifiable'."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent", None
    except OSError as error:
        raise PbzError("LOCAL_STATE_UNAVAILABLE", f"authenticated state cannot be inspected: {error}") from error
    if not stat.S_ISREG(info.st_mode):
        return "unverifiable", None
    try:
        raw = read_checked(path, root=ws.root)
    except PbzError:
        return "unverifiable", None
    except OSError:
        return "unverifiable", None
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "unverifiable", None
    if not isinstance(doc, dict) or not isinstance(doc.get("hmac"), str):
        return "unverifiable", None
    if not verify_doc(ws, doc):
        return "unverifiable", None
    return "ok", {field: value for field, value in doc.items() if field != "hmac"}


def read_repair_intent(ws):
    """The verified repair intent, None when absent.

    A present but unverifiable intent is CORRUPT_STATE: it is evidence of a
    repair in progress, and the remedy is rebind-registry.
    """
    status, doc = _authenticated_read(ws, repair_intent_path(ws))
    if status == "absent":
        return None
    if status != "ok":
        raise PbzError("CORRUPT_STATE", f"repair intent at {repair_intent_path(ws)} cannot be verified; run pbz snapshot rebind-registry")
    return doc


def _write_repair_intent(ws, command, target, before, after) -> None:
    _authenticated_write(ws, repair_intent_path(ws), {
        "version": 1, "command": command, "target": target,
        "before": before, "after": after,
    })


def _clear_repair_intent(ws) -> None:
    try:
        os.unlink(repair_intent_path(ws))
    except FileNotFoundError:
        pass


def _require_no_foreign_intent(ws, command, target):
    intent = read_repair_intent(ws)
    if intent is None:
        return None
    if intent.get("command") == command and intent.get("target") == target:
        return intent
    raise PbzError("CORRUPT_STATE", f"a repair intent for {intent.get('command')} on {intent.get('target')} is recorded at {repair_intent_path(ws)}; complete it or run pbz snapshot rebind-registry")


def _changed_identities(before: dict, after: dict) -> list[str]:
    """Identities whose registry record differs between two mappings:
    exactly the set a write from before to after must exclusively guard."""
    return sorted(pid for pid in set(before) | set(after) if before.get(pid) != after.get(pid))


def _registry_snapshot(registry) -> dict:
    return {project_id: dict(record) for project_id, record in (registry or {}).items()}


@contextmanager
def _exclusive_guards(ws, project_ids):
    """Exclusive try-locks on the guards of the given identities, held by the
    caller through its registry write. Identities with no namespace in this
    store have no guard to take and no local operation to exclude."""
    from . import checks

    present = namespace_ids(ws) | quarantine_ids(ws)
    handles = []
    try:
        for project_id in sorted(set(project_ids)):
            if project_id not in present:
                continue
            handle = checks._acquire_project_guard(ws, project_id, mode="try_exclusive")
            if handle is None:
                raise PbzError("IDENTITY_IN_USE", f"identity {project_id} is in use by a running snapshot operation")
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            checks._snapshot_close_handle(handle)


def namespace_snapshot_names(ws, project_id, *, quarantined=False) -> list[str]:
    """Snapshot names held by one namespace (names only, never contents)."""
    names = set()
    for sub in ("results", "specs"):
        directory = os.path.join(ws.private, "snapshots", "quarantine" if quarantined else "projects", project_id, sub)
        try:
            entries = os.listdir(directory)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise PbzError("LOCAL_STATE_UNAVAILABLE", f"namespace {project_id} cannot be listed: {error}") from error
        for entry in entries:
            if entry.endswith(".json"):
                names.add(entry[:-5])
    return sorted(names)


def _declined(command):
    return {"ok": False, "confirmed": False, "command": command}


def bind_project(ws, target, identity_id=None, *, confirm_fn) -> dict:
    """bind: attach an orphaned or unaccounted identity to an anchorless
    project (state table rows 5, 6, and the quarantine restore of row 12)."""
    with identity_lock(ws):
        anchors = discover_anchors(ws)
        registry = load_registry(ws)
        target_anchor = anchors.get(target.name.lower())
        projects = {project.name.lower(): project for project in ws.projects}

        if identity_id is not None:
            if not is_uuid(identity_id):
                raise PbzError("BAD_REQUEST", "--identity must be a project identity UUID")
            chosen = identity_id
        else:
            record = bound_record_for_name(registry, target.name) or record_for_name_any_status(registry, target.name)
            if record is None:
                raise PbzError("PROJECT_IDENTITY_UNBOUND", f"no orphaned identity is recorded under the name {target.name}; name one with --identity <id>")
            chosen = record["id"]

        record = (registry or {}).get(chosen)
        holders = sorted(name for name, value in anchors.items() if value == chosen)
        if len(holders) > 1 or (holders and holders[0] != target.name.lower()):
            raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"identity {chosen} is anchored by {', '.join(projects[name].name for name in holders)}; bind cannot bind a duplicated or foreign identity, run pbz snapshot reidentify for each extra holder")
        existing_named = bound_record_for_name(registry, target.name)
        if existing_named is not None and existing_named["id"] != chosen:
            raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"project {target.name} is already bound to identity {existing_named['id']}; if its anchor disagrees run pbz snapshot resolve-identity, to start over run pbz snapshot mint-fresh")
        if record is not None and record["status"] == "bound" and record["name"] != target.name:
            raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"identity {chosen} is already bound to project {record['name']}; bind cannot reassign a live identity")
        if target_anchor is not None and target_anchor != chosen:
            raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"project {target.name} already carries anchor {target_anchor}; a different identity cannot be bound over it (see pbz snapshot resolve-identity)")

        quarantined = chosen in quarantine_ids(ws)
        in_projects = chosen in namespace_ids(ws)
        intent = _require_no_foreign_intent(ws, "bind", target.name)
        before = _registry_snapshot(registry)
        after = _registry_snapshot(registry)
        new_record = dict(record) if record is not None else {"id": chosen, "name": None, "previous_names": [], "status": "orphan"}
        if new_record.get("name") not in (None, target.name):
            history = list(new_record.get("previous_names") or [])
            if new_record["name"] not in history:
                history.append(new_record["name"])
            new_record["previous_names"] = history
        new_record["name"] = target.name
        new_record["status"] = "bound"
        after[chosen] = new_record

        if quarantined and in_projects:
            raise PbzError("CORRUPT_STATE", f"identity {chosen} has a namespace in both the active store and quarantine; resolve the duplication before binding")
        if record is not None and record["status"] == "quarantined" and not quarantined:
            # A crash after the restore rename but before the registry
            # write leaves exactly this state; only the matching bind
            # intent plus the restored namespace in the active store
            # distinguish it from corruption.
            if not (intent is not None and (intent.get("after") or {}) == after and in_projects):
                raise PbzError("CORRUPT_STATE", f"identity {chosen} is quarantined but its namespace is not in quarantine")
        if record is None and not in_projects and not quarantined:
            raise PbzError("PROJECT_IDENTITY_UNBOUND", f"identity {chosen} has no registry record and no namespace; there is nothing to bind")
        restoring = quarantined and not in_projects
        if restoring and os.path.lexists(os.path.join(ws.private, "snapshots", "projects", chosen)):
            raise PbzError("CONFLICT", f"the namespace destination for identity {chosen} is occupied; the quarantined namespace was left in place")

        plan = {
            "command": "bind", "target": target.name, "identity": chosen,
            "recorded_name": record["name"] if record else None,
            "previous_names": list(new_record["previous_names"]),
            "namespace_names": namespace_snapshot_names(ws, chosen, quarantined=restoring),
            "restore_from_quarantine": restoring,
            "resume": intent is not None or target_anchor == chosen,
        }
        projected_anchors = dict(anchors)
        projected_anchors[target.name.lower()] = chosen
        problems = _validate_registry_mapping(ws, after, projected_anchors, {chosen})
        if problems:
            raise PbzError("CORRUPT_STATE", "bind refused: " + "; ".join(problems))
        if not confirm_fn(plan):
            return _declined("bind")
        with _exclusive_guards(ws, [chosen]):
            _write_repair_intent(ws, "bind", target.name, before, after)
            if restoring:
                os.rename(os.path.join(ws.private, "snapshots", "quarantine", chosen),
                          os.path.join(ws.private, "snapshots", "projects", chosen))
            if target_anchor != chosen:
                write_anchor(ws, target, chosen)
            write_registry(ws, after)
        _clear_repair_intent(ws)
        return {"ok": True, "confirmed": True, **plan}


def mint_fresh(ws, target, *, confirm_fn) -> dict:
    """mint-fresh: start an anchorless project under a fresh identity,
    leaving the same-name orphan frozen (state table row 11)."""
    with identity_lock(ws):
        anchors = discover_anchors(ws)
        registry = load_registry(ws)
        anchored_ids = {value for value in anchors.values() if value is not None}
        target_anchor = anchors.get(target.name.lower())
        old = bound_record_for_name(registry, target.name) or record_for_name_any_status(registry, target.name)
        intent = read_repair_intent(ws)
        fresh_id = None
        if intent is not None:
            if intent.get("command") != "mint-fresh" or intent.get("target") != target.name:
                raise PbzError("CORRUPT_STATE", f"a repair intent for {intent.get('command')} on {intent.get('target')} is recorded at {repair_intent_path(ws)}; complete it or run pbz snapshot rebind-registry")
            after = intent.get("after") or {}
            before = intent.get("before") or {}
            fresh_id = next((candidate for candidate in after
                             if candidate not in before and after[candidate].get("name") == target.name), None)
            current = _registry_snapshot(registry)
            if fresh_id is not None and current == after and target_anchor == fresh_id:
                _clear_repair_intent(ws)
                return {"ok": True, "confirmed": True, "command": "mint-fresh", "target": target.name,
                        "identity": fresh_id, "resumed": "completed"}
            if fresh_id is not None and current == before and target_anchor == fresh_id:
                plan = {"command": "mint-fresh", "target": target.name, "identity": fresh_id,
                        "orphaned_identity": old["id"] if old else None, "resume": True}
                if not confirm_fn(plan):
                    return _declined("mint-fresh")
                with _exclusive_guards(ws, _changed_identities(before, after)):
                    write_registry(ws, after)
                _clear_repair_intent(ws)
                return {"ok": True, "confirmed": True, **plan}
            if not (current == before and target_anchor is None):
                raise PbzError("CORRUPT_STATE", "the workspace state matches neither the recorded mint-fresh intent nor its completion; run pbz snapshot rebind-registry")
            # Before-state: proceed with the intent's recorded fresh id.

        if target_anchor is not None and fresh_id is None:
            if (registry or {}).get(target_anchor) is not None:
                raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"project {target.name} is already bound to identity {target_anchor}")
            if sum(1 for value in anchors.values() if value == target_anchor) > 1:
                raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"identity {target_anchor} is anchored by more than one project; run pbz snapshot reidentify")
            fresh_id = target_anchor
        if old is None and fresh_id is None:
            raise PbzError("PROJECT_IDENTITY_UNBOUND", f"no identity is recorded under the name {target.name}; a genuinely new project mints on first use, and mint-fresh applies only when an orphaned identity holds the name")
        if old is not None and old["id"] in anchored_ids and old["id"] != target_anchor:
            raise PbzError("CORRUPT_STATE", f"identity {old['id']} recorded under {target.name} is anchored by a live project; this state has no mint-fresh remedy")

        if fresh_id is None:
            fresh_id = str(uuid.uuid4())
        before = _registry_snapshot(registry)
        after = _registry_snapshot(registry)
        if old is not None:
            marked = dict(after[old["id"]])
            marked["status"] = "orphan"
            after[old["id"]] = marked
        after[fresh_id] = {"id": fresh_id, "name": target.name, "previous_names": [], "status": "bound"}
        plan = {
            "command": "mint-fresh", "target": target.name, "identity": fresh_id,
            "orphaned_identity": old["id"] if old else None,
            "orphaned_names": namespace_snapshot_names(ws, old["id"]) if old else [],
            "resume": target_anchor is not None,
        }
        if not confirm_fn(plan):
            return _declined("mint-fresh")
        with _exclusive_guards(ws, _changed_identities(before, after)):
            _write_repair_intent(ws, "mint-fresh", target.name, before, after)
            if target_anchor != fresh_id:
                write_anchor(ws, target, fresh_id)
            write_registry(ws, after)
        _clear_repair_intent(ws)
        return {"ok": True, "confirmed": True, **plan}


def reidentify(ws, target, keeper=None, *, confirm_fn) -> dict:
    """reidentify: give one holder of a duplicated anchor identity a fresh
    identity (state table row 7)."""
    with identity_lock(ws):
        anchors = discover_anchors(ws)
        registry = load_registry(ws)
        target_anchor = anchors.get(target.name.lower())
        intent = read_repair_intent(ws)
        fresh_id = None
        if intent is not None:
            if intent.get("command") != "reidentify" or intent.get("target") != target.name:
                raise PbzError("CORRUPT_STATE", f"a repair intent for {intent.get('command')} on {intent.get('target')} is recorded at {repair_intent_path(ws)}; complete it or run pbz snapshot rebind-registry")
            after = intent.get("after") or {}
            before = intent.get("before") or {}
            fresh_id = next((candidate for candidate in after
                             if candidate not in before and after[candidate].get("name") == target.name), None)
            current = _registry_snapshot(registry)
            if fresh_id is not None and current == after and target_anchor == fresh_id:
                _clear_repair_intent(ws)
                return {"ok": True, "confirmed": True, "command": "reidentify", "target": target.name,
                        "identity": fresh_id, "resumed": "completed"}
            if fresh_id is not None and current == before and target_anchor == fresh_id:
                plan = {"command": "reidentify", "target": target.name, "identity": fresh_id, "resume": True}
                if not confirm_fn(plan):
                    return _declined("reidentify")
                with _exclusive_guards(ws, _changed_identities(before, after)):
                    write_registry(ws, after)
                _clear_repair_intent(ws)
                return {"ok": True, "confirmed": True, **plan}
            if not (current == before and target_anchor is not None and target_anchor != fresh_id):
                raise PbzError("CORRUPT_STATE", "the workspace state matches neither the recorded reidentify intent nor its completion; run pbz snapshot rebind-registry")
            # Before-state with the duplicated anchor still in place:
            # proceed, reusing the intent's recorded fresh id below.

        named = bound_record_for_name(registry, target.name)
        if named is not None and named["id"] != target_anchor:
            raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"a different identity {named['id']} is bound under the name {target.name}; this is the resolve-identity state, not reidentify")
        if target_anchor is None:
            raise PbzError("PROJECT_IDENTITY_UNBOUND", f"project {target.name} has no identity anchor; reidentify applies to a holder of a duplicated identity")
        holders = sorted(name for name, value in anchors.items() if value == target_anchor)
        if len(holders) < 2:
            raise PbzError("PROJECT_IDENTITY_UNBOUND", f"identity {target_anchor} is anchored by only one project; there is no duplicate to reidentify")
        record = (registry or {}).get(target_anchor)
        projects = {project.name.lower(): project for project in ws.projects}
        if record is None:
            # No record names a holder, so no keeper repointing is needed:
            # the target takes a fresh identity and only its own record is
            # added. rebind-registry can then record the remaining holder.
            # (Refusing here deadlocked the repair: rebind refuses a
            # duplicated identity and directs the owner to this command.)
            before = _registry_snapshot(registry)
            after = _registry_snapshot(registry)
            if fresh_id is None:
                fresh_id = str(uuid.uuid4())
            after[fresh_id] = {"id": fresh_id, "name": target.name, "previous_names": [], "status": "bound"}
            plan = {
                "command": "reidentify", "target": target.name, "identity": fresh_id,
                "duplicated_identity": target_anchor,
                "holders": [projects[name].name for name in holders],
                "recorded_holder": None, "keeper": None, "registry_record_missing": True,
            }
            if not confirm_fn(plan):
                return _declined("reidentify")
            with _exclusive_guards(ws, [target_anchor]):
                _write_repair_intent(ws, "reidentify", target.name, before, after)
                write_anchor(ws, target, fresh_id)
                write_registry(ws, after)
            _clear_repair_intent(ws)
            return {"ok": True, "confirmed": True, **plan}
        recorded_holder = record["name"].lower() if record["name"] else None
        target_is_recorded = recorded_holder == target.name.lower()
        keeper_project = None
        if target_is_recorded:
            if keeper is None:
                raise PbzError("BAD_REQUEST", f"project {target.name} is the holder the record names; name the keeper of identity {target_anchor} with --keeper <project>")
            keeper_project = projects.get(keeper.lower())
            if keeper_project is None or keeper.lower() not in holders or keeper.lower() == target.name.lower():
                raise PbzError("BAD_REQUEST", "--keeper must name one of the other projects holding this identity")
            keeper_record = bound_record_for_name(registry, keeper_project.name)
            if keeper_record is not None and keeper_record["id"] != target_anchor:
                raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"project {keeper_project.name} already has identity {keeper_record['id']} bound under its name; the duplicated identity cannot be repointed to it")

        if fresh_id is None:
            fresh_id = str(uuid.uuid4())
        before = _registry_snapshot(registry)
        after = _registry_snapshot(registry)
        if target_is_recorded:
            repointed = dict(after[target_anchor])
            if repointed["name"] not in (None, keeper_project.name):
                history = list(repointed.get("previous_names") or [])
                if repointed["name"] not in history:
                    history.append(repointed["name"])
                repointed["previous_names"] = history
            repointed["name"] = keeper_project.name
            after[target_anchor] = repointed
        after[fresh_id] = {"id": fresh_id, "name": target.name, "previous_names": [], "status": "bound"}
        plan = {
            "command": "reidentify", "target": target.name, "identity": fresh_id,
            "duplicated_identity": target_anchor,
            "holders": [projects[name].name for name in holders],
            "recorded_holder": record["name"],
            "keeper": keeper_project.name if keeper_project else None,
        }
        projected_anchors = dict(anchors)
        projected_anchors[target.name.lower()] = fresh_id
        problems = _validate_registry_mapping(ws, after, projected_anchors, {target_anchor, fresh_id})
        if problems:
            raise PbzError("CORRUPT_STATE", "reidentify refused: " + "; ".join(problems))
        if not confirm_fn(plan):
            return _declined("reidentify")
        with _exclusive_guards(ws, [target_anchor]):
            _write_repair_intent(ws, "reidentify", target.name, before, after)
            write_anchor(ws, target, fresh_id)
            write_registry(ws, after)
        _clear_repair_intent(ws)
        return {"ok": True, "confirmed": True, **plan}


def resolve_identity(ws, target, *, confirm_fn) -> dict:
    """resolve-identity: correct the record held under a name to the
    anchor's identity, orphaning the displaced record (state table row 8)."""
    with identity_lock(ws):
        intent = read_repair_intent(ws)
        if intent is not None:
            raise PbzError("CORRUPT_STATE", f"a repair intent for {intent.get('command')} on {intent.get('target')} is recorded at {repair_intent_path(ws)}; complete it or run pbz snapshot rebind-registry")
        anchors = discover_anchors(ws)
        registry = load_registry(ws)
        anchored_ids = {value for value in anchors.values() if value is not None}
        target_anchor = anchors.get(target.name.lower())
        if target_anchor is None:
            raise PbzError("PROJECT_IDENTITY_UNBOUND", f"project {target.name} has no identity anchor; resolve-identity corrects a record to an existing anchor")
        holders = sorted(name for name, value in anchors.items() if value == target_anchor)
        if len(holders) > 1:
            raise PbzError("PROJECT_IDENTITY_DUPLICATE", f"identity {target_anchor} is anchored by more than one live project; run pbz snapshot reidentify for each extra holder before resolving")
        holder = bound_record_for_name(registry, target.name)
        if holder is None or holder["id"] == target_anchor:
            raise PbzError("PROJECT_IDENTITY_UNBOUND", f"no bound record under the name {target.name} disagrees with its anchor; nothing to resolve")
        if holder["id"] in anchored_ids:
            raise PbzError("CORRUPT_STATE", f"the displaced identity {holder['id']} is anchored by another live project; resolve-identity changes nothing in the swapped-folders state")

        before = _registry_snapshot(registry)
        after = _registry_snapshot(registry)
        displaced = dict(after[holder["id"]])
        displaced["status"] = "orphan"
        after[holder["id"]] = displaced
        existing = after.get(target_anchor)
        if existing is None:
            after[target_anchor] = {"id": target_anchor, "name": target.name, "previous_names": [], "status": "bound"}
        else:
            corrected = dict(existing)
            if corrected["name"] not in (None, target.name):
                history = list(corrected.get("previous_names") or [])
                if corrected["name"] not in history:
                    history.append(corrected["name"])
                corrected["previous_names"] = history
            corrected["name"] = target.name
            corrected["status"] = "bound"
            after[target_anchor] = corrected
        plan = {
            "command": "resolve-identity", "target": target.name,
            "identity": target_anchor, "displaced_identity": holder["id"],
        }
        if not confirm_fn(plan):
            return _declined("resolve-identity")
        with _exclusive_guards(ws, [target_anchor, holder["id"]]):
            write_registry(ws, after)
        return {"ok": True, "confirmed": True, **plan}


def _validate_registry_mapping(ws, proposed, anchors, changed_ids) -> list[str]:
    """Postcondition for the bindings a rebuild itself establishes: no
    added or flipped record may create a second bound record under a live
    project name, and its name must be the live project whose anchor
    carries its id. Pre-existing conflicts the rebuild does not touch are
    listed by the caller, not validated here."""
    problems = []
    live = {project.name.lower(): project for project in ws.projects}
    bound_names = {}
    for project_id, record in proposed.items():
        if record["status"] == "bound" and record["name"] is not None:
            bound_names.setdefault(record["name"].lower(), []).append(project_id)
    for project_id in changed_ids:
        record = proposed[project_id]
        if record["status"] != "bound":
            continue
        name = record["name"]
        if name is None or name.lower() not in live:
            problems.append(f"rebuilt record {project_id} names {name}, which is not a live project")
            continue
        if anchors.get(name.lower()) != project_id:
            problems.append(f"rebuilt record {project_id} names {name}, whose anchor carries {anchors.get(name.lower())}")
        claimants = bound_names.get(name.lower(), [])
        if len(claimants) > 1:
            problems.append(f"the rebuild would leave two bound records claiming the name {name} ({' and '.join(claimants)}); resolve the name's own row first")
    return problems


def rebind_registry(ws, *, confirm_fn) -> dict:
    """rebind-registry: the owner-confirmed workspace-wide registry repair
    (state table rows 4, 10, and 13). Adds records for anchored identities,
    flips row 13 orphans, and never deletes or renames a bound record."""
    with identity_lock(ws):
        anchors = discover_anchors(ws)
        anchored = {value: name for name, value in anchors.items() if value is not None}
        holders_by_id: dict[str, list[str]] = {}
        for holder_name, value in anchors.items():
            if value is not None:
                holders_by_id.setdefault(value, []).append(holder_name)
        duplicated = {value for value, names in holders_by_id.items() if len(names) > 1}
        projects = {project.name.lower(): project for project in ws.projects}
        # rebind-registry is the remedy an unverifiable intent names, so it
        # lists the intent instead of raising on it.
        intent_status, intent_doc = _authenticated_read(ws, repair_intent_path(ws))
        intent = intent_doc if intent_status == "ok" else None
        intent_unverifiable = intent_status == "unverifiable"

        corrupt_path = None
        try:
            registry = load_registry(ws)
            unparseable = False
        except PbzError as error:
            if error.code != "CORRUPT_STATE":
                raise
            registry = None
            unparseable = True
            raw = read_checked(registry_path(ws), root=ws.root)
            digest = hashlib.sha256(raw).hexdigest()
            corrupt_path = pbz_state_path(ws, f"project-identity.corrupt-{digest}.json")

        changes = []
        changed_ids = set()
        conflicts = []
        if unparseable or registry is None:
            # Missing-file rebuild (an unparseable file is quarantined first,
            # inside this confirmed run, then treated as missing).
            proposed = {}
            for project_id, holder_name in sorted(anchored.items()):
                proposed[project_id] = {"id": project_id, "name": projects[holder_name].name, "previous_names": [], "status": "bound"}
                changes.append(f"add record for anchored identity {project_id} under {projects[holder_name].name}")
                changed_ids.add(project_id)
            base_mode = "quarantine-and-rebuild" if unparseable else "rebuild-from-anchors"
        else:
            proposed = _registry_snapshot(registry)
            # Pre-existing conflicts are listed, never repaired by a side
            # effect. A disagreement is a genuine conflict only when the
            # recorded identity is itself anchored by a live project; a
            # bound record anchored nowhere is marked orphan below, which
            # is one of this command's enumerated updates (and is also the
            # end state resolve-identity would produce for it).
            anchored_values = set(anchored)
            conflict_ids = set()
            for project_id, record in proposed.items():
                if record["status"] == "bound" and record["name"] is not None:
                    holder = projects.get(record["name"].lower())
                    if holder is not None and anchors.get(record["name"].lower()) not in (None, project_id) \
                            and project_id in anchored_values:
                        conflicts.append(f"identity {anchors.get(record['name'].lower())} is anchored by {record['name']} while identity {project_id} is recorded under that name (resolve-identity or reidentify)")
                        conflict_ids.add(project_id)
            row8_ids = set()
            for project_id, holder_name in anchored.items():
                holder_record = bound_record_for_name(proposed, projects[holder_name].name)
                if holder_record is not None and holder_record["id"] != project_id \
                        and holder_record["id"] in anchored_values:
                    row8_ids.add(project_id)
            for project_id, holder_name in sorted(anchored.items()):
                if project_id not in proposed:
                    proposed[project_id] = {"id": project_id, "name": projects[holder_name].name, "previous_names": [], "status": "bound"}
                    changes.append(f"add record for anchored identity {project_id} under {projects[holder_name].name}")
                    changed_ids.add(project_id)
                elif proposed[project_id]["status"] == "orphan":
                    if project_id in row8_ids:
                        conflicts.append(f"row 13 flip for {project_id} refused: a name conflict involves this identity")
                        continue
                    flipped = dict(proposed[project_id])
                    if flipped["name"] not in (None, projects[holder_name].name):
                        history = list(flipped.get("previous_names") or [])
                        if flipped["name"] not in history:
                            history.append(flipped["name"])
                        flipped["previous_names"] = history
                    flipped["name"] = projects[holder_name].name
                    flipped["status"] = "bound"
                    proposed[project_id] = flipped
                    changes.append(f"flip orphaned identity {project_id} to bound under {projects[holder_name].name}")
                    changed_ids.add(project_id)
            for project_id, record in proposed.items():
                if record["status"] == "bound" and project_id not in anchored and project_id not in conflict_ids:
                    marked = dict(record)
                    marked["status"] = "orphan"
                    proposed[project_id] = marked
                    changed_ids.add(project_id)
                    changes.append(f"mark identity {project_id} (recorded under {record['name']}) orphan: no live anchor")
            base_mode = "repair"

        problems = _validate_registry_mapping(ws, proposed, anchors, changed_ids)
        for project_id in sorted(changed_ids & duplicated):
            problems.append(f"identity {project_id} is anchored by more than one live project ({', '.join(projects[name].name for name in holders_by_id[project_id])}); run pbz snapshot reidentify for each extra holder before the registry can be rebuilt")
        if intent is not None:
            after = intent.get("after") or {}
            for project_id, record in after.items():
                existing = proposed.get(project_id)
                if record.get("status") == "bound" and existing is not None and existing["status"] == "bound" and existing["name"] != record.get("name"):
                    problems.append(f"a repair intent for {intent.get('command')} covers the name {record.get('name')}; the rebuild adds no second bound record under it")
        plan = {
            "command": "rebind-registry", "mode": base_mode,
            "changes": changes, "problems": problems, "conflicts": conflicts,
            "records_before": 0 if registry is None else len(registry),
            "records_after": len(proposed),
            "intent": intent,
            "intent_unverifiable": intent_unverifiable,
            "unaccounted_projects": sorted(namespace_ids(ws) - set(anchored) - set(proposed)),
            "unaccounted_quarantine": sorted(quarantine_ids(ws) - set(proposed)),
        }
        if problems:
            raise PbzError("CORRUPT_STATE", "registry rebuild refused: " + "; ".join(problems))
        if not confirm_fn(plan):
            return _declined("rebind-registry")
        guard_ids = sorted(set(anchored) | changed_ids | namespace_ids(ws) | quarantine_ids(ws))
        with _exclusive_guards(ws, guard_ids):
            if corrupt_path is not None:
                os.replace(registry_path(ws), corrupt_path)
            if proposed != (registry or {}):
                write_registry(ws, proposed)
            if intent_status != "absent":
                # The confirmed rebuild resolves the repair intent it
                # listed; leaving it would keep blocking the commands it
                # names this command as the remedy for.
                _clear_repair_intent(ws)
                plan["intent_cleared"] = True
        return {"ok": True, "confirmed": True, **plan}


def quarantine_namespace(ws, identity_id, *, confirm_fn) -> dict:
    """quarantine-namespace: move an unrecognized, unaccounted namespace to
    snapshots/quarantine/<id>/ byte-identical and record a tombstone
    (state table row 12)."""
    if not is_uuid(identity_id):
        raise PbzError("BAD_REQUEST", "--identity must be a project identity UUID")
    with identity_lock(ws):
        intent = read_repair_intent(ws)
        if intent is not None:
            raise PbzError("CORRUPT_STATE", f"a repair intent for {intent.get('command')} on {intent.get('target')} is recorded at {repair_intent_path(ws)}; complete it or run pbz snapshot rebind-registry")
        anchors = discover_anchors(ws)
        registry = load_registry(ws)
        if identity_id in {value for value in anchors.values() if value is not None}:
            raise PbzError("CORRUPT_STATE", f"identity {identity_id} is anchored by a live project; it is accounted for and cannot be quarantined")
        record = (registry or {}).get(identity_id)
        in_projects = identity_id in namespace_ids(ws)
        in_quarantine = identity_id in quarantine_ids(ws)
        if record is not None and not (record["status"] == "quarantined" and in_quarantine and not in_projects):
            raise PbzError("CORRUPT_STATE", f"identity {identity_id} already has a registry record; it is accounted for and cannot be quarantined")
        if not in_projects and not in_quarantine:
            raise PbzError("BAD_REQUEST", f"no namespace exists for identity {identity_id}")
        plan = {
            "command": "quarantine-namespace", "identity": identity_id,
            "namespace_names": namespace_snapshot_names(ws, identity_id, quarantined=in_quarantine and not in_projects),
            "tombstone_only": in_quarantine and not in_projects,
        }
        if not confirm_fn(plan):
            return _declined("quarantine-namespace")
        with _exclusive_guards(ws, [identity_id]):
            if in_projects:
                source = os.path.join(ws.private, "snapshots", "projects", identity_id)
                destination = os.path.join(ws.private, "snapshots", "quarantine", identity_id)
                if os.path.lexists(destination):
                    raise PbzError("CONFLICT", f"cannot quarantine identity {identity_id}: {destination} is occupied")
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                os.rename(source, destination)
            after = _registry_snapshot(registry)
            after[identity_id] = {"id": identity_id, "name": None, "previous_names": [], "status": "quarantined"}
            write_registry(ws, after)
        return {"ok": True, "confirmed": True, **plan}
