"""`pbz m-lineage`: Power Query step and column lineage from the sanitized mirror M.

A small let-step analyser (no external parser): splits `let ... in` into steps, reads the top-level
function of each step and applies a column transfer table (select / remove / rename / add / expand /
group / join / split / combine / unpivot; read-only transforms keep the set). Column names are emitted
only from column-name argument slots; every other argument (filters, constants, URLs, paths) stays
unread. Steps it cannot follow make the column set "dynamic" from that point.
"""
from __future__ import annotations

import os

from . import lexers, tmdl
from .common import PbzError

KEEP = {  # transforms that keep the column set
    "Table.SelectRows", "Table.Distinct", "Table.Sort", "Table.FirstN", "Table.LastN", "Table.Skip",
    "Table.RemoveFirstN", "Table.RemoveLastN", "Table.RemoveRows", "Table.Range", "Table.FillDown", "Table.FillUp",
    "Table.ReplaceValue", "Table.ReplaceErrorValues", "Table.RemoveRowsWithErrors", "Table.SelectRowsWithErrors",
    "Table.TransformColumnTypes", "Table.TransformColumns", "Table.Buffer", "Table.AlternateRows",
    "Table.RemoveMatchingRows", "Table.ReorderColumns", "Table.StopFolding", "Table.ConformToPageReader",
    "Table.AddIndexColumn", "Table.Combine", "Table.Repeat", "Table.ReverseRows", "Table.InsertRows",
}
SOURCE_PREFIXES = ("Sql.", "OData.", "Excel.", "Csv.", "Web.", "SharePoint.", "Json.", "Xml.", "Odbc.", "OleDb.",
                   "Oracle.", "PostgreSQL.", "MySQL.", "AzureStorage.", "AzureDataLake", "Lakehouse.", "Fabric.",
                   "PowerPlatform.", "Dataverse.", "CommonDataService.", "Salesforce.", "AnalysisServices.",
                   "PowerBI.", "Folder.", "File.", "Snowflake.", "GoogleBigQuery.", "Databricks.", "DataLake.")


class _Cols:
    """Column set of a step: known names, or dynamic (unknown base) plus known additions/removals."""

    def __init__(self, names=None, dynamic=False):
        self.names = list(names or [])
        self.dynamic = dynamic
        self.removed: set[str] = set()
        self.origin: dict[str, str] = {n: "source" for n in self.names}

    def copy(self):
        c = _Cols(self.names, self.dynamic)
        c.removed, c.origin = set(self.removed), dict(self.origin)
        return c


def _split_args(toks, i):
    """toks[i] is '(' -> (list of arg token lists, index after ')')."""
    depth, args, cur = 0, [], []
    j = i
    while j < len(toks):
        t = toks[j]
        if t.kind == "op" and t.text in "([{":
            depth += 1
            if depth > 1:
                cur.append(t)
        elif t.kind == "op" and t.text in ")]}":
            depth -= 1
            if depth == 0:
                if cur:
                    args.append(cur)
                return args, j + 1
            cur.append(t)
        elif t.kind == "op" and t.text == "," and depth == 1:
            args.append(cur)
            cur = []
        else:
            cur.append(t)
        j += 1
    return args, j


def _strs(arg) -> list[str]:
    """Column names from a "x" / {"a","b"} argument; tokens become "(token)"."""
    out = []
    for t in arg or []:
        if t.kind == "str":
            out.append(t.value)
        elif t.kind == "pbztok":
            out.append("(token)")
    return out


def _pairs(arg) -> list[tuple[str, str]]:
    """{{"old", "new"}, ...} -> pairs (strings only)."""
    names = [t for t in arg or [] if t.kind in ("str", "pbztok")]
    vals = ["(token)" if t.kind == "pbztok" else t.value for t in names]
    return list(zip(vals[0::2], vals[1::2]))


def _first_strings_of_lists(arg) -> list[str]:
    """{{"col", type ...}, ...} -> first string of each inner list."""
    out, depth, want = [], 0, False
    for t in arg or []:
        if t.kind == "op" and t.text == "{":
            depth += 1
            want = depth == 2
        elif t.kind == "op" and t.text == "}":
            depth -= 1
        elif want and t.kind in ("str", "pbztok"):
            out.append("(token)" if t.kind == "pbztok" else t.value)
            want = False
    return out


def split_steps(src: str):
    """[(step name, tokens)] and the result step name, or (None, None) when not `let ... in name`."""
    toks = lexers.significant(lexers.lex_m(src))
    if not toks or toks[0].kind != "ident" or toks[0].text != "let":
        return None, None
    steps, depth, i = [], 0, 1
    cur_name, cur = None, []
    while i < len(toks):
        t = toks[i]
        if depth == 0 and t.kind == "ident" and t.text == "in":
            if cur_name is not None:
                steps.append((cur_name, cur))
            rest = toks[i + 1:]
            result = (rest[0].value or rest[0].text) if len(rest) == 1 and rest[0].kind in ("ident", "qident") else None
            return steps, result
        if depth == 0 and cur_name is None and t.kind in ("ident", "qident") and i + 1 < len(toks) and \
                toks[i + 1].kind == "op" and toks[i + 1].text == "=":
            cur_name, cur = (t.value or t.text), []
            i += 2
            continue
        if t.kind == "op" and t.text in "([{":
            depth += 1
        elif t.kind == "op" and t.text in ")]}":
            depth -= 1
        elif t.kind == "ident" and t.text == "let":
            depth += 1
        elif t.kind == "ident" and t.text == "in" and depth > 0:
            depth -= 1
        if depth == 0 and t.kind == "op" and t.text == ",":
            steps.append((cur_name, cur))
            cur_name, cur = None, []
        else:
            cur.append(t)
        i += 1
    return None, None


