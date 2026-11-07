"""`pbz consolidate merge | families | calc-group`: consolidation refactors on the mirror. Names and DAX
only, never values; mirror tokens are moved and copied exactly as they are.

- merge: retarget every reference to a duplicate measure onto its canonical twin (DAX anywhere in the
  model, PBIR fields, report-level measures, perspectives), drop its translations and remove it. A
  duplicate that registered consumers may bind by name (or whose usage is unknown) stays as a hidden alias
  `[dup] = [into]` with its lineageTag. Reuses the rename / move edit planners.
- families: calculation-group candidates. Each measure M that references a measure B gives a template:
  M's expression with every reference to B replaced by SELECTEDMEASURE(). A template applied to at least
  `min_bases` bases is a family template; templates sharing that many bases form one family (the items of
  one calculation group).
- calc_group: turns a family into a calculation group (one item per template) and rewrites each member as
  CALCULATE([Base], 'Group'[Column] = "Item"), keeping its name, lineageTag, formats, folder, description
  and visibility, so report bindings keep working. Writes a `pbz check` spec that compares every member's
  old expression with the new measure: run it on fake data (`pbz synth check --spec`) and, after the
  push, on the real model (`pbz check`).
Review with `pbz diff` / `pbz validate`, apply with `pbz push`.
"""
from __future__ import annotations

import json
import os
import re
import uuid

from . import deps, governance as gv, lexers, modeling as md, move, pbir, rename, scaffold, similar, tmdl, usage
from .common import PbzError, commit_bytes, read_text
from .tokens import TOKEN_RE, apply_edits

MODEL_AREAS = ("measures", "calculated_columns", "calculation_items", "calculated_tables", "format_strings",
               "detail_rows", "kpis", "rls_filters", "functions")
REPORT_AREAS = ("visual_projections", "filters", "sorts", "conditional_formatting", "bookmarks", "report_measures")
NODE_AREA = {"measure": "measures", "column": "calculated_columns", "calculationItem": "calculation_items",
             "calculationGroup": "calculation_items", "partition": "calculated_tables",
             "formatStringDefinition": "format_strings", "detailRowsDefinition": "detail_rows", "kpi": "kpis",
             "tablePermission": "rls_filters", "function": "functions"}
FIELD_STRING_KEYS = ("queryRef", "nativeQueryRef", "metadata")
TRIVIAL = ["SELECTEDMEASURE", "(", ")"]
REF_RES = (re.compile(r"^\s*'((?:[^']|'')+)'\s*\[(.+)\]\s*$"), re.compile(r"^\s*([^\[\]']+?)\s*\[(.+)\]\s*$"))
NEXT = "pbz diff, pbz validate, then pbz push"


# ---------------------------------------------------------------- shared helpers

def _model(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    return mdef, tmdl.load_model(mdef)


def _rel(project, rel: str) -> str:
    return f"{project.name}.SemanticModel/definition/{rel}"


def _write_ops(items: list[tuple[str, str]]) -> list[tuple]:
    """('write', path, text, bom) for commit_bytes. A missing path snapshots as None inside the commit."""
    ops = []
    for path, text in items:
        bom = read_text(path)[1] if os.path.exists(path) else b""
        ops.append(("write", path, text, bom))
    return ops


def _find_measure(model, ref: str):
    """A model measure from `[M]`, `M`, `'T'[M]` or `T[M]`."""
    ref = (ref or "").strip()
    table = None
    for rx in REF_RES:
        m = rx.match(ref)
        if m:
            table, ref = m.group(1).replace("''", "'"), m.group(2)
            break
    else:
        m = re.match(r"^\[(.+)\]$", ref)
        if m:
            ref = m.group(1)
    ref = ref.replace("]]", "]")
    hit = model.measure(ref)
    if hit is None or hit.node is None or (table and hit.table.lower() != table.lower()):
        raise PbzError("NOT_FOUND", f"measure {ref}")
    return hit


def _report_dirs(ws, project) -> list[tuple[str, str]]:
    """(report name, mirror report dir) for every mirrored report bound to the model, plus the project's own."""
    out = usage.bound_reports(ws, project)
    own = os.path.normcase(os.path.abspath(project.mirror_report))
    if os.path.isdir(project.mirror_report) and all(os.path.normcase(os.path.abspath(d)) != own for _n, d in out):
        out.append((project.name, project.mirror_report))
    return out


def _expr_span(doc, node) -> tuple[int, int]:
    """Span from just after the declaration's `=` to the end of the node's own expression (fences included)."""
    lines = tmdl._lines(doc.text)
    off, line = lines[node.line]
    m = tmdl.DECL_RE.match(line)
    if not m or m.group("rest") is None:
        raise PbzError("UNSUPPORTED", f"cannot locate the expression of {node.name}")
    start = off + line.rindex("=", 0, m.start("rest")) + 1
    if m.group("rest").strip().startswith("```"):
        j = node.line + 1
        while j < len(lines) and lines[j][1].strip() != "```":
            j += 1
        return start, (lines[j][0] + len(lines[j][1]) if j < len(lines) else len(doc.text))
    regs = [r for r in doc.regions if r.node is node and r.prop == "expression"]
    return start, max([r.end for r in regs] + [off + len(line)])


def _same_node(doc, node):
    return next(n for n in doc.nodes if n.kind == node.kind and n.name == node.name and
                (n.parent.name if n.parent else None) == (node.parent.name if node.parent else None))


def _dedent(expr: str) -> str:
    """Expression text without its TMDL indentation (the first line may follow the `=` directly)."""
    lines = expr.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0].strip()
    first, rest = lines[0], lines[1:]
    lead = first[:1] in (" ", "\t")
    pool = [ln for ln in (lines if lead else rest) if ln.strip()]
    ind = os.path.commonprefix([re.match(r"[ \t]*", ln).group(0) for ln in pool]) if pool else ""

    def cut(ln: str) -> str:
        return ln[len(ind):] if ln.startswith(ind) else ln.lstrip()

    return "\n".join([(cut(first) if lead else first.strip()).rstrip()] + [cut(ln).rstrip() if ln.strip() else "" for ln in rest])


