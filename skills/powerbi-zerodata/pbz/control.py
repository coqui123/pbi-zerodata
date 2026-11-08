"""Safe metadata contracts for new orchestration commands."""
from __future__ import annotations

import json
import os
import re
import stat

from . import guard
from .common import PbzError, _contained, _reparse_link, anchor_mirror, preflight_regular, write_text

ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
SPEC_CAP = 4 * 1024 * 1024
_SPEC_BAD = "invalid specification"
_SPEC_DENIED = "refusing to read a protected specification"


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise PbzError("BAD_REQUEST", "expected a metadata identifier")
    return value


def _parse_object(text: str) -> dict:
    try:
        if len(text.encode("utf-8")) > SPEC_CAP:
            raise PbzError("BAD_REQUEST", _SPEC_BAD)
        data = json.loads(text)
    except PbzError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise PbzError("BAD_REQUEST", _SPEC_BAD) from None
    if not isinstance(data, dict):
        raise PbzError("BAD_REQUEST", _SPEC_BAD)
    return data


def _under_root(path: str, root: str) -> bool:
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:
        return False
    return not (rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel))


def _deny_protected_names(path: str) -> None:
    parts = [p.casefold() for p in os.path.abspath(path).split(os.sep)]
    if ".pbz" in parts or any(p.endswith(".semanticmodel") or p.endswith(".report") for p in parts):
        raise PbzError("REFUSED", _SPEC_DENIED)
    try:
        why = guard.classify_path(guard.norm(path))
    except OSError:
        raise PbzError("REFUSED", _SPEC_DENIED) from None
    if why:
        raise PbzError("REFUSED", _SPEC_DENIED)


def _deny_spec_path(path: str, root: str | None) -> None:
    """Refuse links, hard links, protected trees and `.pbz` before any content read."""
    path = os.path.abspath(path)
    if _reparse_link(path):
        raise PbzError("REFUSED", _SPEC_DENIED)
    if root and _under_root(path, root):
        try:
            _contained(path, root)
        except PbzError:
            raise PbzError("REFUSED", _SPEC_DENIED) from None
    else:
        current = os.path.dirname(path)
        for _ in range(64):
            if _reparse_link(current):
                raise PbzError("REFUSED", _SPEC_DENIED)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        else:
            # Depth exhausted before the filesystem root: refuse rather than fail open.
            raise PbzError("REFUSED", _SPEC_DENIED)
    _deny_protected_names(path)
    try:
        resolved = os.path.realpath(path)
    except OSError:
        raise PbzError("REFUSED", _SPEC_DENIED) from None
    if os.path.normcase(resolved) != os.path.normcase(path):
        if _reparse_link(resolved):
            raise PbzError("REFUSED", _SPEC_DENIED)
        _deny_protected_names(resolved)


def _read_spec(path: str, root: str | None) -> bytes:
    _deny_spec_path(path, root)
    try:
        before = os.lstat(path)
    except OSError:
        raise PbzError("BAD_REQUEST", _SPEC_BAD) from None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _reparse_link(path):
        raise PbzError("REFUSED", _SPEC_DENIED)
    if before.st_size > SPEC_CAP:
        raise PbzError("BAD_REQUEST", _SPEC_BAD)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        try:
            info = os.fstat(fd)
        except OSError:
            raise PbzError("REFUSED", _SPEC_DENIED) from None
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_dev != before.st_dev or info.st_ino != before.st_ino):
            raise PbzError("REFUSED", _SPEC_DENIED)
        if info.st_size > SPEC_CAP:
            raise PbzError("BAD_REQUEST", _SPEC_BAD)
        _deny_spec_path(path, root)
        raw = os.read(fd, SPEC_CAP + 1)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if len(raw) > SPEC_CAP:
        raise PbzError("BAD_REQUEST", _SPEC_BAD)
    return raw


def load_spec(spec, *, root=None):
    """Dict and inline JSON pass through. A file is checked before the first content byte."""
    if isinstance(spec, dict):
        return spec
    if not isinstance(spec, str):
        raise PbzError("BAD_REQUEST", _SPEC_BAD)
    if spec.lstrip().startswith("{"):
        return _parse_object(spec)
    path = os.path.abspath(os.path.expanduser(spec))
    if not os.path.lexists(path):
        return _parse_object(spec)
    try:
        text = _read_spec(path, root).decode("utf-8-sig")
    except PbzError:
        raise
    except (OSError, UnicodeError):
        raise PbzError("BAD_REQUEST", _SPEC_BAD) from None
    return _parse_object(text)


def load(spec, *, root=None):
    return load_spec(spec, root=root)


def keys(obj, allowed, required=()):
    if not isinstance(obj, dict) or set(obj) - set(allowed) or set(required) - set(obj):
        raise PbzError("BAD_REQUEST", "unknown or missing specification fields")


def output_path(ws, directory, name):
    """Lexical containment first. realpath runs only after a junction cannot hide the target."""
    anchor_mirror(ws)
    base = os.path.abspath(os.path.join(ws.mirror, directory))
    target = os.path.abspath(os.path.join(base, name))
    if _reparse_link(base) or _reparse_link(target):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    _contained(target, ws.root)
    _contained(target, ws.mirror)
    preflight_regular(target, root=ws.mirror)
    mirror = os.path.realpath(ws.mirror)
    real_base = os.path.realpath(base)
    real_target = os.path.realpath(target)
    if os.path.commonpath([mirror, real_base]) != mirror or os.path.commonpath([real_base, real_target]) != real_base:
        raise PbzError("BAD_REQUEST", "output must remain inside the mirror")
    return target


def save(path, value, *, root=None):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", root=root)
