"""Model authoring beyond properties: hierarchies, dynamic format strings, DAX user-defined functions and
object-level security, written into the mirror's TMDL (then `pbz diff`, `pbz push`; TOM validates).

- `pbz hierarchy add|levels|remove <table> <name> [--level Col ...] [--names ...]`: levels keep their
  lineage tags when a level name survives a re-level.
- `pbz format-expr <[Measure]> "<DAX>" | --remove`: a measure's formatStringDefinition.
- `pbz function add|update|rename|remove|list`: functions in definition/functions.tmdl; rename fixes
  every call site; remove refuses while something calls the function.
- `pbz role ols <role> <Table | 'T'[C]> none|read|default`: table and column metadataPermission.
Names and DAX only; literals typed here are written as typed.
"""
from __future__ import annotations

import os
import re
import uuid

from . import deps, lexers, tmdl
from .common import PbzError, commit_bytes, read_text, write_text
from .governance import _eol, _model, _q, _role, _write
from .tokens import TOKEN_RE, apply_edits

PARAM_TYPES = {"anyval", "scalar", "variant", "int64", "decimal", "double", "string", "datetime", "boolean",
               "numeric", "table", "anyref", "expr", "val", "calendarref", "columnref", "measureref", "tableref"}


def _tag() -> str:
    return str(uuid.uuid4())


COMPAT = {"formatStringDefinition": 1601, "function": 1702, "ols": 1400, "calculationGroup": 1500}


def compat_level(mdef: str) -> tuple[int | None, str]:
    path = os.path.join(mdef, "database.tmdl")
    if not os.path.exists(path):
        return None, path
    m = re.search(r"(?m)^\s*compatibilityLevel:\s*(\d+)", read_text(path)[0])
    return (int(m.group(1)) if m else None), path


def compat_upgrade_text(mdef: str, feature: str) -> tuple[str, str, bytes] | None:
    """(path, text, bom) that raises compatibilityLevel, or None if no write is needed. Does not write."""
    need = COMPAT[feature]
    cur, path = compat_level(mdef)
    if cur is None or cur >= need:
        return None
    text, bom = read_text(path)
    new = re.sub(r"(?m)^(\s*compatibilityLevel:\s*)\d+", lambda mm: mm.group(1) + str(need), text, count=1)
    return path, new, bom


def require_compat(mdef: str, feature: str, upgrade: bool) -> str | None:
    """Raise COMPAT_LEVEL unless the model's compatibility level supports the feature; with upgrade,
    raise the level (one way: older Desktop builds cannot open it) and return a note."""
    need = COMPAT[feature]
    cur, _path = compat_level(mdef)
    if cur is None or cur >= need:
        return None
    if not upgrade:
        raise PbzError("COMPAT_LEVEL", f"{feature} needs compatibility level {need}; the model is at {cur}. "
                                       "Pass --upgrade-compat to raise it (one way: older Desktop builds cannot open it)")
    path, text, bom = compat_upgrade_text(mdef, feature)
    write_text(path, text, bom)
    return f"compatibility level raised from {cur} to {need}"


def _rel(project, rel: str) -> str:
    return f"{project.name}.SemanticModel/definition/{rel}"


# ---------------------------------------------------------------- hierarchies

def _hier_node(t, name: str):
    return next((n for n in (t.node.children if t.node else []) if n.kind == "hierarchy" and n.name.lower() == name.lower()), None)


def _level_block(col: str, level: str, tag: str, eol: str) -> str:
    return (f"\t\tlevel {_q(level)}{eol}\t\t\tlineageTag: {tag}{eol}\t\t\tcolumn: {_q(col)}{eol}")