def _multiline_string(expr: str) -> bool:
    return any(t.kind == "str" and "\n" in t.text for t in lexers.lex_dax(expr))


# ---------------------------------------------------------------- merge

def _format_of(model, m, res) -> tuple:
    """(static formatString, canonical formatStringDefinition) of a measure."""
    static = (m.node.props.get("formatString", "") if m.node else "").strip()
    if len(static) >= 2 and static[0] == static[-1] == '"':
        static = static[1:-1].replace('""', '"')
    fsd = next((c for c in (m.node.children if m.node else []) if c.kind == "formatStringDefinition"), None)
    dyn: tuple = ()
    if fsd is not None:
        doc = model.docs[m.node.file]
        text = "".join(doc.text[r.start:r.end] for r in doc.regions if r.node is fsd)
        dyn = tuple(similar.canon(text, m.table, res).words("hash"))
    return static, dyn


def _measure_closure(model, res, start) -> set[str]:
    """Lower-case names of the measures `start` reaches through DAX (expression, formats, KPI, detail rows)."""
    seen: set[str] = set()
    todo = [start]
    while todo:
        cur = todo.pop()
        if cur.node is None:
            continue
        doc = model.docs[cur.node.file]
        for reg in tmdl.node_regions(doc, cur.node, deep=True):
            if reg.kind != "dax":
                continue
            for ref in deps.dax(doc.text[reg.start:reg.end], cur.table, res).refs:
                if ref.kind == "measure" and ref.name.lower() not in seen:
                    seen.add(ref.name.lower())
                    nxt = model.measure(ref.name)
                    if nxt is not None:
                        todo.append(nxt)
    return seen


def _area(reg) -> str:
    return NODE_AREA.get(reg.node.kind, "other_model")


def _report_area(file_kind: str, path: tuple) -> str:
    if file_kind == "bookmark":
        return "bookmarks"
    via = usage._via(file_kind, path)
    if via.startswith("projection"):
        return "visual_projections"
    if via.startswith("filter"):
        return "filters"
    return {"sort": "sorts", "formatting": "conditional_formatting"}.get(via, "other_report")


def _retarget_dax(expr: str, d, t) -> tuple[str, int]:
    """A DAX string with every reference to measure d pointing at measure t (requalified first when the
    tables differ). Returns (new text, references rewritten)."""
    text = expr
    if d.table.lower() != t.table.lower():
        text = apply_edits(text, move.dax_qualifier_edits(text, 0, len(text), d.name, d.table, t.table))
    edits = rename.dax_ref_edits(text, 0, len(text), "measure", t.table, d.name, t.name)
    return apply_edits(text, edits), len(edits)


def _alias_text(doc, node, target: str) -> str:
    """The duplicate rewritten as a hidden alias of `target` (name, lineageTag and properties kept)."""
    s, e = _expr_span(doc, node)
    text = doc.text[:s] + " " + rename._bracket(target) + doc.text[e:]
    doc2 = tmdl.parse(doc.path, text)
    return apply_edits(text, tmdl.prop_edit(doc2, _same_node(doc2, node), "isHidden", None))


