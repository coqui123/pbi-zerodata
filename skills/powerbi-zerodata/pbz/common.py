"""Shared paths, workspace discovery and closed-vocabulary output for pbz."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile

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


def _path_inside(path: str, root: str) -> bool:
    """True when `path` is `root` or a descendant. Different drives are not inside."""
    try:
        child = os.path.normcase(os.path.abspath(os.path.expanduser(path)))
        parent = os.path.normcase(os.path.abspath(os.path.expanduser(root)))
        return os.path.commonpath([child, parent]) == parent
    except (OSError, ValueError):
        return False


def _reject_private_root(configured: str, workspace: str, mirror: str) -> None:
    """PBZ_PRIVATE_ROOT must not sit in the workspace, the mirror, or a global deny location."""
    raw = os.path.expanduser(configured)
    candidates = [raw]
    if os.path.lexists(raw):
        try:
            candidates.append(os.path.realpath(raw))
        except OSError:
            raise PbzError("UNSAFE_PATH", "private root is inside a denied location") from None
    places = [workspace, mirror, *guard.GLOBAL_DENY_DIRS]
    for candidate in candidates:
        normed = guard.norm(candidate)
        if any(re.search(pat, normed) for pat in guard.GLOBAL_DENY_PATTERNS):
            raise PbzError("UNSAFE_PATH", "private root is inside a denied location")
        for place in places:
            if _path_inside(candidate, place):
                raise PbzError("UNSAFE_PATH", "private root is inside a denied location")


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
        configured = os.environ.get("PBZ_PRIVATE_ROOT")
        if configured:
            _reject_private_root(configured, self.root, self.mirror)
        self.private = os.path.join(configured or PRIVATE_ROOT, self.id)

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
        self.root = ws.root
        self.pbip = os.path.join(ws.root, name + ".pbip")
        self.model_dir = os.path.join(ws.root, name + ".SemanticModel")
        self.report_dir = os.path.join(ws.root, name + ".Report")
        self.mirror_model = os.path.join(ws.mirror, name + ".SemanticModel")
        self.mirror_report = os.path.join(ws.mirror, name + ".Report")


def find_workspace(start: str | None = None) -> Workspace:
    start = os.path.abspath(start or os.getcwd())
    probe = start
    parts = probe.split(os.sep)
    for index, part in enumerate(parts):
        if part.lower() != MIRROR_DIR:
            continue
        # Whole path component only: "old-pbz-workspace" is not the mirror directory.
        head = parts[:index]
        if not head:
            probe = os.sep
        elif len(head) == 1 and head[0].endswith(":"):
            probe = head[0] + os.sep
        else:
            probe = os.sep.join(head) or os.sep
        break
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
    """Read text preserving BOM and newlines exactly. Returns (text, bom).

    Unchecked. Prefer `read_text_checked` / `open_mirror_file_readonly` for mirror content.
    """
    with open(path, "rb") as f:
        data = f.read()
    bom = b""
    if data.startswith(b"\xef\xbb\xbf"):
        bom, data = data[:3], data[3:]
    return data.decode("utf-8"), bom


def open_mirror_file_readonly(path: str, *, mirror_root: str, max_bytes: int | None = None) -> bytes:
    """Read one file under the mirror trust root: single-link, no reparse leaf/ancestors, handle recheck.

    The refusal detail is the shared link message, including on this read path.
    """
    root = os.path.abspath(mirror_root)
    target = os.path.abspath(path)
    if _reparse_link(root):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    return read_checked(target, root=root, max_bytes=max_bytes)


def read_text_checked(path: str, *, root: str, max_bytes: int | None = None) -> tuple[str, bytes]:
    """Like `read_text`, but through `open_mirror_file_readonly` / `read_checked`."""
    data = open_mirror_file_readonly(path, mirror_root=root, max_bytes=max_bytes)
    bom = b""
    if data.startswith(b"\xef\xbb\xbf"):
        bom, data = data[:3], data[3:]
    return data.decode("utf-8"), bom


def definition_root(path: str) -> str:
    """Trust root for a definition read.

    A pbz-workspace directory on the path wins. Otherwise the read is contained to the
    .Report or .SemanticModel directory that holds the file, not to the project parent.
    """
    cur = os.path.abspath(path)
    mirror = None
    artifact = None
    while True:
        base = os.path.basename(cur).lower()
        if base == MIRROR_DIR.lower():
            mirror = cur
        elif artifact is None and (base.endswith(".report") or base.endswith(".semanticmodel")):
            artifact = cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if mirror:
        return mirror
    if artifact:
        return artifact
    raise PbzError("UNSAFE_PATH", "refusing to write through a link")


def read_definition_text(path: str) -> tuple[str, bytes]:
    """Read one mirror definition file. A missing file is FileNotFoundError. A link is UNSAFE_PATH."""
    if not os.path.lexists(path):
        raise FileNotFoundError(path)
    return read_text_checked(path, root=definition_root(path))


# IO_REPARSE_TAG_SYMLINK and IO_REPARSE_TAG_MOUNT_POINT. Junctions are mount points.
_LINK_TAGS = (0xA000000C, 0xA0000003)


def _reparse_link(path: str) -> bool:
    """True when `path` is a symlink or junction. Missing paths are not links."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if os.path.islink(path):
        return True
    return getattr(info, "st_reparse_tag", None) in _LINK_TAGS