def _levels(model, t, columns: list[str], names: list[str] | None) -> list[tuple[str, str]]:
    if not columns:
        raise PbzError("BAD_REQUEST", "a hierarchy needs at least one --level column")
    if names and len(names) != len(columns):
        raise PbzError("BAD_REQUEST", "--names gives one name per level")
    out, seen = [], set()
    for i, c in enumerate(columns):
        col = model.column(t.name, c.strip("'[]"))
        if col is None:
            raise PbzError("NOT_FOUND", f"column {c} in table {t.name}")
        lvl = (names[i] if names else col.name).strip()
        if col.name.lower() in seen or not lvl or TOKEN_RE.search(lvl):
            raise PbzError("BAD_REQUEST", "levels use distinct columns and plain names")
        seen.add(col.name.lower())
        out.append((col.name, lvl))
    if len({l.lower() for _c, l in out}) != len(out):
        raise PbzError("BAD_REQUEST", "level names must be distinct")
    return out


def hierarchy_add(ws, project, table: str, name: str, columns: list[str], names: list[str] | None = None) -> dict:
    mdef, model = _model(project)
    t = model.table(table)
    if t is None or t.node is None:
        raise PbzError("NOT_FOUND", f"table {table}")
    if not name.strip() or TOKEN_RE.search(name):
        raise PbzError("BAD_REQUEST", "a hierarchy needs a plain name")
    if _hier_node(t, name) is not None or name.lower() in {c.lower() for c in t.columns} | {m.lower() for m in t.measures}:
        raise PbzError("NAME_TAKEN", f"{name} in table {t.name}")
    levels = _levels(model, t, columns, names)
    doc = model.docs[t.file]
    eol = _eol(doc.text)
    block = f"\thierarchy {_q(name)}{eol}\t\tlineageTag: {_tag()}{eol}{eol}" + eol.join(
        _level_block(c, l, _tag(), eol) for c, l in levels) + eol
    if t.partition_nodes:
        at = tmdl.block_span(doc, t.partition_nodes[0])[0]
        text = doc.text[:at] + block + doc.text[at:]
    else:
        end = tmdl.block_span(doc, t.node)[1]
        head = doc.text[:end].rstrip("\r\n")
        text = head + eol + eol + block + doc.text[end:].lstrip("\r\n")
    _write(mdef, t.file, text)
    return {"verb": "hierarchy", "action": "add", "table": t.name, "hierarchy": name,
            "levels": [f"{l} ({c})" for c, l in levels], "files": [_rel(project, t.file)], "next": "pbz diff, pbz push"}


def hierarchy_levels(ws, project, table: str, name: str, columns: list[str], names: list[str] | None = None) -> dict:
    """Replace the levels; a level whose name survives keeps its lineage tag."""
    mdef, model = _model(project)
    t = model.table(table)
    if t is None:
        raise PbzError("NOT_FOUND", f"table {table}")
    h = _hier_node(t, name)
    if h is None:
        raise PbzError("NOT_FOUND", f"hierarchy {name} in table {t.name}")
    levels = _levels(model, t, columns, names)
    doc = model.docs[t.file]
    eol = _eol(doc.text)
    old_tags = {lv.name.lower(): lv.props.get("lineageTag", "").strip() for lv in h.children if lv.kind == "level"}
    spans = [tmdl.block_span(doc, lv) for lv in h.children if lv.kind == "level"]
    if not spans:
        raise PbzError("UNSUPPORTED", "hierarchy without levels: edit it in Desktop")
    start, end = spans[0][0], spans[-1][1]
    new = eol.join(_level_block(c, l, old_tags.get(l.lower()) or _tag(), eol) for c, l in levels)
    text = apply_edits(doc.text, [(start, end, new + (eol if doc.text[end:end + 1] not in ("", "\r", "\n") else ""))])
    _write(mdef, t.file, text)
    kept = sum(1 for _c, l in levels if l.lower() in old_tags)
    return {"verb": "hierarchy", "action": "levels", "table": t.name, "hierarchy": h.name,
            "levels": [f"{l} ({c})" for c, l in levels], "kept_identity": kept, "files": [_rel(project, t.file)],
            "next": "pbz diff, pbz push (visuals bound to removed levels lose them: pbz validate)"}