def _perspective_text(rel: str, text: str, d, t, alias: bool) -> tuple[str, int]:
    """Perspectives that list the duplicate list the canonical measure instead (the alias keeps its entry)."""
    changed = 0
    for pname in [n.name for n in tmdl.parse(rel, text).nodes if n.kind == "perspective"]:
        def persp():
            doc_ = tmdl.parse(rel, text)
            return doc_, next(n for n in doc_.nodes if n.kind == "perspective" and n.name == pname)

        doc, p = persp()
        pt = gv._child(p, "perspectiveTable", d.table)
        pm = gv._child(pt, "perspectiveMeasure", d.name) if pt is not None else None
        if pm is None:
            continue
        tt = gv._child(p, "perspectiveTable", t.table)
        has_into = tt is not None and gv._child(tt, "perspectiveMeasure", t.name) is not None
        before = text
        if not alias:
            text = apply_edits(text, [tmdl.block_span(doc, pm) + ("",)])
        if not has_into:
            doc, p = persp()
            if gv._child(p, "perspectiveTable", t.table) is None:
                text = gv._append_child(text, doc, p, f"\tperspectiveTable {gv._q(t.table)}")
                doc, p = persp()
            text = gv._append_child(text, doc, gv._child(p, "perspectiveTable", t.table),
                                    f"\t\tperspectiveMeasure {gv._q(t.name)}")
        changed += text != before
    return text, changed


def _report_measure_edits(root, d, t, counts: dict) -> list[tuple[int, int, str]]:
    """reportExtensions.json: DAX of report-level measures and their `references` entries."""
    out = []

    def refs(node):
        if isinstance(node, pbir.JArr):
            for v in node.items:
                refs(v)
        elif isinstance(node, pbir.JObj):
            ent, name, schema = node.get("entity"), node.get("name"), node.get("schema")
            if isinstance(ent, pbir.JStr) and isinstance(name, pbir.JStr) and not isinstance(schema, pbir.JStr) \
                    and ent.value.lower() == d.table.lower() and name.value.lower() == d.name.lower():
                out.append((ent.start, ent.end, json.dumps(t.table, ensure_ascii=False)))
                out.append((name.start, name.end, json.dumps(t.name, ensure_ascii=False)))
            for _k, v in node.pairs:
                refs(v)

    ents = root.get("entities") if isinstance(root, pbir.JObj) else None
    for ent in (ents.items if isinstance(ents, pbir.JArr) else []):
        ms = ent.get("measures") if isinstance(ent, pbir.JObj) else None
        for mm in (ms.items if isinstance(ms, pbir.JArr) else []):
            if not isinstance(mm, pbir.JObj):
                continue
            ex = mm.get("expression")
            if isinstance(ex, pbir.JStr):
                new, n = _retarget_dax(ex.value, d, t)
                if n:
                    counts["report_measures"] += n
                    out.append((ex.start, ex.end, json.dumps(new, ensure_ascii=False)))
            refs(mm.get("references"))
    return out


