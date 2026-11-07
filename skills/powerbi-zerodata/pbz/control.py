"""Safe metadata contracts for new orchestration commands."""
from __future__ import annotations

import json
import os
import re
from .common import PbzError, write_text

ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise PbzError("BAD_REQUEST", "expected a metadata identifier")
    return value


def load(spec):
    if isinstance(spec, dict):
        return spec
    if not isinstance(spec, str):
        raise PbzError("BAD_REQUEST", "a JSON object is required")
    try:
        if spec.lstrip().startswith("{"):
            text = spec
        else:
            with open(spec, encoding="utf-8-sig") as handle:
                text = handle.read(4 * 1024 * 1024 + 1)
        if len(text) > 4 * 1024 * 1024:
            raise ValueError()
        data = json.loads(text)
    except (OSError, ValueError, UnicodeError):
        raise PbzError("BAD_REQUEST", "invalid JSON specification") from None
    if not isinstance(data, dict):
        raise PbzError("BAD_REQUEST", "a JSON object is required")
    return data


def keys(obj, allowed, required=()):
    if not isinstance(obj, dict) or set(obj) - set(allowed) or set(required) - set(obj):
        raise PbzError("BAD_REQUEST", "unknown or missing specification fields")


def output_path(ws, directory, name):
    mirror = os.path.realpath(ws.mirror)
    base = os.path.realpath(os.path.join(ws.mirror, directory))
    target = os.path.realpath(os.path.join(base, name))
    if os.path.commonpath([mirror, base]) != mirror or os.path.commonpath([base, target]) != base:
        raise PbzError("BAD_REQUEST", "output must remain inside the mirror")
    return target


def save(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
