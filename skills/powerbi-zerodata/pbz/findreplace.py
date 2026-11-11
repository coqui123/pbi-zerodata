"""Find and replace in the sanitized mirror.

A pattern that matches an object name is a rename, so PBIR and DAX references stay consistent.
Other hits are text edits outside token spans. Token syntax is never a search pattern, and a
replacement is never written inside a token or turned into a new token.
"""
from __future__ import annotations

import os
import re

from . import mirror, rename as rename_mod, tmdl
from .common import PbzError, commit_bytes, read_text
from .mirror_txn import transaction
from .tokens import TOKEN_RE


_BRACKETS = {ord("«"), ord("»")}


def _unescape(src: str) -> str:
    """Regex source with \\u, \\U and \\x escapes turned into characters. \\\\ stays a backslash."""
    out = []
    i, n = 0, len(src)
    hexdigits = "0123456789abcdefABCDEF"
    while i < n:
        if src[i] != "\\":
            out.append(src[i])
            i += 1
            continue
        if i + 1 >= n:
            out.append("\\")
            break
        nxt = src[i + 1]
        if nxt == "\\":
            out.append("\\")
            i += 2
            continue
        width = {"u": 4, "U": 8, "x": 2}.get(nxt)
        if width and i + 2 + width <= n and all(c in hexdigits for c in src[i + 2:i + 2 + width]):
            out.append(chr(int(src[i + 2:i + 2 + width], 16)))
            i += 2 + width
            continue
        out.append("\\")
        out.append(nxt)
        i += 2
    return "".join(out)


def _has_bracket(text: str) -> bool:
    if "«" in text or "»" in text or TOKEN_RE.fullmatch(text):
        return True
    decoded = _unescape(text)
    return "«" in decoded or "»" in decoded or bool(TOKEN_RE.fullmatch(decoded))


def _compiled_has_bracket(pattern: str) -> bool:
    """True when the compiled pattern matches « or » as a literal or a range, not merely as '.'."""
    from re._parser import LITERAL, RANGE, parse
    try:
        tree = parse(pattern)
    except re.error:
        return False

    def walk(node) -> bool:
        if isinstance(node, tuple) and len(node) == 2 and type(node[0]) is not int:
            op, av = node
            if op == LITERAL:
                return isinstance(av, int) and av in _BRACKETS
            if op == RANGE and isinstance(av, tuple) and len(av) == 2 and all(isinstance(x, int) for x in av):
                lo, hi = av
                return any(lo <= b <= hi for b in _BRACKETS)
            return walk(av)
        if isinstance(node, (list, tuple)):
            return any(walk(part) for part in node)
        if isinstance(node, int) or node is None or isinstance(node, str):
            return False
        try:
            return any(walk(part) for part in node)
        except TypeError:
            return False

    return walk(tree)


def _template_has_bracket(repl: str) -> bool:
    if _has_bracket(repl):
        return True
    try:
        from re._parser import parse_template
        parts = parse_template(repl, re.compile("a"))
    except re.error:
        return False
    return any(isinstance(part, str) and ("«" in part or "»" in part) for part in parts)


def _refuse_token_syntax(pattern: str, repl: str | None = None):
    if _has_bracket(pattern) or (repl is not None and _template_has_bracket(repl)):
        raise PbzError("REFUSED", "patterns must not match token syntax")
    try:
        rx = re.compile(pattern)
    except re.error as err:
        raise PbzError("BAD_REQUEST", "pattern is not a regular expression") from err
    if _compiled_has_bracket(pattern):
        raise PbzError("REFUSED", "patterns must not match token syntax")
    return rx


