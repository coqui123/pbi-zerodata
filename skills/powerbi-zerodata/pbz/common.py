"""Shared paths, workspace discovery and closed-vocabulary output for pbz."""
from __future__ import annotations

import hashlib
import json
import os
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
        self.root = ws.root
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
    except (OSError, PbzError) as first:
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
            except (OSError, PbzError) as e:
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
