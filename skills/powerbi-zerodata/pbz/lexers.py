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


_NUM = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_IDENT_DAX = re.compile(r"[A-Za-z_À-￿][A-Za-z0-9_.À-￿]*")
_IDENT_M = re.compile(r"[A-Za-z_À-￿][A-Za-z0-9_.À-￿]*")
_OPS = ["==", "<>", "<=", ">=", "&&", "||", "=>", "..", "...", "=", "<", ">", "+", "-", "*", "/", "&", "^",
        "(", ")", "{", "}", ",", ";", ":", "!", "?", "@", "#", "."]


def _quoted(src: str, i: int, q: str) -> tuple[int, str]:
    """Scan a quote-delimited token starting at src[i] == q, doubled quote escapes. Returns (end, value)."""
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
        out.append(c)
        j += 1
    return n, "".join(out)  # unterminated: consume to end


def _bracket(src: str, i: int) -> tuple[int, str]:
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
        out.append(c)
        j += 1
    return n, "".join(out)


def lex_dax(src: str, base: int = 0) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
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
        elif c == "'":
            j, v = _quoted(src, i, "'")
            toks.append(Tok("table", base + i, base + j, src[i:j], v))
            i = j
        elif c == "[":
            j, v = _bracket(src, i)
            toks.append(Tok("bracket", base + i, base + j, src[i:j], v))
            i = j
        elif c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUM.match(src, i)
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
        if c.isspace():
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
        elif c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUM.match(src, i)
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
