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
import math
import os
import secrets
import unicodedata

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _call(fn, data: bytes) -> bytes:
    src = _blob(data)
    out = _Blob()
    if not fn(ctypes.byref(src), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    fn = ctypes.windll.crypt32.CryptProtectData
    src = _blob(data)
    out = _Blob()
    if not fn(ctypes.byref(src), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def unprotect(data: bytes) -> bytes:
    return _call(ctypes.windll.crypt32.CryptUnprotectData, data)


def load_key(private_dir: str, create: bool = True) -> bytes:
    path = os.path.join(private_dir, "key.bin")
    try:
        with open(path, "rb") as f:
            return unprotect(f.read())
    except FileNotFoundError:
        if not create:
            raise
    os.makedirs(private_dir, exist_ok=True)
    key = secrets.token_bytes(32)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(protect(key))
    os.replace(tmp, path)
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


def write_protected(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(protect(data))
    os.replace(tmp, path)


def read_protected(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return unprotect(f.read())
    except FileNotFoundError:
        return None