def _still_owned(tmp: str, dev: int, ino: int) -> bool:
    """True when `tmp` is still the file this call created. A link or a replacement is not."""
    try:
        current = os.lstat(tmp)
    except OSError:
        return False
    if _reparse_link(tmp):
        return False
    return current.st_dev == dev and current.st_ino == ino


def _drop_owned(tmp: str, dev: int, ino: int) -> None:
    """Remove only the temp this call created. An unowned sibling is left in place."""
    try:
        if _still_owned(tmp, dev, ino):
            os.remove(tmp)
    except OSError:
        pass


def _fd_identity(fd: int) -> tuple[int, int] | None:
    """(dev, ino) of an open fd. Does not call os.fstat, which tests may fail on purpose."""
    if fd < 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class _FileId(ctypes.Structure):
                _fields_ = [
                    ("VolumeSerialNumber", ctypes.c_ulonglong),
                    ("FileId", ctypes.c_ubyte * 16),
                ]

            info = _FileId()
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetFileInformationByHandleEx.argtypes = (
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
            handle = msvcrt.get_osfhandle(fd)
            if not kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            ino = int.from_bytes(bytes(info.FileId[:8]), "little")
            return (int(info.VolumeSerialNumber), ino)
        except (OSError, AttributeError, ValueError):
            return None
    try:
        info = os.stat(f"/proc/self/fd/{fd}")
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino)


def _discard_created(fd: int, tmp: str) -> None:
    """Close `fd` and remove `tmp` only when that path is still the file the fd names.

    A replacement parked at `tmp` after create keeps its bytes. The fd is always closed.
    """
    ident = _fd_identity(fd) if fd >= 0 else None
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass
    if not tmp or ident is None or _reparse_link(tmp):
        return
    try:
        info = os.lstat(tmp)
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != ident:
        return
    try:
        os.remove(tmp)
    except OSError:
        pass


def remove_file(path: str, *, root: str | None = None) -> None:
    """Delete one regular file inside `root`. A link or a junction is not deleted."""
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    _contained(path, root)
    if not os.path.lexists(path):
        raise FileNotFoundError(path)
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    _contained(path, root)
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    os.remove(path)


def _contained(path: str, root: str | None) -> None:
    """Refuse when a path component at or below `root` resolves outside that root.

    Ancestors above `root` are not checked: a workspace reached through a junction
    (Dev Drive, a redirected profile) must still be writable. With no root, only the
    leaf link check in the caller applies.
    """
    if root is None:
        return
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    try:
        rel = os.path.relpath(path_abs, root_abs)
    except ValueError:
        raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    try:
        root_real = os.path.realpath(root_abs)
    except OSError:
        raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
    current = os.path.dirname(path_abs)
    for _ in range(64):
        below_root = os.path.normcase(current) != os.path.normcase(root_abs)
        if below_root and _reparse_link(current):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        try:
            real = os.path.realpath(current)
            if os.path.normcase(os.path.commonpath([real, root_real])) != os.path.normcase(root_real):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        except ValueError:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
        if not below_root:
            return
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise PbzError("UNSAFE_PATH", "refusing to write through a link")


