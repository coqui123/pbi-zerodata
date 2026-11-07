"""`pbz format`: local DAX formatting of measures, calculated columns and calculation items in the mirror.
Uppercases functions and keywords, normalises spacing, and breaks long calls one argument per line.
Safety gate: the formatted text must lex to the same tokens (functions and keywords compared without
case); expressions with comments or ``` blocks are skipped. Mirror tokens «...» stay atomic."""
from __future__ import annotations

import os

from . import lexers, tmdl
from .common import PbzError, commit_bytes, read_text, write_text
from .tokens import apply_edits

WIDTH = 90
KEYWORDS = {"VAR", "RETURN", "IN", "NOT", "AND", "OR", "ASC", "DESC", "DEFINE", "EVALUATE", "MEASURE", "ORDER",
            "BY", "START", "AT", "TRUE", "FALSE"}
BINARY = {"+", "-", "*", "/", "&", "&&", "||", "=", "==", "<>", "<", ">", "<=", ">=", "^"}
OPENERS, CLOSERS = {"(": ")", "{": "}"}, {")", "}"}


class _Group:
    def __init__(self, fn: str | None, open_: str):
        self.fn, self.open, self.args = fn, open_, [[]]


def _tree(sig, udfs: set[str]):
    """Nested items: tokens (as text) and _Group calls / parens / braces. None if unbalanced."""
    root = _Group(None, "")
    stack = [root]
    for i, t in enumerate(sig):
        nxt = sig[i + 1] if i + 1 < len(sig) else None
        if t.kind == "ident":
            up = t.text.upper()
            if nxt is not None and nxt.text == "(":
                txt = t.text if t.text.lower() in udfs else up
            else:
                txt = up if up in KEYWORDS else t.text
            stack[-1].args[-1].append(("ident", txt))
        elif t.kind == "op" and t.text in OPENERS:
            prev = stack[-1].args[-1][-1] if stack[-1].args[-1] else None
            fn = None
            if t.text == "(" and prev is not None and prev[0] == "ident" and isinstance(prev, tuple):
                fn = stack[-1].args[-1].pop()[1]
            g = _Group(fn, t.text)
            stack[-1].args[-1].append(g)
            stack.append(g)
        elif t.kind == "op" and t.text in CLOSERS:
            if len(stack) == 1 or OPENERS[stack[-1].open] != t.text:
                return None
            stack.pop()
        elif t.kind == "op" and t.text == "," and len(stack) > 1:
            stack[-1].args.append([])
        else:
            stack[-1].args[-1].append((t.kind, t.text))
    return root if len(stack) == 1 else None


WORDS = {"ident", "num", "str", "table", "bracket", "pbztok", "group"}


def _flat(items) -> str:
    seq: list[tuple[str, str]] = []
    for it in items:
        seq.append(("group", _flat_group(it)) if isinstance(it, _Group) else it)
    out = []
    for i, (kind, s) in enumerate(seq):
        if i:
            pk, pt = seq[i - 1]
            # a sign: -/+ at the start, or right after an operator or keyword
            unary_prev = pt in ("-", "+") and (i == 1 or seq[i - 2][1] in BINARY or
                                               seq[i - 2][1].upper() in ("VAR", "RETURN", "IN", "NOT", "AND", "OR"))
            if unary_prev:
                pass
            elif (kind == "op" and s in BINARY) or (pk == "op" and pt in BINARY):
                out.append(" ")
            elif pk in WORDS and kind in WORDS and not (pk in ("table", "ident") and kind == "bracket"):
                out.append(" ")
        out.append(s)
    return "".join(out)


def _flat_group(g: _Group) -> str:
    inner = ", ".join(_flat(a) for a in g.args if a or len(g.args) > 1)
    return (g.fn or "") + g.open + inner + OPENERS[g.open]


def _render(items, ind: int) -> list[str]:
    """Lines for an item list at indent level `ind` (in units of 4 spaces)."""
    flat = _flat(items)
    pad = "    " * ind
    if len(flat) + len(pad) <= WIDTH and not _has_var(items):
        return [pad + flat]
    lines: list[str] = []
    cur: list = []
    for it in items:  # VAR / RETURN start new lines
        if isinstance(it, tuple) and it[1] in ("VAR", "RETURN") and cur:
            lines += _render_seq(cur, ind)
            cur = []
        cur.append(it)
    lines += _render_seq(cur, ind)
    return lines


def _has_var(items) -> bool:
    return any(isinstance(it, tuple) and it[1] == "VAR" for it in items)


