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

import ctypes
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
            if not isinstance(option, dict):
                continue
            if "anyOf" not in schema and isinstance(value, dict):
                if (option.get("additionalProperties") is False and "properties" in option
                        and any(k not in option["properties"] for k in value)):
                    continue
                if any(k not in value for k in option.get("required", [])):
                    continue
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
        prefix = schema.get("prefixItems", [])
        return [_closed(v, prefix[i] if i < len(prefix) else schema.get("items", {}), defs)
                for i, v in enumerate(value)]
    return value


def _contract_project(verb, response):
    schema = json.loads((Path(__file__).resolve().parent.parent / "engine" / "contract.schema.json").read_text())
    if not isinstance(schema, dict) or not isinstance(schema.get("$defs"), dict):
        raise ValueError("definitions")
    defs = schema["$defs"]
    if any(not isinstance(node, dict) for node in defs.values()):
        raise ValueError("definition")
    if response.get("ok") is False:
        if isinstance(response.get("verb"), str) and response["verb"] != verb:
            raise ValueError("verb")
        return _closed(response, defs["failure"], defs)
    for node in defs.values():
        if node.get("properties", {}).get("verb", {}).get("const") == verb:
            return _closed(response, node, defs)
    raise ValueError("verb")


_TYPES = {"string", "int64", "double", "dateTime", "decimal", "boolean", "binary", "variant", "unknown"}
_COUNTS = {"0", "1-9", "10-99", "100-999", "1k-9k", "10k+"}
_SHARES = {"none", "<1%", "1-10%", "10-50%", ">50%"}


def _probe(response, request):
    """Project probe fields. The effective tier arrives in request.disclosure,
    supplied by checks.probe from the workspace policy.
    """
    request = request or {}
    strict = request.get("disclosure", "strict") != "shape"
    if response.get("verb") != "probe":
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
    except (ValueError, TypeError, RecursionError, OSError, KeyError):
        return _fail(verb, "ENGINE_UNAVAILABLE")


_CLEANUP_WAIT = 5


def create_kill_on_close_job():
    from ctypes import wintypes as w
    from .common import PbzError

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", w.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t), ("active_processes", w.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
            ("scheduling", w.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateJobObjectW
    create.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    create.restype = w.HANDLE
    job = create(None, None)
    if not job:
        raise PbzError("ENGINE_UNAVAILABLE")
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configure = kernel.SetInformationJobObject
    configure.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    configure.restype = w.BOOL
    try:
        if not configure(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise PbzError("ENGINE_UNAVAILABLE")
    except BaseException:
        kernel.CloseHandle(ctypes.c_void_p(job))
        raise
    return job


def _terminate_and_wait(child):
    for _ in range(2):
        try:
            child.kill()
            child.wait(timeout=_CLEANUP_WAIT)
            return True
        except Exception:
            pass
    return False


def _run_inheriting(argv, payload, timeout, options, pass_fds, handles, job):
    child = None
    phase = "setup"
    try:
        if handles or job is not None:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        if handles:
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": list(handles)}
            options.update(startupinfo=startup, close_fds=True)
            toggle = kernel.SetHandleInformation
            toggle.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
            toggle.restype = ctypes.c_int

            def set_inheritable(enabled):
                for handle in handles:
                    if not toggle(handle, 1, int(enabled)):
                        raise ctypes.WinError(ctypes.get_last_error())
        elif pass_fds:
            options["pass_fds"] = pass_fds

        try:
            if handles:
                set_inheritable(True)
            phase = "spawn"
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, **options)
        finally:
            if handles:
                previous_phase = phase
                phase = "clear"
                set_inheritable(False)
                phase = previous_phase

        phase = "assign"
        if job is not None:
            assign = kernel.AssignProcessToJobObject
            assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            assign.restype = ctypes.c_int
            if not assign(job, int(child._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        phase = "communicate"
        stdout, _ = child.communicate(payload, timeout=timeout)
        return stdout, None, "may_have_been_sent"
    except BaseException as error:
        confirmed = True
        if child is not None:
            try:
                confirmed = _terminate_and_wait(child)
            finally:
                if child.stdin is not None:
                    try:
                        child.stdin.close()
                    except OSError:
                        pass
                if phase == "clear":
                    try:
                        set_inheritable(False)
                    except Exception:
                        pass
        if not isinstance(error, Exception):
            raise
        if phase in ("setup", "clear"):
            cls = "OTHER"
        elif isinstance(error, subprocess.TimeoutExpired):
            cls = "TIMEOUT"
        elif isinstance(error, OSError) or phase == "assign":
            cls = "ENGINE_UNAVAILABLE"
        elif confirmed:
            raise
        else:
            cls = "OTHER"
        state = "may_have_been_sent" if phase == "communicate" else "not_sent"
        return None, cls, state if confirmed else "unconfirmed"


def call(verb: str, request: dict, timeout: float = 120, *,
         pass_fds=(), inherit_handles=(), kill_on_close_job=None) -> dict:
    """Run the engine with a JSON request and project its response."""
    inheriting = bool(pass_fds or inherit_handles or kill_on_close_job is not None)
    state = "not_sent"

    def finish(result):
        if inheriting:
            result["payload_state"] = state
        return result

    exe = Path(ENGINE_EXE)
    if not exe.is_file():
        return finish(_fail(verb, "ENGINE_UNAVAILABLE"))
    try:
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return finish(_fail(verb, "BAD_REQUEST"))
    argv = [str(exe), verb]
    options = dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   cwd=str(exe.parent), creationflags=_CREATE_NO_WINDOW)
    if inheriting:
        stdout, failure, state = _run_inheriting(
            argv, payload, timeout, options, pass_fds, inherit_handles, kill_on_close_job)
        if failure:
            return finish(_fail(verb, failure))
    else:
        try:
            stdout = subprocess.run(argv, input=payload, timeout=timeout, **options).stdout
        except subprocess.TimeoutExpired:
            return _fail(verb, "TIMEOUT")
        except OSError:
            return _fail(verb, "ENGINE_UNAVAILABLE")
    try:
        response = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return finish(_fail(verb, "ENGINE_UNAVAILABLE"))
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        return finish(_fail(verb, "ENGINE_UNAVAILABLE"))
    return finish(_project(verb, response, request))