def _workspace_root(ws) -> str:
    """Absolute workspace root. Infer from `.pbz` or mirror when `ws.root` is absent (test fixtures)."""
    root = getattr(ws, "root", None)
    if root is not None:
        return os.path.abspath(root)
    pbz = getattr(ws, "pbz_dir", None)
    if pbz is not None and os.path.normcase(os.path.basename(os.path.abspath(pbz))) == ".pbz":
        return os.path.dirname(os.path.abspath(pbz))
    mirror = getattr(ws, "mirror", None)
    if mirror is not None and os.path.normcase(os.path.basename(os.path.abspath(mirror))) == os.path.normcase(MIRROR_DIR):
        return os.path.dirname(os.path.abspath(mirror))
    raise PbzError("UNSAFE_PATH", "refusing to write through a link")


def pbz_state_path(ws, *parts: str) -> str:
    """A path under `<workspace>/.pbz`.

    Refuse a reparse point on `.pbz` itself and on the leaf. When either already
    exists, containment is checked against the workspace root, so a junction at
    `.pbz` is not its own trust root. Ancestors above the workspace stay unchecked.
    """
    root = _workspace_root(ws)
    pbz = os.path.join(root, ".pbz")
    path = os.path.join(pbz, *parts) if parts else pbz
    if _reparse_link(pbz) or (os.path.normcase(path) != os.path.normcase(pbz) and _reparse_link(path)):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    if os.path.lexists(pbz) or os.path.lexists(path):
        _contained(path, root)
    return path


def anchor_mirror(ws) -> str:
    """Refuse a junction or symlink at the mirror directory before it is used as a trust root.

    `_contained` trusts the root it is given, so passing `ws.mirror` does not see a reparse
    point whose path is exactly the mirror. Ancestors above the workspace stay unchecked.
    """
    mirror = os.path.abspath(ws.mirror)
    root = _workspace_root(ws)
    expected = os.path.abspath(os.path.join(root, MIRROR_DIR))
    if os.path.normcase(mirror) != os.path.normcase(expected) or _reparse_link(mirror):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    if os.path.lexists(mirror):
        _contained(mirror, root)
        try:
            real = os.path.realpath(mirror)
            root_real = os.path.realpath(root)
            if os.path.normcase(os.path.commonpath([real, root_real])) != os.path.normcase(root_real):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        except ValueError:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
    return mirror


def _regular_single(info) -> bool:
    return stat.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) == 1


def preflight_regular(path: str, *, root: str | None) -> None:
    """Refuse a link, an escape, or an existing hard link. Does not open the file."""
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    _contained(path, root)
    if not os.path.lexists(path):
        return
    try:
        info = os.lstat(path)
    except OSError:
        raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
    if not _regular_single(info) or _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    _contained(path, root)


# Detail token when an optional max_bytes cap is exceeded. Callers map it; the token is not a path.
SIZE_CAP = "SIZE_CAP"


# Tests set this to swap the path between the open and the identity re-check. Production leaves it None.
_read_checked_hook = None


def read_checked(path: str, *, root: str | None, max_bytes: int | None = None) -> bytes:
    """Read one regular single-link file. The opened handle must still be that file before any content read.

    `max_bytes` is optional. When set, the cap is the opened handle's size, checked before read().
    Callers that omit it stay unbounded.
    """
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    _contained(path, root)
    try:
        before = os.lstat(path)
    except OSError:
        raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
    if not _regular_single(before) or _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    if max_bytes is not None and before.st_size > max_bytes:
        raise PbzError("REFUSED", SIZE_CAP)
    _contained(path, root)
    if _read_checked_hook is not None:
        _read_checked_hook(path)
    with open(path, "rb") as handle:
        try:
            info = os.fstat(handle.fileno())
        except OSError:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
        if (not _regular_single(info) or info.st_dev != before.st_dev or info.st_ino != before.st_ino):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        if max_bytes is not None and info.st_size > max_bytes:
            raise PbzError("REFUSED", SIZE_CAP)
        try:
            after = os.lstat(path)
        except OSError:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
        if (after.st_dev != before.st_dev or after.st_ino != before.st_ino or _reparse_link(path)):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        _contained(path, root)
        if max_bytes is None:
            return handle.read()
        data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise PbzError("REFUSED", SIZE_CAP)
        return data


def _append_flags(max_bytes: int | None, *, create: bool = False) -> int:
    flags = os.O_APPEND | os.O_WRONLY
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    if max_bytes is not None:
        flags |= getattr(os, "O_BINARY", 0)
    return flags


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        wrote = os.write(fd, view)
        if wrote <= 0:
            raise OSError("write made no progress")
        view = view[wrote:]