def hierarchy_remove(ws, project, table: str, name: str, force: bool = False) -> dict:
    from . import usage
    mdef, model = _model(project)
    t = model.table(table)
    if t is None:
        raise PbzError("NOT_FOUND", f"table {table}")
    h = _hier_node(t, name)
    if h is None:
        raise PbzError("NOT_FOUND", f"hierarchy {name} in table {t.name}")
    if not force:
        g = usage.build(ws, project)
        st = g.state.get(usage.key("hierarchy", t.name, h.name))
        if st in ("used", "used_external", "unknown"):
            raise PbzError("IN_USE", f"{t.name}.{h.name} is {st}: pass --force to remove it anyway")
    doc = model.docs[t.file]
    s, e = tmdl.block_span(doc, h)
    text = apply_edits(doc.text, [(s, e, "")])
    _write(mdef, t.file, re.sub(r"(\r?\n){3,}", lambda m: m.group(1) * 2, text))
    return {"verb": "hierarchy", "action": "remove", "table": t.name, "hierarchy": h.name,
            "files": [_rel(project, t.file)], "next": "pbz diff, pbz push"}


# ---------------------------------------------------------------- dynamic format strings

def format_expression(ws, project, measure: str, dax: str | None, upgrade: bool = False) -> dict:
    mdef, model = _model(project)
    m = model.measure(measure.strip().strip("[]"))
    if m is None or m.node is None:
        raise PbzError("NOT_FOUND", f"measure {measure}")
    doc = model.docs[m.node.file]
    eol = _eol(doc.text)
    child = next((c for c in m.node.children if c.kind == "formatStringDefinition"), None)
    ind = "\t" * (m.node.indent + 1)
    edits = []
    note = None
    db_op = None
    dropped = []
    if dax is None:
        if child is None:
            return {"verb": "format-expr", "measure": m.name, "result": "NOTHING_TO_CHANGE"}
        edits.append(tmdl.block_span(doc, child) + ("",))
    else:
        body = dax.strip()
        if not body:
            raise PbzError("BAD_REQUEST", "give a DAX expression that returns a format string")
        d = deps.dax(body, m.table, deps.Resolver(model))
        if d.unresolved:
            raise PbzError("UNKNOWN_REF", ", ".join(sorted({f"[{u.name}]" if not u.table else f"{u.table}[{u.name}]"
                                                           for u in d.unresolved})))
        cur, _db = compat_level(mdef)
        need = COMPAT["formatStringDefinition"]
        if cur is not None and cur < need:
            if not upgrade:
                raise PbzError("COMPAT_LEVEL", f"formatStringDefinition needs compatibility level {need}; the model is at {cur}. "
                                               "Pass --upgrade-compat to raise it (one way: older Desktop builds cannot open it)")
            path, text, bom = compat_upgrade_text(mdef, "formatStringDefinition")
            db_op = ("write", path, text, bom)
            note = f"compatibility level raised from {cur} to {need}"
        if "\n" in body:
            text = f"{ind}formatStringDefinition ={eol}" + eol.join(ind + "\t" + ln.rstrip() for ln in body.splitlines() if ln.strip()) + eol
        else:
            text = f"{ind}formatStringDefinition = {body}{eol}"
        if child is not None:
            edits.append(tmdl.block_span(doc, child) + (text,))
        else:
            end = tmdl.block_span(doc, m.node)[1]
            prefix = doc.text[:end]
            trail = len(prefix) - len(prefix.rstrip("\r\n"))
            at = end - trail + (len(eol) if trail else 0)
            edits.append((at, at, text if trail else eol + text))
        from .props import _remove_prop
        dropped = _remove_prop(doc, m.node, "formatString")
        edits += dropped
    path = os.path.join(mdef, *m.node.file.split("/"))
    _t, bom = read_text(path)
    ops = ([db_op] if db_op else []) + [("write", path, apply_edits(doc.text, edits), bom)]
    commit_bytes(ops)
    out = {"verb": "format-expr", "measure": m.name, "result": "REMOVED" if dax is None else "SET",
           "files": [_rel(project, m.node.file)] + ([_rel(project, "database.tmdl")] if note else []),
           "next": "pbz diff, pbz push; check the format on fake data with pbz synth (it can return any text)"}
    if dax is not None and dropped:
        out["replaced"] = "formatString (a measure cannot have both; Desktop refuses to open the model)"
    if dax is None and "formatString" not in m.node.props:
        out["next"] = "the measure has no format now: pbz set \"[" + m.name + "]\" formatString=\"...\", then pbz push"
    if note:
        out["note"] = note
    return out


