"""Shared paths, workspace discovery and closed-vocabulary output for pbz."""
from __future__ import annotations

import hashlib
import json
import os
import sys

from . import guard

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_BIN = os.path.join(SKILL_DIR, "tools", "node_modules", ".bin")
MIRROR_DIR = guard.MIRROR_DIR
LOCALAPPDATA = guard.LOCALAPPDATA
PRIVATE_ROOT = guard.PRIVATE_ROOT


class PbzError(Exception):
    """User-facing error with a closed-vocabulary code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class Workspace:
    """A Power BI workspace root plus its projects (one per *.pbip)."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.id = hashlib.sha256(guard.norm(self.root).encode()).hexdigest()[:16]
        self.pbz_dir = os.path.join(self.root, ".pbz")
        self.policy_path = os.path.join(self.pbz_dir, "policy.json")
        self.mirror = os.path.join(self.root, MIRROR_DIR)
        # Not under AppData: packaged apps (e.g. the Claude desktop app) get a virtualized AppData,
        # which would split the store between agents launched from different hosts.
        self.private = os.path.join(os.environ.get("PBZ_PRIVATE_ROOT") or PRIVATE_ROOT, self.id)

    @property
    def projects(self) -> list["Project"]:
        out = []
        for name in sorted(os.listdir(self.root)):
            if name.lower().endswith(".pbip") and os.path.isfile(os.path.join(self.root, name)):
                out.append(Project(self, name[:-5]))
        return out

    def project(self, name: str | None = None) -> "Project":
        projects = self.projects
        if not projects:
            raise PbzError("NO_PROJECT", "no *.pbip in workspace root")
        if name is None:
            if len(projects) > 1:
                raise PbzError("AMBIGUOUS_PROJECT", "pass --project, one of: " + ", ".join(p.name for p in projects))
            return projects[0]
        for p in projects:
            if p.name.lower() == name.lower():
                return p
        raise PbzError("NO_PROJECT", name)


class Project:
    def __init__(self, ws: Workspace, name: str):
        self.ws = ws
        self.name = name
        self.pbip = os.path.join(ws.root, name + ".pbip")
        self.model_dir = os.path.join(ws.root, name + ".SemanticModel")
        self.report_dir = os.path.join(ws.root, name + ".Report")
        self.mirror_model = os.path.join(ws.mirror, name + ".SemanticModel")
        self.mirror_report = os.path.join(ws.mirror, name + ".Report")


def find_workspace(start: str | None = None) -> Workspace:
    start = os.path.abspath(start or os.getcwd())
    probe = start
    if os.path.basename(probe).lower() == MIRROR_DIR or (MIRROR_DIR + os.sep) in probe.lower():
        probe = probe[: probe.lower().index(MIRROR_DIR)]
    root = guard.find_root(guard.norm(probe))
    if not root:
        raise PbzError("NO_WORKSPACE", "run pbz from a folder that holds a .pbip")
    # guard returns a normalized lowercase path; recover the on-disk spelling
    cur = start
    while guard.norm(cur) != root:
        parent = os.path.dirname(cur)
        if parent == cur:
            cur = root
            break
        cur = parent
    return Workspace(cur)


def engine_call(verb: str, request: dict, timeout: int = 120) -> dict:
    """Call pbz-engine through pbz/engine.py; missing engine -> ENGINE_UNAVAILABLE."""
    try:
        from . import engine
    except ImportError:
        return {"ok": False, "verb": verb, "class": "ENGINE_UNAVAILABLE"}
    try:
        res = engine.call(verb, request, timeout=timeout)
    except Exception:  # never surface engine internals
        return {"ok": False, "verb": verb, "class": "OTHER"}
    return res if isinstance(res, dict) else {"ok": False, "verb": verb, "class": "OTHER"}


# ---------------------------------------------------------------- output

def emit(obj, as_json: bool = False) -> None:
    """Print agent-visible output. Only call with closed-vocabulary content."""
    if as_json:
        sys.stdout.write(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
    elif isinstance(obj, str):
        sys.stdout.write(obj.rstrip("\n") + "\n")
    else:
        sys.stdout.write(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def read_text(path: str) -> tuple[str, bytes]:
    """Read text preserving BOM and newlines exactly. Returns (text, bom)."""
    with open(path, "rb") as f:
        data = f.read()
    bom = b""
    if data.startswith(b"\xef\xbb\xbf"):
        bom, data = data[:3], data[3:]
    return data.decode("utf-8"), bom


def write_text(path: str, text: str, bom: bytes = b"") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".pbz-tmp"
    with open(tmp, "wb") as f:
        f.write(bom + text.encode("utf-8"))
    os.replace(tmp, path)


def commit_bytes(ops: list[tuple]) -> None:
    """Apply ('write', path, text, bom) and ('delete', path) as one mirror edit.

    Snapshots each path's bytes first (missing -> None), same idea as a recipe
    snapshot or a push journal, but only for files this call touches. On failure,
    restores those snapshots and removes leftover `.pbz-tmp` files so a later
    step cannot leave an earlier write or temp behind.
    """
    snap: dict[str, bytes | None] = {}
    for op in ops:
        path = op[1]
        if path in snap:
            continue
        try:
            with open(path, "rb") as f:
                snap[path] = f.read()
        except FileNotFoundError:
            snap[path] = None
    done: list[str] = []
    try:
        for op in ops:
            kind, path = op[0], op[1]
            if kind == "write":
                write_text(path, op[2], op[3] if len(op) > 3 else b"")
            elif kind == "delete":
                if os.path.lexists(path):
                    os.remove(path)
            else:
                raise PbzError("BAD_REQUEST", "unknown file operation")
            done.append(path)
    except OSError as first:
        restore_fail = []
        touched = list(dict.fromkeys(list(reversed(done)) + list(snap.keys())))
        for path in touched:
            tmp = path + ".pbz-tmp"
            try:
                if os.path.lexists(tmp):
                    os.remove(tmp)
            except OSError as e:
                restore_fail.append(f"{tmp}:{type(e).__name__}")
            if path not in snap:
                continue
            data = snap[path]
            try:
                if data is None:
                    if os.path.isfile(path):
                        os.remove(path)
                else:
                    parent = os.path.dirname(path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    restore = path + ".pbz-restore"
                    with open(restore, "wb") as f:
                        f.write(data)
                    os.replace(restore, path)
            except OSError as e:
                restore_fail.append(f"{path}:{type(e).__name__}")
        if restore_fail:
            raise PbzError("RECOVERY_REQUIRED",
                           "commit rolled back incompletely: " + ", ".join(restore_fail[:12])) from first
        raise


def sha(text_or_bytes) -> str:
    b = text_or_bytes.encode("utf-8") if isinstance(text_or_bytes, str) else text_or_bytes
    return hashlib.sha256(b).hexdigest()


def file_sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None
