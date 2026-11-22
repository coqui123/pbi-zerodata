"""Windows DPAPI (CurrentUser) and the shared HMAC used with pbz-engine.

Key file: <private_dir>/key.bin = DPAPI-protected 32 random bytes (CRYPTPROTECT_UI_FORBIDDEN,
no entropy). hmac(value) = hex HMAC-SHA256(key, utf8(normalize(value))), normalization identical
to engine/CONTRACT.md.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as _hmac
import importlib.util
import math
import os
import secrets
import tempfile
import unicodedata
from pathlib import Path

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _bind_dpapi():
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    protect_fn = crypt32.CryptProtectData
    unprotect_fn = crypt32.CryptUnprotectData
    local_free = kernel32.LocalFree
    blob_p = ctypes.POINTER(_Blob)
    protect_fn.argtypes = [blob_p, wt.LPCWSTR, blob_p, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, blob_p]
    protect_fn.restype = wt.BOOL
    unprotect_fn.argtypes = [blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, blob_p]
    unprotect_fn.restype = wt.BOOL
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    return protect_fn, unprotect_fn, local_free


_FNS = None


def _fns():
    global _FNS
    if _FNS is None:
        try:
            _FNS = _bind_dpapi()
        except AttributeError:
            raise RuntimeError("DPAPI is only available on Windows") from None
    return _FNS


_FAKE = None


def _fake_provider():
    global _FAKE
    if _FAKE is None:
        path = Path(__file__).resolve().parent.parent / "tests" / "_fake_dpapi.py"
        if not path.is_file():
            raise RuntimeError("PBZ_FAKE_DPAPI requested but the test fake provider is not present in this install")
        spec = importlib.util.spec_from_file_location("_pbz_fake_dpapi", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FAKE = module
    return _FAKE


def _use_fake():
    if os.environ.get("PBZ_FAKE_DPAPI") != "1":
        return False
    if os.environ.get("PBZ_TEST_HARNESS") != "1":
        raise RuntimeError("PBZ_FAKE_DPAPI is set outside the test harness; refusing to substitute the DPAPI provider")
    return True


def _blob(data: bytes) -> _Blob:
    # The struct stores a raw pointer. Keep the buffer alive until the DPAPI call returns.
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob._buf = buf
    return blob


def _free_out(out: _Blob) -> None:
    if out.pbData:
        _fns()[2](ctypes.cast(out.pbData, ctypes.c_void_p))


def _call(fn, data: bytes) -> bytes:
    src = _blob(data)
    out = _Blob()
    if not fn(ctypes.byref(src), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _free_out(out)


def protect(data: bytes) -> bytes:
    if _use_fake():
        return _fake_provider().protect(data)
    src = _blob(data)
    out = _Blob()
    if not _fns()[0](ctypes.byref(src), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _free_out(out)


def unprotect(data: bytes) -> bytes:
    if _use_fake():
        return _fake_provider().unprotect(data)
    return _call(_fns()[1], data)


def _refuse_link(path: str) -> None:
    from .common import PbzError, _reparse_link
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")


def _read_key_file(path: str) -> bytes:
    _refuse_link(path)
    with open(path, "rb") as handle:
        return unprotect(handle.read())


def load_key(private_dir: str, create: bool = True) -> bytes:
    path = os.path.join(private_dir, "key.bin")
    while True:
        try:
            return _read_key_file(path)
        except FileNotFoundError:
            if not create:
                raise
        os.makedirs(private_dir, exist_ok=True)
        key = secrets.token_bytes(32)
        payload = protect(key)
        fd, tmp = tempfile.mkstemp(prefix=".key-", suffix=".tmp", dir=private_dir)
        published = False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            try:
                # Existing key.bin belongs to the writer that won. Do not replace it.
                os.rename(tmp, path)
                published = True
            except FileExistsError:
                published = False
        finally:
            if not published:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if published:
            return key


# .NET char.IsWhiteSpace set (String.Trim). Python's str.strip() also strips U+001C..U+001F.
DOTNET_WS = "\t\n\v\f\r \x85\xa0 " + "".join(chr(c) for c in range(0x2000, 0x200B)) + "    　"


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s).strip(DOTNET_WS)
    # .NET ToLowerInvariant uses simple (1:1) case mapping
    return "".join("i" if ch == "İ" else (ch.lower() if len(ch.lower()) == 1 else ch) for ch in s)


def _dotnet_r(x: float) -> str:
    """double.ToString("R", InvariantCulture) on .NET 10 (scientific only when >= 1e17 or < 1e-4)."""
    from decimal import Decimal
    if x != x:
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign, digits, exp = Decimal(repr(x)).as_tuple()
    ds = "".join(map(str, digits)).lstrip("0")
    e = int(exp)
    while len(ds) > 1 and ds.endswith("0"):
        ds, e = ds[:-1], e + 1
    scale = len(ds) + e
    neg = "-" if sign else ""
    if scale > 17 or scale < -3:
        mant = ds[0] + ("." + ds[1:] if len(ds) > 1 else "")
        k = scale - 1
        return f"{neg}{mant}E{'+' if k >= 0 else '-'}{abs(k):02d}"
    if scale <= 0:
        return f"{neg}0.{'0' * (-scale)}{ds}"
    if scale >= len(ds):
        return f"{neg}{ds}{'0' * (scale - len(ds))}"
    return f"{neg}{ds[:scale]}.{ds[scale:]}"


def normalize(value) -> str:
    """Identical to pbz-engine's normalization (engine/README.md, 'Python side')."""
    import datetime as _dt
    if value is None:
        return "∅blank"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        r = float(f"{float(value):.12g}")
        return _dotnet_r(0.0 if r == 0 else r)
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return _norm_text(str(value))


def word_core(raw: str) -> str:
    """A word as the engine indexes it for word_hmacs: non-letters stripped at both ends."""
    a, b = 0, len(raw) - 1
    while a <= b and not raw[a].isalpha():
        a += 1
    while b >= a and not raw[b].isalpha():
        b -= 1
    return raw[a:b + 1] if a <= b else ""


def hmac_hex(key: bytes, value) -> str:
    return _hmac.new(key, normalize(value).encode("utf-8"), hashlib.sha256).hexdigest()


def write_protected(path: str, data: bytes) -> bytes:
    parent = os.path.dirname(path)
    _refuse_link(parent)
    _refuse_link(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    _refuse_link(tmp)
    with open(tmp, "wb") as f:
        protected = protect(data)
        f.write(protected)
    os.replace(tmp, path)
    return protected


def read_protected(path: str) -> bytes | None:
    try:
        _refuse_link(path)
        with open(path, "rb") as f:
            return unprotect(f.read())
    except FileNotFoundError:
        return None