def _step(name, toks, env, queries) -> tuple[_Cols, dict]:
    info = {"step": name}
    if not toks:
        return _Cols(dynamic=True), dict(info, effect="empty")
    head = toks[0]
    if head.kind in ("ident", "qident") and len(toks) == 1:
        ref = head.value or head.text
        if ref in env:
            return env[ref].copy(), dict(info, effect=f"same as {ref}")
        if ref in queries:
            return queries[ref].copy(), dict(info, effect=f"query {ref}")
    if head.kind == "op" and head.text == "#" and len(toks) > 2 and toks[1].text == "table" and toks[2].text == "(":
        cols = []
        args, _ = _split_args(toks, 2)
        first = args[0] if args else []
        if first[:1] and first[0].kind == "ident" and first[0].text == "type":
            cols = [t.value or t.text for k, t in enumerate(first) if t.kind in ("ident", "qident")
                    and k + 1 < len(first) and first[k + 1].kind == "op" and first[k + 1].text == "="]
        elif first[:1] and first[0].kind == "op" and first[0].text == "{":
            cols = _strs(first)
        return _Cols(cols, dynamic=not cols), dict(info, function="#table", effect=f"inline table, {len(cols)} column(s)")
    if not (head.kind == "ident" and len(toks) > 1 and toks[1].kind == "op" and toks[1].text == "("):
        return _Cols(dynamic=True), dict(info, effect="dynamic (not a function call)")
    fn = head.text
    info["function"] = fn
    args, end = _split_args(toks, 1)
    prev_name = None
    if args and len(args[0]) == 1 and args[0][0].kind in ("ident", "qident"):
        prev_name = args[0][0].value or args[0][0].text
    prev = env.get(prev_name) or queries.get(prev_name)
    if fn.startswith(SOURCE_PREFIXES) or prev is None:
        effect = "source" if fn.startswith(SOURCE_PREFIXES) else "dynamic (input is not a previous step)"
        return _Cols(dynamic=True), dict(info, effect=effect)
    cur = prev.copy()
    a = args + [[]] * 6
    if end < len(toks):  # navigation after the call, e.g. ...{0}[Data]
        return _Cols(dynamic=True), dict(info, effect="dynamic (navigation)")
    if fn == "Table.SelectColumns":
        names = _strs(a[1])
        cur = _Cols(names)
        cur.origin = {n: prev.origin.get(n, "source") for n in names}
        info["effect"] = f"select {len(names)}"
    elif fn == "Table.RemoveColumns":
        names = _strs(a[1])
        cur.names = [n for n in cur.names if n not in names]
        cur.removed |= set(names)
        info["effect"], info["columns"] = f"remove {len(names)}", names
    elif fn == "Table.RenameColumns":
        pairs = _pairs(a[1])
        for old, new in pairs:
            cur.names = [new if n == old else n for n in cur.names]
            if cur.dynamic and old not in cur.origin:
                cur.names.append(new)
            cur.origin[new] = f"renamed from {old} in {name}"
            cur.removed.add(old)
        info["effect"], info["columns"] = f"rename {len(pairs)}", [f"{o} -> {n}" for o, n in pairs]
    elif fn in ("Table.AddColumn", "Table.DuplicateColumn"):
        names = _strs(a[1])[:1] if fn == "Table.AddColumn" else _strs(a[2])[:1]
        cur.names += names
        for n in names:
            cur.origin[n] = f"added in {name}"
        info["effect"], info["columns"] = f"add {len(names)}", names
    elif fn in ("Table.ExpandTableColumn", "Table.ExpandRecordColumn", "Table.ExpandListColumn"):
        col = (_strs(a[1]) or [""])[0]
        new = _strs(a[3]) or _strs(a[2]) if fn != "Table.ExpandListColumn" else [col]
        cur.names = [n for n in cur.names if n != col] + [n for n in new if n != col]
        for n in new:
            cur.origin[n] = f"expanded from {col} in {name}"
        info["effect"], info["columns"] = f"expand {col} into {len(new)}", new
    elif fn == "Table.Group":
        keys = _strs(a[1])
        aggs = _first_strings_of_lists(a[2]) or [t.value for t in a[2][1:2] if t.kind == "str"]
        cur = _Cols(keys + aggs)
        cur.origin = {n: prev.origin.get(n, "source") for n in keys}
        cur.origin.update({n: f"aggregated in {name}" for n in aggs})
        info["effect"] = f"group by {len(keys)}, {len(aggs)} aggregate(s)"
    elif fn in ("Table.NestedJoin", "Table.FuzzyNestedJoin"):
        new = _strs(a[4])[:1]
        cur.names += new
        for n in new:
            cur.origin[n] = f"merged in {name}"
        info["effect"], info["columns"] = f"merge into {new[0] if new else '?'}", new
        info["join_keys"] = _strs(a[1])
    elif fn in ("Table.UnpivotOtherColumns", "Table.Unpivot"):
        keep = _strs(a[1])
        attr = _strs(a[2])[:1] + _strs(a[3])[:1]
        cur = _Cols((keep if fn == "Table.UnpivotOtherColumns" else [n for n in prev.names if n not in keep]) + attr,
                    dynamic=fn == "Table.Unpivot" and prev.dynamic)
        info["effect"] = f"unpivot into {len(attr)}"
    elif fn == "Table.SplitColumn":
        col = (_strs(a[1]) or [""])[0]
        new = _strs(a[3])
        cur.names = [n for n in cur.names if n != col] + new
        for n in new:
            cur.origin[n] = f"split from {col} in {name}"
        info["effect"], info["columns"] = f"split {col} into {len(new)}", new
    elif fn == "Table.CombineColumns":
        old = _strs(a[1])
        new = _strs(a[3])[:1]
        cur.names = [n for n in cur.names if n not in old] + new
        cur.removed |= set(old)
        for n in new:
            cur.origin[n] = f"combined in {name}"
        info["effect"], info["columns"] = f"combine {len(old)} into 1", new
    elif fn in ("Table.TransformColumnTypes", "Table.TransformColumns"):
        info["effect"] = f"transform {len(_first_strings_of_lists(a[1]) or _strs(a[1]))}"
    elif fn in KEEP:
        info["effect"] = "rows / values only"
    else:
        cur = _Cols(dynamic=True)
        info["effect"] = "dynamic (unknown function)"
    return cur, info