def _render_seq(items, ind: int) -> list[str]:
    pad = "    " * ind
    flat = _flat(items)
    if len(flat) + len(pad) <= WIDTH:
        return [pad + flat]
    groups = [i for i, it in enumerate(items) if isinstance(it, _Group)]
    if not groups:
        return [pad + flat]
    k = groups[-1]  # break the last call / group open, keep the head on one line
    head = _flat(items[:k])
    g = items[k]
    tail = _flat(items[k + 1:])
    lines = [pad + (head + " " if head and not head.endswith(("(", "{")) else head) + (g.fn or "") + g.open]
    args = [a for a in g.args if a or len(g.args) > 1]
    for j, a in enumerate(args):
        sub = _render(a, ind + 1)
        if j < len(args) - 1:
            sub[-1] += ","
        lines += sub
    lines.append(pad + OPENERS[g.open] + ((" " + tail) if tail and not tail.startswith((",", ")")) else tail))
    return lines


def format_dax(expr: str, udfs: set[str] = frozenset()) -> str | None:
    """Formatted expression text, or None when it cannot be formatted safely."""
    toks = lexers.lex_dax(expr)
    if any(t.kind == "comment" for t in toks):
        return None
    sig = lexers.significant(toks)
    if not sig:
        return None
    tree = _tree(sig, {u.lower() for u in udfs})
    if tree is None:
        return None
    out = "\n".join(_render(tree.args[0], 0))
    again = lexers.significant(lexers.lex_dax(out))
    if len(again) != len(sig):
        return None
    for a, b in zip(sig, again):
        same = a.text.upper() == b.text.upper() if a.kind == "ident" else a.text == b.text
        if a.kind != b.kind or not same:
            return None
    return out


def run(ws, project, apply: bool = False, target: str | None = None) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    udfs = set(model.functions)
    changed, skipped, unchanged = [], [], 0
    edits: dict[str, list] = {}
    for rel, doc in model.docs.items():
        lines = doc.text.splitlines(keepends=True)
        for reg in doc.regions:
            node = reg.node
            if reg.kind != "dax" or reg.prop != "expression" or node.kind not in ("measure", "column", "calculationItem"):
                continue
            label = f"{node.parent.name if node.parent else ''}[{node.name}]" if node.kind != "calculationItem" else node.name
            if target and target.strip("[]'").lower() not in (node.name.lower(), label.lower()):
                continue
            old = doc.text[reg.start:reg.end]
            if "```" in doc.text[max(0, reg.start - 8):reg.start] or old.lstrip().startswith("```"):
                skipped.append({"object": label, "reason": "``` block"})
                continue
            base = "\t" * (node.indent + 2)
            body = "\n".join(l.strip("\t") if l.startswith(base) else l.strip() for l in old.splitlines())
            new = format_dax(body, udfs)
            if new is None:
                skipped.append({"object": label, "reason": "comments or unparsed syntax"})
                continue
            eol = "\r\n" if "\r\n" in doc.text else "\n"
            decl_end = sum(len(l) for l in lines[:node.line + 1])
            on_decl = reg.start < decl_end
            if "\n" not in new and len(new) <= WIDTH:
                if on_decl and old == new:
                    unchanged += 1
                    continue
                rep = new
                start = reg.start
                if not on_decl:  # was multi-line: pull it back onto the declaration line
                    start = decl_end - len(eol) if doc.text[decl_end - 2:decl_end] == "\r\n" else decl_end - 1
                    rep = " " + new
            else:
                block = eol.join(base + l for l in new.split("\n"))
                if not on_decl and old.rstrip() == block.rstrip():
                    unchanged += 1
                    continue
                if on_decl:
                    start = reg.start - (1 if doc.text[reg.start - 1:reg.start] == " " else 0)
                    rep = eol + block
                else:
                    start, rep = reg.start, block
            edits.setdefault(rel, []).append((start, reg.end, rep))
            changed.append(label)
    if apply and edits:
        ops = []
        for rel, eds in edits.items():
            path = os.path.join(mdef, *rel.split("/"))
            _t, bom = read_text(path)
            ops.append(("write", path, apply_edits(model.docs[rel].text, sorted(eds)), bom))
        commit_bytes(ops)
    return {"verb": "format", "mode": "apply" if apply else "check", "formatted": len(changed), "unchanged": unchanged,
            "objects": changed[:200], "skipped": skipped[:100],
            **({"next": "pbz diff, then pbz push"} if apply and changed else {})}