def _append_existing(path: str, payload: bytes, *, root: str | None, max_bytes: int | None) -> None:
    """Identity-check an existing file and append. A cap failure writes nothing."""
    try:
        before = os.lstat(path)
    except OSError:
        raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
    if not _regular_single(before) or _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    fd = os.open(path, _append_flags(max_bytes))
    try:
        try:
            info = os.fstat(fd)
        except OSError:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
        if not _regular_single(info) or info.st_dev != before.st_dev or info.st_ino != before.st_ino:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        try:
            after = os.lstat(path)
        except OSError:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
        if (after.st_dev != before.st_dev or after.st_ino != before.st_ino
                or not _regular_single(after) or _reparse_link(path)):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        _contained(path, root)
        if max_bytes is not None:
            # Fresh size after identity/containment — do not reuse the earlier fstat.
            try:
                sized = os.fstat(fd)
            except OSError:
                raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
            if sized.st_size + len(payload) > max_bytes:
                raise PbzError("REFUSED", SIZE_CAP)
        _write_all(fd, payload)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def append_checked(path: str, text: str, *, root: str | None, max_bytes: int | None = None) -> None:
    """Append text. An existing file must still be the regular single-link file that was checked, before any write.

    `max_bytes` is optional. When it is omitted, a new file still goes through write_text and an existing
    file is opened with O_APPEND|O_WRONLY only. When it is set, the cap is checked before any byte is
    written, new files are created with O_CREAT|O_EXCL|O_APPEND|O_WRONLY|O_BINARY, and a FileExistsError
    re-enters the existing-file path.
    """
    preflight_regular(path, root=root)
    payload = text.encode("utf-8")
    if not os.path.lexists(path):
        if max_bytes is None:
            write_text(path, text, root=root)
            return
        if len(payload) > max_bytes:
            raise PbzError("REFUSED", SIZE_CAP)
        parent = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(parent, exist_ok=True)
        if _reparse_link(parent) or _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        _contained(path, root)
        try:
            fd = os.open(path, _append_flags(max_bytes, create=True))
        except FileExistsError:
            _append_existing(path, payload, root=root, max_bytes=max_bytes)
            return
        try:
            _write_all(fd, payload)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        return
    _append_existing(path, payload, root=root, max_bytes=max_bytes)


def _trust_root(path: str, root: str | None, roots: dict | None) -> str | None:
    if roots and path in roots:
        return roots[path]
    return root


def replace_bytes(path: str, data: bytes, *, root: str | None = None) -> None:
    """Write `path` via an exclusive temp in the same directory, then replace.

    The destination is refused when it, or a parent at or below `root`, is a symlink
    or junction. The handle is closed before replace. A write that makes no progress
    is an error. The temp's identity is checked again before replace, and only that
    temp is removed on failure.
    """
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    _contained(path, root)
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = -1, ""
    opened = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".pbz-", suffix=".pbz-tmp", dir=parent)
        try:
            opened = os.fstat(fd)
        except OSError:
            _discard_created(fd, tmp)
            fd, tmp = -1, ""
            raise
        view = memoryview(data)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                raise OSError("write made no progress")
            view = view[wrote:]
        try:
            os.close(fd)
        except OSError:
            # Keep fd owned so the outer handler can still close and drop the temp.
            raise
        else:
            fd = -1
        if opened is None or not _still_owned(tmp, opened.st_dev, opened.st_ino):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        if _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        _contained(path, root)
        os.replace(tmp, path)
        tmp = ""
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = -1
        if tmp and opened is not None:
            _drop_owned(tmp, opened.st_dev, opened.st_ino)
        raise


def write_text(path: str, text: str, bom: bytes = b"", *, root: str | None = None) -> None:
    replace_bytes(path, bom + text.encode("utf-8"), root=root)


def write_bytes(path: str, data: bytes, *, root: str | None = None) -> None:
    """Write raw bytes the same way as write_text: exclusive temp, then replace."""
    replace_bytes(path, data, root=root)


def _op_shape(op) -> tuple[str, str]:
    if not isinstance(op, tuple) or len(op) < 2 or not isinstance(op[1], str):
        raise PbzError("BAD_REQUEST", "unknown file operation")
    kind = op[0]
    if kind not in ("write", "write_bytes", "delete"):
        raise PbzError("BAD_REQUEST", "unknown file operation")
    return kind, op[1]