# ---------------------------------------------------------------- DAX user-defined functions

FN_FILE = "functions.tmdl"


def _fn_text(name: str, params: str, body: str, eol: str, tag: str) -> str:
    params = params.strip()
    if not (params.startswith("(") and params.endswith(")")):
        params = f"({params})"
    for p in [x.strip() for x in params[1:-1].split(",") if x.strip()]:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*([A-Za-z]+)(?:\s+([A-Za-z]+))?)?$", p)
        if not m or (m.group(2) and m.group(2).lower() not in PARAM_TYPES) or (m.group(3) and m.group(3).lower() not in PARAM_TYPES):
            raise PbzError("BAD_REQUEST", f"parameter `{p}`: name [: TYPE [SUBTYPE]] (types like NUMERIC, STRING, TABLE, ANYREF)")
    body = body.strip()
    if not body:
        raise PbzError("BAD_REQUEST", "a function needs a body")
    head = f"function {_q(name)} ="
    if "\n" in body:
        expr = eol + f"\t\t{params} =>" + eol + eol.join("\t\t\t" + ln.rstrip() for ln in body.splitlines() if ln.strip())
    else:
        expr = f" {params} => {body}"
    return head + expr + eol + f"\tlineageTag: {tag}" + eol


def _fn(model, name: str):
    return next((n for k, n in model.functions.items() if k.lower() == name.lower()), None)


def _check_body(model, params: str, body: str) -> None:
    d = deps.dax(f"{params if params.strip().startswith('(') else '(' + params + ')'} => {body}", "", deps.Resolver(model), function=True)
    if d.unresolved:
        raise PbzError("UNKNOWN_REF", ", ".join(sorted({f"[{u.name}]" if not u.table else f"{u.table}[{u.name}]" for u in d.unresolved})))


def function_add(ws, project, name: str, params: str, body: str, upgrade: bool = False) -> dict:
    mdef, model = _model(project)
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", name or ""):
        raise PbzError("BAD_REQUEST", "a function name has letters, digits, _ and dots")
    if _fn(model, name) is not None or model.measure(name) or model.table(name):
        raise PbzError("NAME_TAKEN", name)
    _check_body(model, params, body)
    note = None
    ops = []
    cur, _db = compat_level(mdef)
    need = COMPAT["function"]
    if cur is not None and cur < need:
        if not upgrade:
            raise PbzError("COMPAT_LEVEL", f"function needs compatibility level {need}; the model is at {cur}. "
                                           "Pass --upgrade-compat to raise it (one way: older Desktop builds cannot open it)")
        path, text, bom = compat_upgrade_text(mdef, "function")
        ops.append(("write", path, text, bom))
        note = f"compatibility level raised from {cur} to {need}"
    path = os.path.join(mdef, FN_FILE)
    text = read_text(path)[0] if os.path.exists(path) else ""
    bom = read_text(path)[1] if os.path.exists(path) else b""
    eol = _eol(text) if text else ("\r\n" if any("\r\n" in d.text for d in model.docs.values()) else "\n")
    new = (text.rstrip("\r\n") + eol + eol if text.strip() else "") + _fn_text(name, params, body, eol, _tag())
    ops.append(("write", path, new, bom))
    commit_bytes(ops)
    out = {"verb": "function", "action": "add", "function": name,
           "files": [_rel(project, FN_FILE)] + ([_rel(project, "database.tmdl")] if note else []),
           "next": "pbz diff, pbz push (TOM validates the function); call it as " + f"{name}(...)"}
    if note:
        out["note"] = note
    return out


def _fn_decl_span(doc, node) -> tuple[int, int]:
    """From the declaration line to the end of its expression (property lines after it stay)."""
    lines = tmdl._lines(doc.text)
    start = lines[node.line][0]
    regs = [r for r in doc.regions if r.node is node and r.prop == "expression"]
    end = max([r.end for r in regs] + [start + len(lines[node.line][1])])
    nl = doc.text.find("\n", end)
    return start, (len(doc.text) if nl < 0 else nl + 1)