def merge(ws, project, dup: str, into: str, keep_alias: bool | None = None, force: bool = False,
          dry_run: bool = False) -> dict:
    """Retarget every reference to measure `dup` onto `into`, then remove `dup` (or keep it as a hidden alias)."""
    mdef, model = _model(project)
    d, t = _find_measure(model, dup), _find_measure(model, into)
    if d.table.lower() == t.table.lower() and d.name.lower() == t.name.lower():
        raise PbzError("BAD_REQUEST", "dup and into are the same measure")
    for nm in (d.name, t.name):
        if any(c.name.lower() == nm.lower() for c in model.all_columns()):
            raise PbzError("AMBIGUOUS", f"a column is also named {nm}: bare [{nm}] references are ambiguous; rename it first")
    res = deps.Resolver(model)
    if d.name.lower() in _measure_closure(model, res, t):
        raise PbzError("CYCLE", f"[{t.name}] references [{d.name}]: after the merge it would reference itself")
    cd, ct = similar.canon(d.expression, d.table, res), similar.canon(t.expression, t.table, res)
    disc = similar.discovery(cd).toks
    if cd.words("hash") == ct.words("hash"):
        equivalence = "identical"
    elif len(disc) == 1 and disc[0][1] == "meas" and disc[0][2].lower() == f"[{t.name}]".lower():
        equivalence = "alias"
    elif force:
        equivalence = "forced"
    else:
        raise PbzError("NOT_EQUIVALENT", f"[{d.name}] and [{t.name}] differ after normalization (pbz similar shows "
                                         "how); pass --force only if you have checked they agree")
    if _format_of(model, d, res) != _format_of(model, t, res) and not force:
        raise PbzError("FORMAT_DIFFERS", f"[{d.name}] and [{t.name}] have different format strings: references would "
                                         f"show [{t.name}]'s format. Align them (pbz set / pbz format-expr) or pass --force")
    if d.name.lower() in rename.translated_names(project):
        raise PbzError("UNSUPPORTED", "cultures not in this mirror: run pbz pull")

    # ---- who binds the duplicate by name outside the scanned reports
    g = usage.build(ws, project)
    dk = usage.key("measure", d.table, d.name)
    state = g.state.get(dk, "unknown")
    direct = sorted({u.report for u in g.ext_usages.get(dk, [])})
    need_alias = state in ("used_external", "unknown") or bool(direct) or g.incomplete
    alias = need_alias if keep_alias is None else bool(keep_alias)
    if direct:
        reason = "registered consumers use it: " + ", ".join(direct)
    elif state == "used_external":
        reason = "a registered consumer reaches it through another object"
    elif need_alias:
        reason = "the usage analysis is incomplete (an unreadable report file or consumer)"
    else:
        reason = "forced" if alias else ""
    warnings = []
    if need_alias and not alias:
        warnings.append({"code": "USED_EXTERNALLY", "consumers": direct, "state": state,
                         "message": "external consumers may bind this name and break after the merge (keep_alias)"})
    gone = [c.kind for c in d.node.children if c.kind in ("kpi", "detailRowsDefinition")] if not alias else []
    if gone:
        warnings.append({"code": "DROPPED_WITH_DUPLICATE", "objects": sorted(set(gone)),
                         "message": f"[{d.name}] carries objects [{t.name}] does not inherit; recreate them if needed"})

    counts = {a: 0 for a in MODEL_AREAS + REPORT_AREAS + ("perspectives", "translations")}
    texts = {rel: doc.text for rel, doc in model.docs.items()}
    # ---- the duplicate itself: removed, or rewritten as the hidden alias
    ddoc = model.docs[d.node.file]
    if alias:
        texts[d.node.file] = _alias_text(ddoc, d.node, t.name)
    else:
        texts[d.node.file] = apply_edits(ddoc.text, [tmdl.block_span(ddoc, d.node) + ("",)])
    # ---- model DAX: requalify 'Src'[dup] (move planner), then [dup] -> [into] (rename planner)
    cross = d.table.lower() != t.table.lower()
    named: dict[str, int] = {}   # DAX string literals spelling the duplicate's name (never rewritten)
    for rel in list(texts):
        text = texts[rel]
        doc = model.docs[rel] if text == model.docs[rel].text else tmdl.parse(rel, text)
        if cross:
            a = [ed for reg in doc.regions if reg.kind == "dax"
                 for ed in move.dax_qualifier_edits(text, reg.start, reg.end, d.name, d.table, t.table)]
            if a:
                text = apply_edits(text, a)
                doc = tmdl.parse(rel, text)
        b = []
        for reg in doc.regions:
            if reg.kind != "dax":
                continue
            es = rename.dax_ref_edits(text, reg.start, reg.end, "measure", t.table, d.name, t.name)
            if es:
                counts[_area(reg)] = counts.get(_area(reg), 0) + len(es)
                b += es
            if any(tk.kind == "str" and tk.value.lower() == d.name.lower() for tk in lexers.lex_dax(text[reg.start:reg.end])):
                named[_area(reg)] = named.get(_area(reg), 0) + 1
        texts[rel] = apply_edits(text, b) if b else text
    if named:
        warnings.append({"code": "NAME_IN_STRINGS", "areas": named,
                         "message": f"DAX text literals spelling \"{d.name}\" stay as they are (SELECTEDMEASURENAME() "
                                    "comparisons, field parameter labels): review them"})
    for rel in sorted({n.file for n in model.perspectives.values()}):
        texts[rel], n = _perspective_text(rel, texts[rel], d, t, alias)
        counts["perspectives"] += n
    plan: dict[str, str] = {os.path.join(mdef, *rel.split("/")): text
                            for rel, text in texts.items() if text != model.docs[rel].text}
    if not alias:  # translations of the duplicate go with it
        counts["translations"] = sum(len(gv._culture_objects(doc, "measure", d.table, d.name))
                                     for doc in gv.culture_docs(mdef).values())
        for rel, text in gv.culture_edits_drop(mdef, [("measure", d.table, d.name)]).items():
            plan[os.path.join(mdef, *rel.split("/"))] = text
    # ---- reports: entities (move planner), properties and queryRefs (rename planner), report measures
    refused, leftovers, bad = [], [], []
    olds = {f"{d.table}.{d.name}".lower(), f"{t.table}.{d.name}".lower()}
    for _rname, rdir in _report_dirs(ws, project):
        ddir = os.path.join(rdir, "definition")
        for path in pbir.report_files(rdir):
            rel = os.path.relpath(path, ws.mirror).replace("\\", "/")
            try:
                text0 = read_text(path)[0]
                root = pbir.parse_json(text0)
            except (pbir.JsonError, IndexError, ValueError):
                bad.append(rel)
                continue
            fkind = usage._file_kind(os.path.relpath(path, ddir).replace("\\", "/"))
            text = text0
            try:
                if cross:
                    a, no = move.pbir_move_edits(root, d.name, d.table, t.table)
                    if no:
                        refused.append(rel)
                        continue
                    if a:
                        text = apply_edits(text, sorted(set(a)))
                        root = pbir.parse_json(text)
                b = rename.pbir_ref_edits(root, model, "measure", t.table, d.name, t.name)
                if b:
                    where = {f.prop_node.start: p for f, p in pbir.walk_fields_with_path(root, model) if f.prop_node is not None}
                    for s, _e, _r in b:
                        if s in where:
                            area = _report_area(fkind, where[s])
                            counts[area] = counts.get(area, 0) + 1
                    text = apply_edits(text, sorted(set(b)))
                    root = pbir.parse_json(text)
                c = [(n.start, n.end, json.dumps(f"{t.table}.{t.name}", ensure_ascii=False))
                     for key, n in pbir.string_nodes(root) if key not in FIELD_STRING_KEYS and n.value.lower() in olds]
                if fkind == "extensions":
                    c += _report_measure_edits(root, d, t, counts)
                if c:
                    text = apply_edits(text, sorted(set(c)))
                if not alias and any(f.kind == "measure" and f.name.lower() == d.name.lower() and f.table.lower() in
                                     (d.table.lower(), t.table.lower()) for f in pbir.walk_fields(pbir.parse_json(text), model)):
                    leftovers.append(rel)
            except (pbir.JsonError, IndexError, ValueError):
                bad.append(rel)
                continue
            if text != text0:
                plan[path] = text
    if bad:
        raise PbzError("REFUSED", "PBIR parse failed: " + ", ".join(sorted(set(bad))))
    if refused:
        raise PbzError("UNSUPPORTED", "a filter shares one source alias between this measure and other fields of "
                                      f"{d.table} in " + ", ".join(sorted(refused)) + ": edit those filters by hand "
                                      "(or merge into a measure of the same table)")
    if leftovers:
        warnings.append({"code": "UNRETARGETED", "files": sorted(leftovers),
                         "message": f"references to [{d.name}] remain (pbz validate names them): fix them by hand"})
    files = sorted(os.path.relpath(p, ws.mirror).replace("\\", "/") for p in plan)
    if not dry_run:
        commit_bytes(_write_ops(list(plan.items())))
    rewritten = {k: v for k, v in counts.items() if v or k in MODEL_AREAS + REPORT_AREAS + ("perspectives", "translations")}
    out = {"verb": "consolidate", "action": "merge", "result": "DRY_RUN" if dry_run else "MERGED",
           "dup": f"[{d.name}]", "into": f"[{t.name}]", "tables": {"dup": d.table, "into": t.table},
           "equivalence": equivalence, "rewritten": rewritten,
           "references": sum(v for k, v in counts.items() if k not in ("perspectives", "translations")),
           "alias_kept": alias, "files": files, "next": NEXT}
    if alias:
        out["alias"] = {"measure": f"[{d.name}]", "table": d.table, "expression": f"[{t.name}]", "hidden": True,
                        "reason": reason}
    if warnings:
        out["warnings"] = warnings
    return out


