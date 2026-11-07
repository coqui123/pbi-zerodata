"""`pbz move measure <name> --to <table>`: move a measure to another table (for example a measure table)
in the mirror, fixing every reference that names its home table: qualified DAX references and the PBIR
entity of each visual, filter, sort and bookmark reference. Names only."""
from __future__ import annotations

import json
import os

from . import lexers, pbir, tmdl
from .common import PbzError, commit_bytes, read_text
from .tokens import apply_edits


def _insert_point(doc, tnode) -> tuple[int, str]:
    """Offset in the target table's file to insert a measure block, and the separator to put before it."""
    kids = [c for c in tnode.children if c.kind not in ("annotation", "extendedProperty", "changedProperty")]
    measures = [c for c in kids if c.kind == "measure"]
    if measures:
        return tmdl.block_span(doc, measures[-1])[1], ""
    if kids:
        return tmdl.block_span(doc, kids[0])[0], ""
    s, e = tmdl.block_span(doc, tnode)
    return e, "\n"


def dax_qualifier_edits(text: str, start: int, end: int, measure: str, src: str, dst: str) -> list[tuple[int, int, str]]:
    """Edits requalifying 'src'[measure] / src[measure] as 'dst'[measure] inside text[start:end] (one DAX
    expression). Offsets are in `text`."""
    low, slow = measure.lower(), src.lower()
    out = []
    sig = lexers.significant(lexers.lex_dax(text[start:end], start))
    for i, tok in enumerate(sig[:-1]):
        nxt = sig[i + 1]
        if nxt.kind == "bracket" and nxt.value.lower() == low and (
                (tok.kind == "table" and tok.value.lower() == slow) or (tok.kind == "ident" and tok.text.lower() == slow)):
            out.append((tok.start, tok.end, lexers.dax_table_name(dst)))
    return out


def pbir_move_edits(root, measure: str, src: str, dst: str) -> tuple[list[tuple[int, int, str]], bool]:
    """(edits, refused) moving the PBIR references of `measure` from entity `src` to `dst` in one parsed
    document: SourceRef entities, From aliases used only by the measure, and queryRef / metadata strings.
    refused: a From alias serves the measure and other fields of `src` (edit that filter by hand)."""
    low, slow = measure.lower(), src.lower()
    pedits = []
    scopes: list[dict] = []

    def visit(node, scope):
        if isinstance(node, pbir.JArr):
            for v in node.items:
                visit(v, scope)
            return
        if not isinstance(node, pbir.JObj):
            return
        frm = node.get("From")
        if isinstance(frm, pbir.JArr):
            scope = {"aliases": {}, "uses": {}, "moved": set()}
            for ent in frm.items:
                if isinstance(ent, pbir.JObj) and isinstance(ent.get("Name"), pbir.JStr) and isinstance(ent.get("Entity"), pbir.JStr):
                    scope["aliases"][ent.get("Name").value] = ent.get("Entity")
            scopes.append(scope)
        for key in ("Column", "Measure"):
            ref = node.get(key)
            if not isinstance(ref, pbir.JObj):
                continue
            prop, expr = ref.get("Property"), ref.get("Expression")
            sr = expr.get("SourceRef") if isinstance(expr, pbir.JObj) else None
            if not isinstance(sr, pbir.JObj):
                continue
            ent, alias = sr.get("Entity"), sr.get("Source")
            hit = key == "Measure" and isinstance(prop, pbir.JStr) and prop.value.lower() == low
            if isinstance(ent, pbir.JStr) and ent.value.lower() == slow and hit:
                pedits.append((ent.start, ent.end, json.dumps(dst, ensure_ascii=False)))
            elif isinstance(alias, pbir.JStr) and scope is not None:
                scope["uses"].setdefault(alias.value, []).append(hit)
        for _k, v in node.pairs:
            visit(v, scope)

    visit(root, None)
    refused = False
    for sc in scopes:
        for alias, hits in sc["uses"].items():
            ent = sc["aliases"].get(alias)
            if ent is None or ent.value.lower() != slow or not any(hits):
                continue
            if all(hits):
                pedits.append((ent.start, ent.end, json.dumps(dst, ensure_ascii=False)))
            else:
                refused = True
    for key, node in pbir.string_nodes(root):
        if key in ("queryRef", "metadata") and node.value.lower() == f"{slow}.{low}":
            pedits.append((node.start, node.end, json.dumps(f"{dst}.{measure}", ensure_ascii=False)))
    return pedits, refused