def function_update(ws, project, name: str, params: str | None, body: str | None) -> dict:
    mdef, model = _model(project)
    node = _fn(model, name)
    if node is None:
        raise PbzError("NOT_FOUND", f"function {name}")
    doc = model.docs[node.file]
    cur = "".join(doc.text[r.start:r.end] for r in doc.regions if r.node is node and r.prop == "expression").strip()
    m = re.match(r"^\s*(\([^)]*\))\s*=>\s*(.*)$", cur, re.S)
    old_params, old_body = (m.group(1), m.group(2)) if m else ("()", cur)
    params = params if params is not None else old_params
    body = body if body is not None else "\n".join(ln.strip() for ln in old_body.splitlines())
    _check_body(model, params, body)
    eol = _eol(doc.text)
    s, e = _fn_decl_span(doc, node)
    full = _fn_text(node.name, params, body, eol, "x")
    decl = full[:full.rfind(f"\tlineageTag: x")]
    _write(mdef, node.file, apply_edits(doc.text, [(s, e, decl)]))
    return {"verb": "function", "action": "update", "function": node.name, "files": [_rel(project, node.file)],
            "next": "pbz diff, pbz push; callers keep working only if the signature still fits (TOM checks)"}


def _call_edits(model, old: str, new: str) -> dict[str, list]:
    """Edits renaming calls `old(` to `new(` in every DAX region of the model."""
    edits: dict[str, list] = {}
    for rel, doc in model.docs.items():
        for r in doc.regions:
            if r.kind != "dax":
                continue
            sig = lexers.significant(lexers.lex_dax(doc.text[r.start:r.end], base=r.start))
            for i, tok in enumerate(sig):
                if tok.kind == "ident" and tok.text.lower() == old.lower() and i + 1 < len(sig) and sig[i + 1].text == "(":
                    edits.setdefault(rel, []).append((tok.start, tok.end, new))
    return edits


def function_rename(ws, project, name: str, new: str) -> dict:
    mdef, model = _model(project)
    node = _fn(model, name)
    if node is None:
        raise PbzError("NOT_FOUND", f"function {name}")
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", new or "") or _fn(model, new) is not None or model.measure(new):
        raise PbzError("NAME_TAKEN" if new and _fn(model, new) is not None else "BAD_REQUEST", new)
    edits = _call_edits(model, node.name, new)
    doc = model.docs[node.file]
    line_off, line = tmdl._lines(doc.text)[node.line]
    m = re.match(r"^(\t*function\s+)('(?:[^']|'')*'|[^\s=]+)", line)
    edits.setdefault(node.file, []).append((line_off + m.start(2), line_off + m.end(2), _q(new)))
    ops = []
    for rel, eds in edits.items():
        path = os.path.join(mdef, *rel.split("/"))
        _t, bom = read_text(path)
        ops.append(("write", path, apply_edits(model.docs[rel].text, sorted(set(eds))), bom))
    commit_bytes(ops)
    calls = sum(len(v) for v in edits.values()) - 1
    return {"verb": "function", "action": "rename", "from": node.name, "to": new, "calls_fixed": calls,
            "files": sorted(_rel(project, r) for r in edits), "next": "pbz diff, pbz push"}


def function_remove(ws, project, name: str, force: bool = False) -> dict:
    mdef, model = _model(project)
    node = _fn(model, name)
    if node is None:
        raise PbzError("NOT_FOUND", f"function {name}")
    callers = sum(len(v) for v in _call_edits(model, node.name, node.name).values())
    if callers and not force:
        raise PbzError("IN_USE", f"{node.name} is called {callers} time(s): rewrite the callers first, or --force")
    doc = model.docs[node.file]
    s, e = tmdl.block_span(doc, node)
    text = apply_edits(doc.text, [(s, e, "")])
    _write(mdef, node.file, re.sub(r"(\r?\n){3,}", lambda mm: mm.group(1) * 2, text))
    return {"verb": "function", "action": "remove", "function": node.name, "files": [_rel(project, node.file)]}