# ---------------------------------------------------------------- families

def _measure_ref_spans(expr: str, res) -> list[tuple[int, int, str]]:
    """(start, end, measure name) of every measure reference in one DAX expression (resolved like deps.dax:
    'T'[x] / T[x] when x is no column of T, bare [x] when a measure has that name)."""
    sig = lexers.significant(lexers.lex_dax(expr))
    local = deps._vars_and_params(sig)
    out = []
    i, n = 0, len(sig)
    while i < n:
        t = sig[i]
        nxt = sig[i + 1] if i + 1 < n else None
        if t.kind in ("table", "ident") and nxt is not None and nxt.kind == "bracket" and (
                t.kind == "table" or t.text.lower() in local or res.table(t.text)):
            qual = t.value if t.kind == "table" else t.text
            tab = None if (t.kind == "ident" and qual.lower() in local) else res.table(qual)
            if tab and not res.column(tab, nxt.value) and res.measure(nxt.value):
                out.append((t.start, nxt.end, res.measure(nxt.value)[1]))
            i += 2
            continue
        if t.kind == "bracket" and res.measure(t.value):
            out.append((t.start, t.end, res.measure(t.value)[1]))
        i += 1
    return out


def _clean(s: str) -> str:
    return s.strip(" \t-_:|/()[]")


def _affix(base: str, member: str) -> str:
    """What the member's name adds to its base's name ("Sales YTD" vs "Sales" -> "YTD"), else ""."""
    b, m = base.strip(), member.strip()
    if len(m) > len(b):
        if m.lower().startswith(b.lower()):
            return _clean(m[len(b):])
        if m.lower().endswith(b.lower()):
            return _clean(m[:len(m) - len(b)])
    rest = m.split()
    for w in b.lower().split():
        k = next((i for i, x in enumerate(rest) if x.lower() == w), None)
        if k is None:
            return ""
        rest.pop(k)
    return _clean(" ".join(rest))


def _item_name(members: dict) -> str:
    affixes = {_affix(b, m).lower(): _affix(b, m) for b, m in members.items()}
    if len(affixes) == 1:
        a = next(iter(affixes.values()))
        if a and not TOKEN_RE.search(a) and len(a) <= 60:
            return a
    words = [m.split() for m in members.values()]
    pre, suf = [], []
    for col in zip(*words):
        if len({w.lower() for w in col}) != 1:
            break
        pre.append(col[0])
    for col in zip(*[list(reversed(w)) for w in words]):
        if len({w.lower() for w in col}) != 1:
            break
        suf.insert(0, col[0])
    names = {m.lower() for m in members.values()} | {b.lower() for b in members}
    for cand in (" ".join(suf), " ".join(pre)):
        c = _clean(cand)
        if c and c.lower() not in names and not TOKEN_RE.search(c) and len(c) <= 60:
            return c
    return ""