def _checked_op(op) -> tuple:
    """Arity and types only. No path is stated or read."""
    kind, path = _op_shape(op)
    if kind == "write":
        if len(op) not in (3, 4) or not isinstance(op[2], str):
            raise PbzError("BAD_REQUEST", "unknown file operation")
        bom = b"" if len(op) == 3 else op[3]
        if not isinstance(bom, (bytes, bytearray)):
            raise PbzError("BAD_REQUEST", "unknown file operation")
        return ("write", path, op[2], bytes(bom))
    if kind == "write_bytes":
        if len(op) != 3 or not isinstance(op[2], (bytes, bytearray)):
            raise PbzError("BAD_REQUEST", "unknown file operation")
        return ("write_bytes", path, bytes(op[2]))
    if len(op) != 2:
        raise PbzError("BAD_REQUEST", "unknown file operation")
    return ("delete", path)


def commit_bytes(ops: list[tuple], *, private: bool = False, root: str | None = None,
                 roots: dict | None = None) -> None:
    """Apply ('write', path, text, bom), ('write_bytes', path, data) and ('delete', path) as one edit.

    Unsafe paths are refused before any snapshot or content read, so a symlink is not
    opened and is not restored. `root` is the trust root for every op; `roots` overrides
    it per path. Deletes use that same containment, including while rolling back.
    On failure, snapshots are restored through the same root checks.
    """
    checked = [_checked_op(op) for op in ops]
    for kind, path, *_rest in checked:
        if _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        _contained(path, _trust_root(path, root, roots))
    snap: dict[str, bytes | None] = {}
    for _kind, path, *_rest in checked:
        if path in snap:
            continue
        trust = _trust_root(path, root, roots)
        # Re-validate each snapshot target immediately before opening it. A parent that
        # becomes a junction after the initial loop must still refuse before the read.
        if _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        _contained(path, trust)
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            snap[path] = None
            continue
        except OSError:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        try:
            with open(path, "rb") as f:
                try:
                    info = os.fstat(f.fileno())
                except OSError:
                    raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_dev != before.st_dev or info.st_ino != before.st_ino):
                    raise PbzError("UNSAFE_PATH", "refusing to write through a link")
                try:
                    after = os.lstat(path)
                except OSError:
                    raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
                if (after.st_dev != before.st_dev or after.st_ino != before.st_ino
                        or _reparse_link(path)):
                    raise PbzError("UNSAFE_PATH", "refusing to write through a link")
                _contained(path, trust)
                snap[path] = f.read()
        except FileNotFoundError:
            snap[path] = None
    done: list[str] = []
    try:
        for op in checked:
            kind, path = op[0], op[1]
            op_root = _trust_root(path, root, roots)
            if kind == "write":
                write_text(path, op[2], op[3], root=op_root)
            elif kind == "write_bytes":
                write_bytes(path, op[2], root=op_root)
            else:
                _contained(path, op_root)
                if os.path.lexists(path):
                    if _reparse_link(path):
                        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
                    os.remove(path)
            done.append(path)
    except (OSError, PbzError, UnicodeError) as first:
        restore_fail = []
        touched = list(dict.fromkeys(list(reversed(done)) + list(snap.keys())))
        for path in touched:
            if path not in snap:
                continue
            data = snap[path]
            try:
                trust = _trust_root(path, root, roots)
                if data is None:
                    _contained(path, trust)
                    if os.path.lexists(path):
                        if _reparse_link(path) or not os.path.isfile(path):
                            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
                        os.remove(path)
                else:
                    replace_bytes(path, data, root=trust)
            except (OSError, PbzError, UnicodeError) as e:
                restore_fail.append(f"{path}:{type(e).__name__}")
        if restore_fail:
            if private:
                raise PbzError("RECOVERY_REQUIRED", "commit rolled back incompletely") from None
            raise PbzError("RECOVERY_REQUIRED",
                           "commit rolled back incompletely: " + ", ".join(restore_fail[:12])) from first
        if isinstance(first, PbzError):
            raise first
        if private:
            raise PbzError("FAILED", "write failed; mirror rolled back, no file changed") from None
        raise PbzError("FAILED", "write failed; mirror rolled back, no file changed") from first


def sha(text_or_bytes) -> str:
    b = text_or_bytes.encode("utf-8") if isinstance(text_or_bytes, str) else text_or_bytes
    return hashlib.sha256(b).hexdigest()


def file_sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None