def _model(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    return tmdl.load_model(mdef)


def _names(model, kind: str):
    if kind in ("any", "table"):
        for table in model.tables.values():
            yield "table", table.name, None
    if kind in ("any", "column"):
        for table in model.tables.values():
            for column in table.columns.values():
                yield "column", column.name, table.name
    if kind in ("any", "measure"):
        for table in model.tables.values():
            for measure in table.measures.values():
                yield "measure", measure.name, table.name


def _name_jobs(model, kind: str, rx, repl: str):
    jobs = []
    for obj_kind, name, table in _names(model, kind):
        if not rx.search(name):
            continue
        new = rx.sub(repl, name)
        if new == name:
            continue
        if "«" in new or "»" in new:
            raise PbzError("REFUSED", "replacement must not create token syntax")
        jobs.append((obj_kind, name, new, table))
    # Columns and measures first, so a table rename still finds them by the old table name.
    jobs.sort(key=lambda job: {"column": 0, "measure": 1, "table": 2}[job[0]])
    return jobs


def _overlaps(start: int, end: int, spans) -> bool:
    return any(s < end and start < e for s, e in spans)


def sub_outside(text: str, rx, repl: str) -> str:
    """Replace outside mirror-token spans. The expanded template is checked, not only the raw pattern."""
    spans = [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]

    def _one(match):
        if _overlaps(match.start(), match.end(), spans):
            return match.group(0)
        try:
            new = match.expand(repl)
        except (re.error, IndexError) as err:
            raise PbzError("BAD_REQUEST", "replacement is not a valid template") from err
        if "«" in new or "»" in new or TOKEN_RE.search(new):
            raise PbzError("REFUSED", "replacement must not create token syntax")
        return new

    return rx.sub(_one, text)


def _snippet(line: str) -> str:
    line = line.strip()
    return line if len(line) <= 180 else line[:180]


def find(ws, project, pattern: str, kind: str = "any") -> dict:
    rx = _refuse_token_syntax(pattern)
    model = _model(project)
    name_hits = [{"kind": k, "name": name, "table": table}
                 for k, name, table in _names(model, kind) if rx.search(name)]
    snippets = []
    for rel, text in sorted(mirror.mirror_texts(ws, project).items()):
        for i, line in enumerate(text.split("\n"), 1):
            if rx.search(line) and len(snippets) < 50:
                snippets.append({"file": rel, "line": i, "text": _snippet(line)})
    return {"verb": "find", "project": project.name, "names": name_hits[:100],
            "name_count": len(name_hits), "snippets": snippets, "snippet_count": len(snippets)}


def _outside_count(texts, rx) -> int:
    total = 0
    for text in texts.values():
        spans = [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]
        total += sum(1 for m in rx.finditer(text) if not _overlaps(m.start(), m.end(), spans))
    return total


def replace(ws, project, pattern: str, repl: str, kind: str = "any", apply: bool = False) -> dict:
    rx = _refuse_token_syntax(pattern, repl)
    model = _model(project)
    jobs = _name_jobs(model, kind, rx, repl)
    planned = [{"kind": k, "from": old, "to": new, "table": table} for k, old, new, table in jobs]
    # A name hit is a rename. Text replacement runs only when no object name matched, so a
    # renamed Sales cannot be rewritten again into SalesDataData.
    outside = 0 if jobs else _outside_count(mirror.mirror_texts(ws, project), rx)
    out = {"verb": "replace", "project": project.name, "dry_run": not apply,
           "renames": planned, "text_hits": outside}
    if not apply:
        return out

    def _apply():
        for obj_kind, old, new, table in jobs:
            rename_mod.rename(ws, project, obj_kind, old, new, table)
        ops = []
        if not jobs:
            for rel, text in mirror.mirror_texts(ws, project).items():
                new_text = sub_outside(text, rx, repl)
                if new_text != text:
                    path = os.path.join(ws.mirror, *rel.split("/"))
                    _old, bom = read_text(path)
                    ops.append(("write", path, new_text, bom))
            if ops:
                commit_bytes(ops, root=ws.root)
        return {**out, "dry_run": False, "result": "CHANGED" if planned or ops else "NOTHING_TO_CHANGE"}

    if jobs or outside:
        return transaction(ws.mirror, _apply)
    return {**out, "dry_run": False, "result": "NOTHING_TO_CHANGE"}


def rename_match(ws, project, kind: str, pattern: str, repl: str, dry_run: bool = False) -> dict:
    rx = _refuse_token_syntax(pattern, repl)
    jobs = _name_jobs(_model(project), kind, rx, repl)
    planned = [{"kind": k, "from": old, "to": new, "table": table} for k, old, new, table in jobs]
    out = {"verb": "rename", "project": project.name, "matches": len(jobs), "renames": planned}
    if dry_run:
        return {**out, "dry_run": True}
    if not jobs:
        return {**out, "result": "NOTHING_TO_CHANGE"}

    def _apply():
        for obj_kind, old, new, table in jobs:
            rename_mod.rename(ws, project, obj_kind, old, new, table)
        return {**out, "result": "CHANGED"}

    return transaction(ws.mirror, _apply)