def families(model, min_bases: int = 3) -> list[dict]:
    """Calculation-group candidates: measures that apply one pattern to at least `min_bases` base measures.
    Names and DAX only."""
    res = deps.Resolver(model)
    cg_tables = {t.name.lower() for t in model.tables.values()
                 if t.calc_items or any(c.kind == "calculationGroup" for c in (t.node.children if t.node else []))}
    groups: dict[str, dict] = {}   # template key -> {"bases": {base: member}, "text": {base: template}, "dups": [...]}
    for m in sorted(model.all_measures(), key=lambda x: (x.name.lower(), x.table.lower())):
        expr = m.expression
        if not expr.strip():
            continue
        by_base: dict[str, list] = {}
        for s, e, base in _measure_ref_spans(expr, res):
            if base.lower() != m.name.lower():
                by_base.setdefault(base, []).append((s, e))
        for base, spans in by_base.items():
            tmpl = apply_edits(expr, [(s, e, "SELECTEDMEASURE()") for s, e in spans])
            if _multiline_string(tmpl):
                continue
            c = similar.canon(tmpl, m.table, res)
            if similar.discovery(c).words("hash") == TRIVIAL or any(
                    r.split("[", 1)[0].lower() in cg_tables for r in c.refs()):
                continue  # an alias, or already a wrapper that applies a calculation group
            key = similar._h(" ".join(c.words("hash")))
            grp = groups.setdefault(key, {"bases": {}, "text": {}, "dups": []})
            if base in grp["bases"]:
                grp["dups"].append((m.name, grp["bases"][base]))
                continue
            grp["bases"][base] = m.name
            grp["text"][base] = _dedent(tmpl)
    cands = sorted((k for k, grp in groups.items() if len(grp["bases"]) >= min_bases),
                   key=lambda k: (-len(groups[k]["bases"]), sorted(v.lower() for v in groups[k]["bases"].values())))
    parent = {k: k for k in cands}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            if len({x.lower() for x in groups[a]["bases"]} & {x.lower() for x in groups[b]["bases"]}) >= min_bases:
                parent[find(b)] = find(a)
    clusters: dict[str, list] = {}
    for k in cands:
        clusters.setdefault(find(k), []).append(k)
    out = []
    for keys in clusters.values():
        assigned: set[str] = set()
        templates = []
        for k in keys:
            mem = {b: mb for b, mb in sorted(groups[k]["bases"].items(), key=lambda x: x[0].lower())
                   if mb.lower() not in assigned}
            if len(mem) < min_bases:
                continue  # its members already belong to a wider template of this family
            assigned |= {mb.lower() for mb in mem.values()}
            templates.append({"key": k, "expression": groups[k]["text"][next(iter(mem))], "members": mem})
        if not templates:
            continue
        used: set[str] = set()
        for i, tp in enumerate(templates, 1):
            nm = _item_name(tp["members"]) or f"Item {i}"
            cand, n = nm, 2
            while cand.lower() in used:
                cand, n = f"{nm} {n}", n + 1
            used.add(cand.lower())
            tp["item"] = cand
        members = sorted({mb for tp in templates for mb in tp["members"].values()}, key=str.lower)
        bases = sorted({b for tp in templates for b in tp["members"]}, key=str.lower)
        fid = similar._h("|".join(x.lower() for x in members) + "#" + "|".join(sorted(tp["key"] for tp in templates)))[:10]
        fam = {"id": fid,
               "templates": [{"item": tp["item"], "expression": tp["expression"], "members": tp["members"]} for tp in templates],
               "bases": bases, "members": members, "confidence": "high" if len(templates) >= 2 else "medium"}
        notes = []
        nested = [b for b in bases if model.measure(b) is not None and _measure_ref_spans(model.measure(b).expression, res)]
        if nested:
            notes.append({"code": "NESTED_MEASURES", "measures": [f"[{b}]" for b in nested],
                          "message": "these bases reference other measures; a calculation item may also apply to "
                                     "those references: run the verify spec on fake data first"})
        refs = sorted({f"[{x}]" for tp in templates for _s, _e, x in _measure_ref_spans(tp["expression"], res)})
        if refs:
            notes.append({"code": "ITEM_REFERENCES_MEASURES", "measures": refs,
                          "message": "calculation items that reference measures can apply to them too: verify"})
        dups = sorted({(a, b) for k in keys for a, b in groups[k]["dups"] if b.lower() in assigned})
        if dups:
            notes.append({"code": "DUPLICATE_MEMBERS", "pairs": [[f"[{a}]", f"[{b}]"] for a, b in dups],
                          "message": "identical members: pbz consolidate merge the first into the second, then rerun"})
        if notes:
            fam["notes"] = notes
        out.append(fam)
    out.sort(key=lambda f: (-len(f["members"]), f["id"]))
    return out