def analyse(src: str, queries: dict) -> dict:
    steps, result = split_steps(src)
    if steps is None:
        return {"shape": "not a let ... in query", "dynamic": True, "steps": []}
    env: dict[str, _Cols] = {}
    infos = []
    for name, toks in steps:
        cols, info = _step(name, toks, env, queries)
        env[name] = cols
        infos.append(info)
    final = env.get(result) if result else None
    if final is None:
        return {"shape": "result is not a step", "dynamic": True, "steps": infos}
    return {"steps": infos, "result": result, "columns": final.names, "dynamic": final.dynamic,
            "removed": sorted(final.removed), "origin": final.origin, "_cols": final}


def run(ws, project, table: str | None = None) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    queries: dict[str, _Cols] = {}
    q_out = []
    for name, node in model.expressions.items():
        text = "".join(model.text_of(r) for r in tmdl.node_regions(model.docs[node.file], node, deep=False) if r.kind == "m")
        res = analyse(text, queries)
        if "_cols" in res:
            queries[name] = res.pop("_cols")
        res.pop("origin", None)
        q_out.append({"query": name, **res})
    tables = []
    for t in model.tables.values():
        if table and t.name.lower() != table.lower():
            continue
        for p in t.partition_nodes:
            if (p.decl_value.strip().lower() or "m") != "m":
                continue
            reg = next((r for r in model.docs[p.file].regions if r.node is p and r.kind == "m"), None)
            if reg is None:
                continue
            res = analyse(model.text_of(reg), queries)
            cols = res.pop("_cols", None)
            origin = res.pop("origin", {})
            mapping, problems = [], []
            for c in t.columns.values():
                if c.calculated or not c.source_column:
                    continue
                sc = c.source_column
                if cols is not None and sc in cols.names:
                    mapping.append({"column": c.name, "source": sc, "origin": origin.get(sc, "source")})
                elif cols is not None and sc in cols.removed:
                    problems.append({"code": "COLUMN_REMOVED_IN_QUERY", "column": c.name, "source": sc})
                elif cols is not None and not cols.dynamic:
                    problems.append({"code": "COLUMN_NOT_IN_QUERY", "column": c.name, "source": sc})
                else:
                    mapping.append({"column": c.name, "source": sc, "origin": "source (schema not visible)"})
            if cols is not None and not cols.dynamic:
                mapped = {c.source_column for c in t.columns.values() if c.source_column}
                extra = [n for n in cols.names if n not in mapped and n != "(token)"]
                if extra:
                    problems.append({"code": "QUERY_COLUMN_NOT_IN_MODEL", "columns": extra[:30],
                                     "note": "Desktop adds these to the model on the next refresh"})
            tables.append({"table": t.name, "partition": p.name, "steps": res.get("steps", []),
                           "dynamic": res.get("dynamic", True), "columns": mapping, "problems": problems,
                           **({"shape": res["shape"]} if "shape" in res else {})})
    out = {"verb": "m-lineage", "project": project.name, "tables": tables}
    if not table:
        out["queries"] = q_out
    if table and not tables:
        raise PbzError("NOT_FOUND", f"M table {table}")
    return out
