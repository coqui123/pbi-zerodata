"""Small, checked rewrites of one measure.

The lexer token stream may change only by the tokens the action names. DIVIDE is a semantic
change (blank divided by blank) and is written only with --accept-semantic. There is no fix-all.
"""
from __future__ import annotations

import os

from . import deps, lexers, suggest, tmdl
from .common import PbzError, commit_bytes, read_text
from .daxcatalog import compile_note

ACTIONS = ("unused-var", "qualify", "divide", "format", "description")


def _model(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    return mdef, tmdl.load_model(mdef)


def _measure(model, target: str):
    name = target.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    found = model.measure(name)
    if found is None:
        raise PbzError("NOT_FOUND", "measure not found")
    return found


def _region(model, measure):
    doc = model.docs[measure.node.file]
    reg = next((r for r in doc.regions if r.node is measure.node and r.kind == "dax"), None)
    if reg is None:
        raise PbzError("BAD_REQUEST", "measure has no expression")
    return doc, reg


def _sig(expr: str):
    return [(t.kind, t.text) for t in lexers.significant(lexers.lex_dax(expr))]


def _write_expr(ws, project, model, measure, new_expr: str, mdef: str):
    doc, reg = _region(model, measure)
    text = doc.text[:reg.start] + new_expr + doc.text[reg.end:]
    path = os.path.join(mdef, *measure.node.file.split("/"))
    _old, bom = read_text(path)
    commit_bytes([("write", path, text, bom)], root=ws.root)


def _compile(ws, project, model) -> dict:
    note = compile_note(ws, project, model)
    if note.get("status") != "DONE":
        return {"compile": "NOT_COMPILED"}
    return {"compile": "DONE", "compile_counts": note.get("counts", {})}


def _span_covers(spans, start: int, end: int) -> bool:
    return any(start < stop and end > begin for begin, stop in spans)


def _outside_brackets(expr: str, spans) -> list[str]:
    """Bracket tokens whose span does not overlap a deleted unused VAR."""
    return [tok.text for tok in lexers.lex_dax(expr)
            if tok.kind == "bracket" and not _span_covers(spans, tok.start, tok.end)]


def _require_outside_brackets(expr: str, spans, out: str) -> None:
    """Refuse when a bracket outside the deleted unused-VAR spans is missing from `out`."""
    outside = _outside_brackets(expr, spans)
    after = [tok.text for tok in lexers.lex_dax(out) if tok.kind == "bracket"]
    if not _sig(out) or outside != after:
        raise PbzError("REFUSED", "unused-var rewrite did not keep the rest of the expression")


def _return_without_var(sig) -> bool:
    """True when a RETURN is not closing an open VAR block in its parenthesis group.

    One VAR block may contain several VAR lines. A new block starts at the beginning of a
    group, after RETURN, or after an operator other than ")". RETURN closes one block.
    A comma ends a function argument, so a later RETURN cannot reuse an earlier block.
    A comma inside a table constructor does not end that block.
    """
    counts = [0]
    depth = 0
    braces = 0
    prev = None
    for tok in sig:
        if tok.kind == "op" and tok.text == "(":
            depth += 1
            if len(counts) <= depth:
                counts.append(0)
            else:
                counts[depth] = 0
            prev = tok
            continue
        if tok.kind == "op" and tok.text == ")":
            if depth == 0:
                return True
            counts[depth] = 0
            depth -= 1
            prev = tok
            continue
        if tok.kind == "op" and tok.text == "{":
            braces += 1
            prev = tok
            continue
        if tok.kind == "op" and tok.text == "}":
            braces = max(0, braces - 1)
            prev = tok
            continue
        if tok.kind == "op" and tok.text == "," and braces == 0:
            counts[depth] = 0
            prev = tok
            continue
        if tok.kind == "ident" and tok.text.upper() == "VAR":
            starts = prev is None or prev.text.upper() == "RETURN" or (
                prev.kind == "op" and prev.text not in (")", "}"))
            if starts:
                counts[depth] += 1
            prev = tok
            continue
        if tok.kind == "ident" and tok.text.upper() == "RETURN":
            if counts[depth] == 0:
                return True
            counts[depth] -= 1
            prev = tok
            continue
        prev = tok
    return False


def _unused(expr: str) -> str | None:
    names = set(suggest._unused_vars(expr))
    if not names:
        return None
    sig = lexers.significant(lexers.lex_dax(expr))
    # Drop each unused `VAR name = <simple>` that ends before RETURN or the next VAR.
    spans = []
    for i, tok in enumerate(sig):
        if not (tok.kind == "ident" and tok.text.upper() == "VAR" and i + 1 < len(sig)):
            continue
        name = sig[i + 1]
        if name.text not in names:
            continue
        end = None
        for later in sig[i + 2:]:
            if later.kind == "ident" and later.text.upper() in ("RETURN", "VAR"):
                end = later.start
                break
        if end is None:
            continue
        spans.append((tok.start, end))
    if not spans:
        return None
    out = expr
    for start, end in sorted(spans, reverse=True):
        out = out[:start] + out[end:]
    sig2 = lexers.significant(lexers.lex_dax(out))
    # Drop a leading RETURN only when no VAR remains.
    if sig2 and sig2[0].text.upper() == "RETURN" and not any(t.text.upper() == "VAR" for t in sig2):
        out = out[:sig2[0].start] + out[sig2[0].end:]
        sig2 = lexers.significant(lexers.lex_dax(out))
    # A nested VAR does not make a leading RETURN valid: `RETURN SUMX(..., VAR y = ...)` is refused.
    # A RETURN left inside a call, such as `SUMX(T, RETURN [M])`, is refused the same way.
    if (sig2 and sig2[0].text.upper() == "RETURN") or _return_without_var(sig2):
        raise PbzError("REFUSED", "unused-var rewrite did not keep the rest of the expression")
    # A RETURN that still has its VAR may remain. A bracket outside the deleted VAR span may not disappear.
    _require_outside_brackets(expr, spans, out)
    return out


def _qualify(expr: str, measure, model) -> str:
    res = deps.Resolver(model)
    info = deps.dax(expr, measure.table, res, row_home=False)
    # Bare column of the home table gains a qualifier. A qualified measure loses it.
    sig = lexers.lex_dax(expr)
    edits = []
    i = 0
    while i < len(sig):
        tok = sig[i]
        if tok.kind == "bracket" and (i == 0 or sig[i - 1].kind != "table"):
            col = res.column(measure.table, tok.value or tok.text.strip("[]"))
            meas = res.measure(tok.value or tok.text.strip("[]"))
            if col and not (meas and meas[0] != measure.table):
                edits.append((tok.start, tok.start, lexers.tmdl_name(measure.table)))
        if tok.kind == "table" and i + 1 < len(sig) and sig[i + 1].kind == "bracket":
            meas = res.measure(sig[i + 1].value or sig[i + 1].text.strip("[]"))
            if meas and tok.value.lower() == meas[0].lower():
                edits.append((tok.start, sig[i + 1].start, ""))
        i += 1
    if not edits:
        return expr
    out = expr
    for start, end, text in sorted(edits, reverse=True):
        out = out[:start] + text + out[end:]
    return out


def _divide(expr: str) -> str | None:
    sig = lexers.significant(lexers.lex_dax(expr))
    # The whole expression must be one slash between two standalone operands.
    if len(sig) != 3 or sig[1].text != "/":
        return None
    left, right = sig[0], sig[2]
    if left.kind not in ("bracket", "ident", "num") or right.kind not in ("bracket", "ident", "num"):
        return None
    return expr[:left.start] + f"DIVIDE({left.text}, {right.text})" + expr[right.end:]


def run(ws, project, action: str, target: str, *, accept_semantic: bool = False,
        format_string: str | None = None, description: str | None = None) -> dict:
    if action not in ACTIONS:
        raise PbzError("BAD_REQUEST", "rewrite action must be unused-var, qualify, divide, format or description")
    mdef, model = _model(project)
    measure = _measure(model, target)
    out = {"verb": "rewrite", "action": action, "object": f"[{measure.name}]"}
    if action in ("unused-var", "qualify", "divide"):
        doc, reg = _region(model, measure)
        expr = doc.text[reg.start:reg.end]
        if action == "unused-var":
            new = _unused(expr)
        elif action == "qualify":
            new = _qualify(expr, measure, model)
        else:
            new = _divide(expr)
            if new and not accept_semantic:
                raise PbzError("REFUSED", "DIVIDE changes blank-divide semantics; pass --accept-semantic")
        if not new or new == expr:
            return {**out, "result": "NOTHING_TO_CHANGE"}
        _write_expr(ws, project, model, measure, new, mdef)
        out["result"] = "CHANGED"
        out.update(_compile(ws, project, model))
        return out
    from . import props as props_mod
    if action == "format":
        if any(c.kind == "formatStringDefinition" for c in (measure.node.children or [])):
            raise PbzError("FORMAT_STRING_CONFLICT", "measure has a dynamic format string")
        if not format_string:
            raise PbzError("BAD_REQUEST", "--format is required")
        return {**out, **props_mod.run(ws, project, f"[{measure.name}]", [f"formatString={format_string}"])}
    if not description:
        raise PbzError("BAD_REQUEST", "--description is required")
    text = description.replace("{name}", measure.name).replace("{table}", measure.table)
    return {**out, **props_mod.run(ws, project, f"[{measure.name}]", [f"description={text}"])}