def families_report(ws, project, min_bases: int = 3) -> dict:
    """families() on the mirror model, wrapped as verb output."""
    _mdef, model = _model(project)
    fams = families(model, min_bases)
    return {"verb": "consolidate", "action": "families", "project": project.name, "min_bases": min_bases,
            "families": fams, "next": "pbz consolidate calc-group --family <id> --name <table> [--dry-run]" if fams else
            "no measure pattern applies to enough base measures"}


# ---------------------------------------------------------------- calculation group

def _pick_family(model, family: str | None, measures: list[str] | None) -> dict:
    if not family and not measures:
        raise PbzError("BAD_REQUEST", "name a family id (pbz consolidate families) or member measures")
    want = {_find_measure(model, m).name.lower() for m in measures or []}
    for mb in (3, 2):
        for f in families(model, min_bases=mb):
            if family and f["id"] != family:
                continue
            if want and not want <= {m.lower() for m in f["members"]}:
                continue
            return f
    raise PbzError("NOT_FOUND", f"no calculation-group family {family}" if family else
                   "no calculation-group family covers those measures")


def _date_grain(model) -> list[str]:
    """Year and Month columns of the marked date table (else a table named like one), for grouped checks."""
    marked = [t for t in model.tables.values() if not t.private and t.node is not None
              and t.node.props.get("dataCategory", "").strip().lower() == "time"]
    named = [t for t in model.tables.values() if not t.private and t.name.lower() in ("date", "dates", "calendar", "dimdate")]
    for t in marked + named:
        by = []
        for want in ("year", "month"):
            c = next((c for c in t.columns.values() if c.name.lower() == want), None)
            if c is not None:
                by.append(f"{lexers.dax_table_name(t.name)}[{c.name.replace(']', ']]')}]")
        if by:
            return by
    return []


def _implicit_visuals(ws, project, model) -> list[str]:
    """Visuals that aggregate columns in their query (implicit measures; see BPA AVOID_IMPLICIT_MEASURES).

    An unreadable or unparsable visual refuses before any calc-group write.
    """
    hits, bad = [], []
    for rname, rdir in _report_dirs(ws, project):
        ddir = os.path.join(rdir, "definition")
        for path in pbir.report_files(rdir):
            rel = os.path.relpath(path, ddir).replace("\\", "/")
            if usage._file_kind(rel) != "visual":
                continue
            mirror_rel = os.path.relpath(path, ws.mirror).replace("\\", "/")
            try:
                root = pbir.parse_json(read_text(path)[0])
                if any(f.kind == "aggregation" and "queryState" in p for f, p in pbir.walk_fields_with_path(root, model)):
                    parts = rel.split("/")
                    hits.append(f"{rname} | {parts[1]} | {parts[3]}")
            except (OSError, UnicodeDecodeError, pbir.JsonError, IndexError, ValueError):
                bad.append(mirror_rel)
    if bad:
        raise PbzError("REFUSED", "PBIR parse failed: " + ", ".join(sorted(set(bad))))
    return hits


def _item_block(item: str, expr: str, ordinal: int) -> str:
    if "\n" in expr:
        body = "\n" + "\n".join(("\t\t\t\t" + ln) if ln.strip() else "" for ln in expr.split("\n"))
        head = f"\t\tcalculationItem {scaffold._q(item)} =" + body
    else:
        head = f"\t\tcalculationItem {scaffold._q(item)} = {expr}"
    return head.rstrip() + f"\n\t\t\tordinal: {ordinal}\n\n"