def _mirror_rel(ws, path: str) -> str:
    return os.path.relpath(path, ws.mirror).replace("\\", "/")


def plan_pbir(ws, report_dir, edit_fn) -> list[tuple]:
    """Planned PBIR writes: (path, text, bom, rel). Raises before any write.

    REFUSED lists files that do not parse. UNSUPPORTED lists shared-alias conflicts
    (a From alias used by this measure and by other fields of its old table).
    """
    bad, shared, plans = [], [], []
    for path in pbir.report_files(report_dir):
        rel = _mirror_rel(ws, path)
        text, bom = read_text(path)
        try:
            root = pbir.parse_json(text)
        except (pbir.JsonError, IndexError, ValueError):
            bad.append(rel)
            continue
        edits, file_refused = edit_fn(root, text)
        if file_refused:
            shared.append(rel)
            continue
        if edits:
            plans.append((path, apply_edits(text, sorted(set(edits))), bom, rel))
    if bad:
        raise PbzError("REFUSED", "PBIR parse failed: " + ", ".join(sorted(set(bad))))
    if shared:
        raise PbzError("UNSUPPORTED", "a filter shares one source alias between this measure and other fields of "
                       "its old table: " + ", ".join(sorted(set(shared))))
    return plans


def move_measure(ws, project, name: str, target: str) -> dict:
    from .rename import translated_names
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    m = model.measure(name)
    if m is None or m.node is None:
        raise PbzError("NOT_FOUND", f"measure {name}")
    src, dst = model.table(m.table), model.table(target)
    if dst is None or dst.node is None:
        raise PbzError("NOT_FOUND", f"table {target}")
    if src.name == dst.name:
        raise PbzError("BAD_REQUEST", "the measure already lives in that table")
    if dst.columns.get(m.name) or any(c.lower() == m.name.lower() for c in dst.columns):
        raise PbzError("NAME_TAKEN", f"{dst.name} has a column named {m.name}")
    if m.name.lower() in translated_names(project):
        raise PbzError("UNSUPPORTED", "cultures not in this mirror: run pbz pull")
    sdoc, ddoc = model.docs[src.file], model.docs[dst.file]
    s, e = tmdl.block_span(sdoc, m.node)
    block = sdoc.text[s:e]
    if not block.endswith("\n"):
        block += "\n"
    if not block.endswith("\n\n"):
        block += "\n"
    changed = []
    texts = {rel: doc.text for rel, doc in model.docs.items()}
    edits: dict[str, list] = {}
    for rel, doc in model.docs.items():  # qualified references 'Src'[Measure] -> 'Dst'[Measure]
        for reg in doc.regions:
            if reg.kind != "dax":
                continue
            for ed in dax_qualifier_edits(doc.text, reg.start, reg.end, m.name, src.name, dst.name):
                edits.setdefault(rel, []).append(ed)
    edits.setdefault(src.file, []).append((s, e, ""))
    at, sep = _insert_point(ddoc, dst.node)
    edits.setdefault(dst.file, []).append((at, at, sep + block))
    planned = dict(texts)
    for rel, eds in edits.items():
        planned[rel] = apply_edits(texts[rel], sorted(eds))
    pbir_plans = plan_pbir(ws, project.mirror_report,
                           lambda root, _text: pbir_move_edits(root, m.name, src.name, dst.name))
    from .governance import culture_move_texts, perspective_move_texts
    cultures = culture_move_texts(mdef, src.name, dst.name, m.name)
    perspectives = perspective_move_texts(mdef, src.name, dst.name, m.name, planned)
    ops = []
    for rel, text in planned.items():
        final = perspectives.get(rel, text)
        if final == texts[rel]:
            continue
        path = os.path.join(mdef, *rel.split("/"))
        _t, bom = read_text(path)
        ops.append(("write", path, final, bom))
        changed.append(f"{project.name}.SemanticModel/definition/{rel}")
    for rel, text in cultures.items():
        path = os.path.join(mdef, *rel.split("/"))
        _t, bom = read_text(path)
        ops.append(("write", path, text, bom))
        changed.append(f"{project.name}.SemanticModel/definition/{rel}")
    for path, text, bom, rel in pbir_plans:
        ops.append(("write", path, text, bom))
        changed.append(rel)
    commit_bytes(ops)
    return {"verb": "move", "measure": m.name, "from": src.name, "to": dst.name, "files": sorted(set(changed)),
            "next": "pbz diff, pbz validate, then pbz push"}
