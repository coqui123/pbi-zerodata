"""Thin wrapper around pbz-engine.exe, the only component allowed to touch a live model.

call(verb, request) sends one JSON request on stdin and returns the engine's single JSON response.
The response is already a closed vocabulary (see engine/CONTRACT.md); this wrapper never adds engine
text of its own. Failures to run the engine collapse to a fixed envelope:

  missing exe / unparseable output -> {"ok": False, "verb": verb, "class": "ENGINE_UNAVAILABLE"}
  wall-clock timeout               -> {"ok": False, "verb": verb, "class": "TIMEOUT"}
  request not JSON-serializable    -> {"ok": False, "verb": verb, "class": "BAD_REQUEST"}

Callers pass private_dir (and port for live verbs) in every request themselves.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

ENGINE_EXE = Path(__file__).resolve().parent.parent / "engine" / "bin" / "pbz-engine.exe"

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _fail(verb: str, cls: str) -> dict:
    return {"ok": False, "verb": verb, "class": cls}


def _matches(value, schema, defs):
    try:
        _closed(value, schema, defs)
        return True
    except ValueError:
        return False


def _closed(value, schema, defs):
    """Project unknown keys recursively; reject wrong types on known keys."""
    if "$ref" in schema:
        return _closed(value, defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in schema or "oneOf" in schema:
        for option in schema.get("anyOf", schema.get("oneOf", [])):
            try:
                return _closed(value, option, defs)
            except ValueError:
                pass
        raise ValueError("union")
    if "not" in schema and _matches(value, schema["not"], defs):
        raise ValueError("not")
    if "if" in schema:
        branch = "then" if _matches(value, schema["if"], defs) else "else"
        if branch in schema:
            _closed(value, schema[branch], defs)
    typ = schema.get("type")
    if isinstance(typ, list):
        return _closed(value, {"anyOf": [{**schema, "type": t} for t in typ]}, defs)
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if typ == "number":
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("number")
    elif typ and type(value) is not types.get(typ):
        raise ValueError("type")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError("const")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("enum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            raise ValueError("length")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError("pattern")
    if type(value) in (int, float):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ValueError("range")
    if isinstance(value, dict):
        known = schema.get("properties")
        size = len(value) if known is None else sum(k in known for k in value)
        if size < schema.get("minProperties", 0) or size > schema.get("maxProperties", size):
            raise ValueError("properties")
        if any(k not in value for k in schema.get("required", [])):
            raise ValueError("required")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", False)
        return {k: _closed(v, properties[k] if k in properties else extra, defs)
                for k, v in value.items() if k in properties or isinstance(extra, dict)}
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise ValueError("items")
        return [_closed(v, schema.get("items", {}), defs) for v in value]
    return value


def _contract_project(verb, response):
    schema = json.loads((Path(__file__).resolve().parent.parent / "engine" / "contract.schema.json").read_text())
    defs = schema["$defs"]
    if response.get("ok") is False:
        return _closed(response, defs["failure"], defs)
    for node in defs.values():
        if node.get("properties", {}).get("verb", {}).get("const") == verb:
            return _closed(response, node, defs)
    raise ValueError("verb")


_TYPES = {"string", "int64", "double", "dateTime", "decimal", "boolean", "binary", "variant", "unknown"}
_COUNTS = {"0", "1-9", "10-99", "100-999", "1k-9k", "10k+"}
_SHARES = {"none", "<1%", "1-10%", "10-50%", ">50%"}


def _probe(response, request):
    """Unknown fields are dropped; malformed known fields invalidate their record."""
    request = request or {}
    strict = request.get("disclosure", "strict") != "shape"
    if "verb" in response and (type(response["verb"]) is not str or response["verb"] != "probe"):
        return _fail("probe", "ENGINE_UNAVAILABLE")
    table = request.get("table")
    names = request.get("columns", [])
    if (not isinstance(table, str) or type(response.get("table")) is not str
            or response["table"].casefold() != table.casefold() or not isinstance(names, list)):
        return _fail("probe", "ENGINE_UNAVAILABLE")
    # Requests may use bare names or fully qualified DAX column references.
    allowed = set()
    for name in names:
        if isinstance(name, str):
            ref = re.fullmatch(r"(?:'((?:[^']|'')+)'|([^\[\]]+))\[((?:[^\]]|\]\])+)\]", name)
            if ref:
                owner = (ref[1].replace("''", "'") if ref[1] else ref[2]).strip()
                if owner.casefold() != table.casefold():
                    continue
                name = ref[3].replace("]]", "]")
            allowed.add(name.casefold())
    out = {"ok": True, "verb": "probe", "table": table, "columns": []}
    if type(response.get("columns")) is not list:
        return _fail("probe", "ENGINE_UNAVAILABLE")
    if "rows_bucket" in response:
        if type(response["rows_bucket"]) is not str or response["rows_bucket"] not in _COUNTS:
            return _fail("probe", "ENGINE_UNAVAILABLE")
        if not strict:
            out["rows_bucket"] = response["rows_bucket"]
    for col in response["columns"]:
        if not isinstance(col, dict):
            continue
        if type(col.get("column")) is not str or col["column"].casefold() not in allowed:
            continue
        if type(col.get("type")) is not str or col["type"] not in _TYPES:
            continue
        clean = {"column": col["column"], "type": col["type"]}
        valid = True
        for key in ("has_blanks", "has_negatives", "has_future_dates", "blank_bucket", "card_bucket"):
            if key not in col:
                continue
            value = col[key]
            choices = _SHARES if key == "blank_bucket" else _COUNTS
            if (type(value) is not bool if key.startswith("has_") else
                    type(value) is not str or value not in choices):
                valid = False
                break
            if not strict or not key.endswith("_bucket"):
                clean[key] = value
        if valid:
            out["columns"].append(clean)
    return out


def _project(verb: str, response: dict, request: dict | None = None) -> dict:
    """Select the requested verb schema, drop unknown keys, and reject malformed known values."""
    if verb == "probe" and response.get("ok") is True:
        return _probe(response, request)
    try:
        return _contract_project(verb, response)
    except (ValueError, TypeError, RecursionError):
        return _fail(verb, "ENGINE_UNAVAILABLE")


def call(verb: str, request: dict, timeout: float = 120) -> dict:
    """Run `pbz-engine.exe <verb>` with `request` as JSON on stdin and return the parsed response."""
    exe = Path(ENGINE_EXE)
    if not exe.is_file():
        return _fail(verb, "ENGINE_UNAVAILABLE")
    try:
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return _fail(verb, "BAD_REQUEST")
    try:
        proc = subprocess.run(
            [str(exe), verb],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(exe.parent),
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return _fail(verb, "TIMEOUT")
    except OSError:
        return _fail(verb, "ENGINE_UNAVAILABLE")
    try:
        response = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _fail(verb, "ENGINE_UNAVAILABLE")
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        return _fail(verb, "ENGINE_UNAVAILABLE")
    return _project(verb, response, request)
