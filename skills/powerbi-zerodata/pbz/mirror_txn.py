"""Snapshot a mirror tree and restore it when a multi-file edit fails.

`rename` writes each object immediately. A pattern that renames several objects has to look like one
edit: any failure puts the previous bytes back and deletes files the attempt created.
"""
from __future__ import annotations

import os

from .common import PbzError, _reparse_link


def _files(root: str) -> list[str]:
    if not os.path.isdir(root):
        return []
    out = []
    for dirpath, dirnames, names in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or _reparse_link(path):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            kept.append(name)
        dirnames[:] = kept
        for name in names:
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or _reparse_link(path):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            out.append(path)
    return out


def _restore(root: str, snap: dict[str, tuple[str, bytes]]) -> None:
    if os.path.isdir(root):
        for path in _files(root):
            if os.path.normcase(path) not in snap:
                os.remove(path)
    for path, data in snap.values():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)


def transaction(root: str, fn):
    """Run `fn`. On any exception, restore `root` to the bytes it had before the call."""
    snap = {}
    for path in _files(root):
        with open(path, "rb") as handle:
            snap[os.path.normcase(path)] = (path, handle.read())
    try:
        return fn()
    except BaseException:
        _restore(root, snap)
        raise