def function_list(ws, project) -> dict:
    _mdef, model = _model(project)
    rows = []
    for name, node in sorted(model.functions.items(), key=lambda x: x[0].lower()):
        doc = model.docs[node.file]
        cur = "".join(doc.text[r.start:r.end] for r in doc.regions if r.node is node and r.prop == "expression").strip()
        m = re.match(r"^\s*(\([^)]*\))", cur)
        rows.append({"function": name, "params": m.group(1) if m else "?",
                     "calls": sum(len(v) for v in _call_edits(model, name, name).values()), "file": node.file})
    return {"verb": "function", "action": "list", "functions": rows}


# ---------------------------------------------------------------- object-level security

def role_ols(ws, project, role: str, target: str, permission: str) -> dict:
    if permission not in ("none", "read", "default"):
        raise PbzError("BAD_REQUEST", "permission: none (hidden), read, or default (remove the rule)")
    mdef, model = _model(project)
    require_compat(mdef, "ols", False)
    rel, doc, rnode = _role(mdef, model, role)
    m = re.match(r"^'((?:[^']|'')+)'\s*\[(.+)\]$", target.strip()) or re.match(r"^([^\[\]']+?)\s*\[(.+)\]$", target.strip())
    if m:
        col = model.column(m.group(1).replace("''", "'"), m.group(2))
        if col is None:
            raise PbzError("NOT_FOUND", target)
        tname, cname = col.table, col.name
    else:
        t = model.table(target.strip().strip("'"))
        if t is None:
            raise PbzError("NOT_FOUND", target)
        tname, cname = t.name, None
    eol = _eol(doc.text)
    tp = next((c for c in rnode.children if c.kind == "tablePermission" and c.name.lower() == tname.lower()), None)
    edits = []
    if permission == "default":
        if tp is None:
            return {"verb": "role", "action": "ols", "result": "NOTHING_TO_CHANGE"}
        if cname:
            cp = next((c for c in tp.children if c.kind == "columnPermission" and c.name.lower() == cname.lower()), None)
            if cp is None:
                return {"verb": "role", "action": "ols", "result": "NOTHING_TO_CHANGE"}
            edits.append(tmdl.block_span(doc, cp) + ("",))
        else:
            from .props import _remove_prop
            edits += _remove_prop(doc, tp, "metadataPermission")
    elif tp is None:
        block = f"\ttablePermission {_q(tname)}{eol}"
        block += (f"\t\tcolumnPermission {_q(cname)}{eol}\t\t\tmetadataPermission: {permission}{eol}" if cname
                  else f"\t\tmetadataPermission: {permission}{eol}")
        text = doc.text.rstrip("\r\n") + eol + eol + block
        _write(mdef, rel, text)
        return {"verb": "role", "action": "ols", "role": rnode.name, "target": target, "permission": permission,
                "files": [_rel(project, rel)], "next": "pbz diff, pbz push; the user tests with View as role"}
    elif cname:
        cp = next((c for c in tp.children if c.kind == "columnPermission" and c.name.lower() == cname.lower()), None)
        if cp is not None:
            edits += tmdl.prop_edit(doc, cp, "metadataPermission", permission)
        else:
            end = tmdl.block_span(doc, tp)[1]
            prefix = doc.text[:end]
            trail = len(prefix) - len(prefix.rstrip("\r\n"))
            at = end - trail + (len(eol) if trail else 0)
            line = f"\t\tcolumnPermission {_q(cname)}{eol}\t\t\tmetadataPermission: {permission}{eol}"
            edits.append((at, at, line if trail else eol + line))
    else:
        edits += tmdl.prop_edit(doc, tp, "metadataPermission", permission)
    if not edits:
        return {"verb": "role", "action": "ols", "result": "NOTHING_TO_CHANGE"}
    _write(mdef, rel, apply_edits(doc.text, edits))
    return {"verb": "role", "action": "ols", "role": rnode.name, "target": target, "permission": permission,
            "files": [_rel(project, rel)], "next": "pbz diff, pbz push; the user tests with View as role"}
