"""Minimal DAX and Power Query M lexers with exact source spans.

Only what tokenization and reference analysis need: strings, numbers, identifiers, quoted
table names, bracketed column/measure refs, M quoted identifiers, comments, punctuation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Tok:
    kind: str      # str, num, ident, table, bracket, qident, comment, op, ws
    start: int
    end: int
    text: str      # raw source text
    value: str = ""  # decoded value for str/table/bracket/qident


_NUM = re.compile(r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _match_num(src: str, i: int):
    """ASCII number at i, False when that start did not match, None when it is not a number.

    Unicode digits are not numbers. Callers must not call end() on a failed match.
    """
    c = src[i]
    if not (("0" <= c <= "9") or (c == "." and i + 1 < len(src) and "0" <= src[i + 1] <= "9")):
        return None
    return _NUM.match(src, i) or False
_IDENT_DAX = re.compile(r"[A-Za-z_À-￿][A-Za-z0-9_.À-￿]*")
_IDENT_M = re.compile(r"[A-Za-z_À-￿][A-Za-z0-9_.À-￿]*")
_OPS = ["==", "<>", "<=", ">=", "&&", "||", "=>", "..", "...", "=", "<", ">", "+", "-", "*", "/", "&", "^",
        "(", ")", "{", "}", ",", ";", ":", "!", "?", "@", "#", "."]


def _quoted(src: str, i: int, q: str, one_line: bool = False) -> tuple[int, str]:
    """Scan a quote-delimited token starting at src[i] == q, doubled quote escapes. Returns (end, value).
    Unterminated: consume to end, or with one_line (names never span lines) return (-1, "")."""
    j = i + 1
    out = []
    n = len(src)
    while j < n:
        c = src[j]
        if c == q:
            if j + 1 < n and src[j + 1] == q:
                out.append(q)
                j += 2
                continue
            return j + 1, "".join(out)
        if one_line and c in "\r\n":
            return -1, ""
        out.append(c)
        j += 1
    return (-1, "") if one_line else (n, "".join(out))


def _bracket(src: str, i: int) -> tuple[int, str]:
    """[Name] with ]] escapes; names never span lines, so an unterminated bracket returns (-1, "")."""
    j = i + 1
    out = []
    n = len(src)
    while j < n:
        c = src[j]
        if c == "]":
            if j + 1 < n and src[j + 1] == "]":
                out.append("]")
                j += 2
                continue
            return j + 1, "".join(out)
        if c in "\r\n":
            return -1, ""
        out.append(c)
        j += 1
    return -1, ""


def _pbz_token(src: str, i: int) -> int:
    """End of a mirror token «...» starting at i, or -1."""
    if src[i] != "«":
        return -1
    j = src.find("»", i + 1, i + 302)
    return j + 1 if j > 0 and "\n" not in src[i:j] else -1


def lex_dax(src: str, base: int = 0) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        j = _pbz_token(src, i) if c == "«" else -1
        if j > 0:  # sanitized literal: opaque, never a reference
            toks.append(Tok("pbztok", base + i, base + j, src[i:j], src[i:j]))
            i = j
        elif c.isspace():
            j = i
            while j < n and src[j].isspace():
                j += 1
            toks.append(Tok("ws", base + i, base + j, src[i:j]))
            i = j
        elif src.startswith("//", i) or src.startswith("--", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            toks.append(Tok("comment", base + i, base + j, src[i:j]))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            toks.append(Tok("comment", base + i, base + j, src[i:j]))
            i = j
        elif c == '"':
            j, v = _quoted(src, i, '"')
            toks.append(Tok("str", base + i, base + j, src[i:j], v))
            i = j
        elif c in "'[" and (j_v := (_quoted(src, i, "'", one_line=True) if c == "'" else _bracket(src, i)))[0] > 0:
            j, v = j_v
            toks.append(Tok("table" if c == "'" else "bracket", base + i, base + j, src[i:j], v))
            i = j
        elif (m := _match_num(src, i)) is not None:
            if m is False:
                toks.append(Tok("op", base + i, base + i + 1, c))
                i += 1
            else:
                toks.append(Tok("num", base + i, base + m.end(), m.group(0)))
                i = m.end()
        else:
            m = _IDENT_DAX.match(src, i)
            if m:
                toks.append(Tok("ident", base + i, base + m.end(), m.group(0), m.group(0)))
                i = m.end()
                continue
            for op in sorted(_OPS, key=len, reverse=True):
                if src.startswith(op, i):
                    toks.append(Tok("op", base + i, base + i + len(op), op))
                    i += len(op)
                    break
            else:
                toks.append(Tok("op", base + i, base + i + 1, c))
                i += 1
    return toks


def lex_m(src: str, base: int = 0) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        j = _pbz_token(src, i) if c == "«" else -1
        if j > 0:
            toks.append(Tok("pbztok", base + i, base + j, src[i:j], src[i:j]))
            i = j
        elif c.isspace():
            j = i
            while j < n and src[j].isspace():
                j += 1
            toks.append(Tok("ws", base + i, base + j, src[i:j]))
            i = j
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            toks.append(Tok("comment", base + i, base + j, src[i:j]))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            toks.append(Tok("comment", base + i, base + j, src[i:j]))
            i = j
        elif c == "#" and i + 1 < n and src[i + 1] == '"':
            j, v = _quoted(src, i + 1, '"')
            toks.append(Tok("qident", base + i, base + j, src[i:j], v))
            i = j
        elif c == '"':
            j, v = _quoted(src, i, '"')
            toks.append(Tok("str", base + i, base + j, src[i:j], v))
            i = j
        elif c == "[":
            # field access / record field name: [Name] or [#"Name"]; record literals [a = 1] are
            # lexed as an opening bracket token followed by normal tokens.
            m = re.match(r"\[\s*([^\[\]=,\"#]+?)\s*\]", src[i:])
            if m:
                toks.append(Tok("bracket", base + i, base + i + m.end(), m.group(0), m.group(1).strip()))
                i += m.end()
            else:
                toks.append(Tok("op", base + i, base + i + 1, "["))
                i += 1
        elif c == "]":
            toks.append(Tok("op", base + i, base + i + 1, "]"))
            i += 1
        elif (m := _match_num(src, i)) is not None:
            if m is False:
                toks.append(Tok("op", base + i, base + i + 1, c))
                i += 1
            else:
                toks.append(Tok("num", base + i, base + m.end(), m.group(0)))
                i = m.end()
        else:
            m = _IDENT_M.match(src, i)
            if m:
                toks.append(Tok("ident", base + i, base + m.end(), m.group(0), m.group(0)))
                i = m.end()
                continue
            for op in sorted(_OPS, key=len, reverse=True):
                if src.startswith(op, i):
                    toks.append(Tok("op", base + i, base + i + len(op), op))
                    i += len(op)
                    break
            else:
                toks.append(Tok("op", base + i, base + i + 1, c))
                i += 1
    return toks


def significant(toks: list[Tok]) -> list[Tok]:
    return [t for t in toks if t.kind not in ("ws", "comment")]


def dax_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def dax_table_name(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def tmdl_name(name: str) -> str:
    """Quote a TMDL object name when needed."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return name
    return "'" + name.replace("'", "''") + "'"