def calc_group(ws, project, family: str | None = None, measures: list[str] | None = None, name: str = "Calculation",
               column: str = "Name", precedence: int | None = None, dry_run: bool = False) -> dict:
    """Create a calculation group from a family and rewrite its members as wrappers over their bases."""
    mdef, model = _model(project)
    fam = _pick_family(model, family, measures)
    scaffold._check_new_table(model, name)
    if os.path.exists(os.path.join(mdef, "tables", scaffold._file_name(name) + ".tmdl")):
        raise PbzError("NAME_TAKEN", f"file tables/{scaffold._file_name(name)}.tmdl exists")
    if not column or column != column.strip() or re.search(r"[\r\n\t]", column) or column.lower() == "ordinal":
        raise PbzError("BAD_REQUEST", "the calculation column needs a plain name other than Ordinal")
    items = [tp["item"] for tp in fam["templates"]]
    scaffold._literal_guard(name, column, *items)
    others = []
    for tb in model.tables.values():
        for c in (tb.node.children if tb.node else []):
            if c.kind == "calculationGroup":
                p = c.props.get("precedence", "0").strip()
                others.append({"table": tb.name, "precedence": int(p) if p.isdigit() else 0})
    used = {o["precedence"] for o in others}
    prec = precedence if precedence is not None else (max(used) + 1 if used else 1)
    if prec in used:
        raise PbzError("NAME_TAKEN", f"precedence {prec} is used by another calculation group")
    cur, _p = md.compat_level(mdef)
    need = md.COMPAT["calculationGroup"]
    note = f"compatibility level raised from {cur} to {need}" if cur is not None and cur < need else None

    # ---- the calculation group table (scaffold writer conventions)
    text = (f"table {scaffold._q(name)}\n\tlineageTag: {uuid.uuid4()}\n\n\tcalculationGroup\n\t\tprecedence: {prec}\n\n"
            + "".join(_item_block(tp["item"], tp["expression"], i) for i, tp in enumerate(fam["templates"]))
            + scaffold._column(column, "string", "Name", sort_by="Ordinal")
            + scaffold._column("Ordinal", "int64", "Ordinal", hidden=True, summarize="sum", fmt="0")
            + f"\tpartition {scaffold._q(name)} = calculationGroup\n\t\tmode: import\n")
    flags = ["discourageImplicitMeasures"] if model.model_node is not None and not model.model_node.flag(
        "discourageImplicitMeasures") else []
    # ---- members become wrappers; the verify spec compares their old expressions with them
    cg_col = f"{lexers.dax_table_name(name)}[{column.replace(']', ']]')}]"
    by = _date_grain(model)
    edits: dict[str, list] = {}
    rewrites, checks = [], []
    for tp in fam["templates"]:
        for base, member in tp["members"].items():
            mm = model.measure(member)
            doc = model.docs[mm.node.file]
            s, e = _expr_span(doc, mm.node)
            wrapper = f"CALCULATE({rename._bracket(base)}, {cg_col} = {lexers.dax_string(tp['item'])})"
            edits.setdefault(mm.node.file, []).append((s, e, " " + wrapper))
            rewrites.append({"measure": f"[{mm.name}]", "table": mm.table, "base": f"[{base}]", "item": tp["item"]})
            chk = {"id": f"consolidate:{mm.name}", "kind": "equal",
                   "left": _dedent(mm.expression), "right": rename._bracket(mm.name)}
            if by:
                chk["by"] = by
            chk["tol"] = 1e-9
            checks.append(chk)
    warnings = list(fam.get("notes", []))
    implicit = _implicit_visuals(ws, project, model)
    if implicit:
        warnings.append({"code": "IMPLICIT_MEASURES", "visuals": implicit[:20], "count": len(implicit),
                         "message": "these visuals aggregate columns (implicit measures); with a calculation group the "
                                    "model discourages implicit measures: bind explicit measures instead and check them in Desktop"})
    if others:
        warnings.append({"code": "OTHER_CALC_GROUPS", "groups": others, "precedence": prec,
                         "message": "when a visual filters several calculation groups their items apply in precedence "
                                    "order (higher first): check visuals that combine them"})
    spec_path = os.path.join(ws.mirror, f"consolidate-{fam['id']}.check.json")
    spec_rel = f"{os.path.basename(ws.mirror)}/{os.path.basename(spec_path)}"
    files = [_rel(project, f"tables/{scaffold._file_name(name)}.tmdl"), _rel(project, "model.tmdl")]
    files += [_rel(project, rel) for rel in edits]
    if note:
        files.append(_rel(project, "database.tmdl"))
    planned = scaffold._plan_table(mdef, model, name, text, model_flags=flags)
    upgraded = md.compat_upgrade_text(mdef, "calculationGroup") if note else None
    if upgraded is not None:
        planned.append((upgraded[0], upgraded[1]))
    for rel, eds in edits.items():
        path = os.path.join(mdef, *rel.split("/"))
        planned.append((path, apply_edits(model.docs[rel].text, sorted(eds))))
    spec_text = json.dumps({"checks": checks}, indent=1, ensure_ascii=False) + "\n"
    planned.append((spec_path, spec_text))
    if not dry_run:
        commit_bytes(_write_ops(planned))
    out = {"verb": "consolidate", "action": "calc-group", "result": "DRY_RUN" if dry_run else "CREATED",
           "family": fam["id"], "table": name, "column": column, "precedence": prec,
           "items": [{"item": tp["item"], "ordinal": i, "members": len(tp["members"])}
                     for i, tp in enumerate(fam["templates"])],
           "members": rewrites, "files": sorted(set(files)),
           "verify": {"spec": spec_path, "written": not dry_run, "checks": len(checks),
                      "commands": ["pbz synth build", "pbz synth open", f"pbz synth check --spec {spec_rel}",
                                   f"after pbz push, on the real model: pbz check {spec_rel}"]},
           "next": "pbz diff, pbz validate, pbz synth build / open / check --spec (fake data), pbz push, then pbz check"}
    if flags:
        out["model_setting"] = "discourageImplicitMeasures turned on (calculation groups need explicit measures)"
    if note:
        out["note"] = note
    if warnings:
        out["warnings"] = warnings
    return out
