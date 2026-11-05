"""`pbz synth`: a synthetic copy of the model with fake data that agents may query freely.

Built from the sanitized mirror only (never the real files) into %USERPROFILE%\\pbz-synth\\<ws-id>\\, a folder
marked synthetic for the guard. Every data partition (M, entity, DirectQuery, and calculated tables that
embed literal rows) becomes a calculated DATATABLE of fake rows: types from TMDL, relationship keys that
mostly match (a few orphans on purpose), dates inside the model's calendar, seeded random numbers, and
strings drawn from the literals and mirror tokens the DAX and report filters compare each column to, so
filtered measures and visuals return rows. Measures, calculated columns, calculation groups, field
parameters, hierarchies, relationships and roles are kept. Every object gets a new lineageTag and the
model carries `annotation PBZ_SYNTHETIC = <hmac>`, an HMAC (private workspace key) of its table lineage
tags: the engine's `synth-query` verb returns rows only when that marker verifies and every partition is
calculated, so real data can never come back through it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import random
import re
import shutil
import uuid

from . import desktop, deps, dpapi, lexers, pbir, tmdl
from .common import PbzError, Project, Workspace, engine_call, read_text, write_text
from .tokens import TOKEN_RE, apply_edits

MARKER = "PBZ_SYNTHETIC"
DAX_TYPES = {"string": "STRING", "int64": "INTEGER", "double": "DOUBLE", "decimal": "CURRENCY",
             "datetime": "DATETIME", "boolean": "BOOLEAN"}
NUMERIC = {"int64", "double", "decimal"}
LABEL_RE = re.compile(r"^«(.+)\[(.+)\]#\d+»$")
CMP = {"=", "==", "<>"}


# ---------------------------------------------------------------- locations

def synth_base() -> str:
    return os.environ.get("PBZ_SYNTH_ROOT") or os.path.join(os.path.expanduser("~"), "pbz-synth")


def synth_project(ws, project) -> Project:
    return Project(Workspace(os.path.join(synth_base(), ws.id)), project.name + "Synth")


def _is_synthetic_root(root: str) -> bool:
    try:
        with open(os.path.join(root, ".pbz", "policy.json"), encoding="utf-8") as f:
            pol = json.load(f)
        return pol.get("synthetic") is True and os.path.exists(os.path.join(root, ".pbz", "synth.json"))
    except (OSError, ValueError):
        return False


def _require_synthetic(root: str) -> None:
    if not _is_synthetic_root(root):
        raise PbzError("UNSAFE", "not a pbz synthetic folder; refusing to touch it")


# ---------------------------------------------------------------- fake values

def _hash_int(s: str, mod: int) -> int:
    return int(hashlib.sha256(s.encode("utf-8")).hexdigest()[:12], 16) % mod


def fake_int(token: str) -> int:
    return 900000 + _hash_int(token, 99999)


def _date_range(model) -> tuple[dt.date, dt.date]:
    for t in model.tables.values():
        for _n, kind, expr in t.partitions:
            m = re.search(r"(?is)CALENDAR\s*\(\s*DATE\s*\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)\s*,"
                          r"\s*DATE\s*\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)", expr or "")
            if kind == "calculated" and m:
                try:
                    a = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    b = dt.date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
                    if a <= b:
                        return a, b
                except ValueError:
                    pass
    return dt.date(2024, 1, 1), dt.date(2026, 12, 31)


def _fake_date(token: str, lo: dt.date, hi: dt.date) -> dt.date:
    return lo + dt.timedelta(days=_hash_int(token, max(1, (hi - lo).days + 1)))


class _Pools:
    """Values each column is compared with in the mirror (strings, plus fake stand-ins for tokens)."""

    def __init__(self, model, lo, hi):
        self.model, self.lo, self.hi = model, lo, hi
        self.res = deps.Resolver(model)
        self.values: dict[tuple, list] = {}

    def col(self, table: str, name: str):
        t = self.res.table(table or "")
        c = self.res.column(t, name) if t else None
        return (t, c) if c else None

    def dtype(self, key) -> str:
        c = self.model.column(*key) if key else None
        return (c.data_type if c else "").lower()

    def add(self, key, value) -> None:
        if key is None:
            return
        lst = self.values.setdefault(key, [])
        if value not in lst:
            lst.append(value)

    def add_token(self, key, token: str) -> None:
        if key is None:
            return  # label of an embedded-table header the model does not have
        dtp = self.dtype(key)
        if dtp in NUMERIC:
            self.add(key, fake_int(token))
        elif dtp.startswith("date"):
            self.add(key, _fake_date(token, self.lo, self.hi).isoformat())
        elif dtp == "string":
            self.add(key, token)


def _collect_pools(model, project, lo, hi) -> _Pools:
    pools = _Pools(model, lo, hi)
    texts = [d.text for d in model.docs.values()]
    for path in pbir.report_files(project.mirror_report):
        texts.append(read_text(path)[0])
    for text in texts:  # every column-bound token anywhere in the mirror
        for tok in TOKEN_RE.findall(text):
            m = LABEL_RE.match(tok)
            if m:
                pools.add_token(pools.col(m.group(1), m.group(2)), tok)
    for doc in model.docs.values():  # visible literals compared with a column in DAX
        for reg in doc.regions:
            if reg.kind != "dax":
                continue
            home = reg.node
            while home is not None and home.kind not in ("table", "tablePermission"):
                home = home.parent
            sig = lexers.significant(lexers.lex_dax(doc.text[reg.start:reg.end]))
            refs = {}
            for i, t in enumerate(sig):
                if t.kind == "bracket":
                    prev = sig[i - 1] if i else None
                    if prev is not None and prev.kind in ("table", "ident"):
                        refs[i] = pools.col(prev.value if prev.kind == "table" else prev.text, t.value)
                    elif home is not None:
                        refs[i] = pools.col(home.name, t.value)
            for i, t in enumerate(sig):
                if t.kind == "op" and t.text in CMP:
                    for a, b in ((i - 1, i + 1), (i + 1, i - 1)):
                        if 0 <= a < len(sig) and 0 <= b < len(sig) and refs.get(a) and sig[b].kind == "str":
                            pools.add(refs[a], sig[b].value)
                elif t.kind == "ident" and t.text.upper() == "IN" and refs.get(i - 1) and i + 1 < len(sig) and sig[i + 1].text == "{":
                    j = i + 2
                    while j < len(sig) and sig[j].text != "}":
                        if sig[j].kind == "str":
                            pools.add(refs[i - 1], sig[j].value)
                        j += 1
    for path in pbir.report_files(project.mirror_report):  # visible report filter / slicer labels
        try:
            root = pbir.parse_json(read_text(path)[0])
        except (pbir.JsonError, OSError, UnicodeDecodeError):
            continue
        for site in pbir.walk_literals(root, model):
            if site.column:
                value, vtype = pbir.decode_literal(site.raw)
                if vtype == "string" and isinstance(value, str):
                    pools.add(pools.col(*site.column), value)
    return pools


# ---------------------------------------------------------------- planning


def _rewrite_table(t) -> bool:
    if t.private or t.calc_items or not t.partitions:
        return False
    for _n, kind, expr in t.partitions:
        if kind not in ("calculated", "calculationgroup"):
            return True
        # calculated tables that embed literal rows carry (tokenized) data: replace them as well
        if kind == "calculated" and "nameof" not in expr.lower() and TOKEN_RE.search(expr) and \
                re.search(r"(?i)DATATABLE|\{", expr):
            return True
    return False


def _fmt(value, dtype: str) -> str:
    if value is None:  # only the edge profile plants real blanks
        return "BLANK()"
    if dtype == "int64":
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(fake_int(str(value)))
    if dtype in ("double", "decimal"):
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return f"{fake_int(str(value))}.00"
    if dtype == "boolean":
        return "TRUE" if value else "FALSE"
    return lexers.dax_string(str(value))


def _generate(model, pools: _Pools, rows: int, seed: int, plan: list) -> dict:
    """{table: (columns [(name, dtype)], rows [[python values]])} with relationship-consistent keys."""
    rng = random.Random(seed)
    lo, hi = pools.lo, pools.hi
    span = max(1, (hi - lo).days)
    one_side, many_side = {}, {}
    for r in model.relationships:
        to = (r.to_table, r.to_column)
        many = r.node is not None and r.node.props.get("toCardinality", "").strip() == "many"
        if not many:
            one_side.setdefault((r.to_table.lower(), r.to_column.lower()), to)
        many_side[(r.from_table.lower(), r.from_column.lower())] = to
    dims = {k[0] for k in one_side}
    keys: dict[tuple, list] = {}
    out = {}
    order = sorted(plan, key=lambda t: t.name.lower() not in dims)  # dimensions first: their keys feed facts
    for t in order:
        cols = [c for c in t.columns.values() if not c.calculated]
        if not cols:
            continue
        n = max(5, min(50, rows // 4)) if t.name.lower() in dims else rows
        data = []
        for c in cols:
            dtp = (c.data_type or "string").lower()
            key = (t.name, c.name)
            pool = pools.values.get(key, [])
            low = (t.name.lower(), c.name.lower())
            vals: list = []
            if low in one_side or (c.node is not None and c.node.flag("isKey")):
                if dtp == "string":
                    base = [v for v in pool if isinstance(v, str)]
                    vals = (base + [f"{c.name} {i}" for i in range(1, n + 1) if f"{c.name} {i}" not in base])[:n]
                elif dtp in NUMERIC:
                    base = [v for v in pool if isinstance(v, int)]
                    vals = (base + [i for i in range(1, 10 * n) if i not in base])[:n]
                elif dtp.startswith("date"):
                    vals = [(lo + dt.timedelta(days=i % (span + 1))).isoformat() for i in range(n)]
                else:
                    vals = [f"{c.name} {i}" for i in range(1, n + 1)]
                vals += [vals[i % len(vals)] for i in range(n - len(vals))]
                keys[key] = vals
            elif low in many_side and _pk(keys, many_side[low]):
                pk = _pk(keys, many_side[low])
                odd = 2 if n > len(pk) + 3 else 0  # the last rows are orphans (and an empty text key) on purpose
                for i in range(n):
                    if i >= n - odd:
                        if dtp == "string":
                            vals.append(f"{c.name} orphan {i}")
                        elif dtp in NUMERIC:
                            vals.append(9999000 + i)
                        elif dtp.startswith("date"):
                            vals.append((hi + dt.timedelta(days=400 + i)).isoformat())
                        else:
                            vals.append(f"{c.name} {i}")
                    elif odd and i == n - odd - 1 and dtp == "string":
                        vals.append("")
                    elif i < len(pk):
                        vals.append(pk[i])
                    else:
                        vals.append(rng.choice(pk))
            else:
                for i in range(n):
                    if i < len(pool):
                        vals.append(pool[i])
                    elif dtp == "string":
                        choices = [v for v in pool if isinstance(v, str)] + [f"{c.name} {k}" for k in range(1, 6)]
                        vals.append(rng.choice(choices))
                    elif dtp == "int64":
                        vals.append(rng.randint(0, 1000))
                    elif dtp in ("double", "decimal"):
                        vals.append(round(rng.uniform(0, 1000), 2))
                    elif dtp.startswith("date"):
                        vals.append((lo + dt.timedelta(days=rng.randint(0, span))).isoformat())
                    elif dtp == "boolean":
                        vals.append(rng.random() < 0.5)
                    else:
                        vals.append("")
                vals = [_coerce(v, dtp) for v in vals]
            data.append(vals)
        out[t.name] = ([(c.name, (c.data_type or "string").lower()) for c in cols], [list(r) for r in zip(*data)])
    return out


def _literal_table(t) -> bool:
    """A calculated table that is only literal rows (DATATABLE / table constructor): the edge profile
    fakes it too, so its rows can carry edge cases."""
    if t.private or t.calc_items or not t.partitions:
        return False
    return all(kind == "calculated" and "nameof" not in (expr or "").lower() and
               bool(re.search(r"(?i)\bDATATABLE\s*\(", expr or "") or (expr or "").lstrip().startswith("{"))
               for _n, kind, expr in t.partitions)


def _edge(model, gen: dict, seed: int, lo: dt.date, hi: dt.date) -> tuple[dict, dict]:
    """Edge-case variant of generated rows (a copy; `gen` is untouched): keys without related rows,
    orphan and blank foreign keys, duplicate and blank labels, zero / negative / blank amounts, blank
    text, dates on year and month boundaries and a month with no rows. Keys of one-side columns and
    every column that takes part in a sort-by stay as they are (Desktop rejects ambiguous sort-by)."""
    out = {t: (cols, [list(r) for r in rows]) for t, (cols, rows) in gen.items()}
    names = {t.lower(): t for t in out}
    notes: dict[str, list] = {}

    def note(table, text):
        notes.setdefault(table, []).append(text)

    one, many = {}, {}
    for r in model.relationships:
        many_card = r.node is not None and r.node.props.get("toCardinality", "").strip() == "many"
        if not many_card:
            one[(r.to_table.lower(), r.to_column.lower())] = (r.to_table, r.to_column)
        many[(r.from_table.lower(), r.from_column.lower())] = (r.to_table, r.to_column)
    sort_cols = set()
    for c in model.all_columns():
        if c.sort_by:
            sort_cols |= {(c.table.lower(), c.name.lower()), (c.table.lower(), c.sort_by.lower())}
    keys = set(one) | {(c.table.lower(), c.name.lower()) for c in model.all_columns()
                       if c.node is not None and c.node.flag("isKey")}

    def cidx(table: str, col: str) -> int | None:
        cols = out[table][0]
        return next((i for i, (n, _d) in enumerate(cols) if n.lower() == col.lower()), None)

    # keys of a one-side table that no related row uses
    for (dt_l, dc_l), (dtab, dcol) in one.items():
        t = names.get(dt_l)
        i = cidx(t, dcol) if t else None
        if i is None or len(out[t][1]) < 6:
            continue
        values = [r[i] for r in out[t][1]]
        unused, used_any = values[-2:], False
        for (ft_l, fc_l), to in many.items():
            ft = names.get(ft_l)
            j = cidx(ft, fc_l) if ft else None
            if j is None or (to[0].lower(), to[1].lower()) != (dt_l, dc_l):
                continue
            used_any = True
            for r in out[ft][1]:
                if r[j] in unused:
                    r[j] = values[0]
        if used_any:
            note(t, f"2 {dcol} values with no related rows")
    # orphan and blank foreign keys
    for (ft_l, fc_l), (tt, tc) in many.items():
        ft = names.get(ft_l)
        j = cidx(ft, fc_l) if ft else None
        if j is None or names.get(tt.lower()) is None or len(out[ft][1]) < 12:
            continue  # only keys into a faked table can be made orphans
        dtp = out[ft][0][j][1]
        for k, pos in enumerate((3, 7, 11)):
            out[ft][1][pos][j] = (f"{out[ft][0][j][0]} orphan edge {k}" if dtp == "string" else 8888000 + k if dtp in NUMERIC
                                  else (hi + dt.timedelta(days=500 + k)).isoformat() if dtp.startswith("date") else None)
        out[ft][1][5][j] = None
        note(ft, f"{out[ft][0][j][0]}: 3 orphan keys, 1 blank key")
    # other columns: blanks, zero and negative numbers, duplicate labels, boundary dates, a sparse month
    one_tables = {k[0] for k in one}
    for t, (cols, rows) in out.items():
        if len(rows) < 4:
            continue
        for i, (cname, dtp) in enumerate(cols):
            low = (t.lower(), cname.lower())
            # keys into a computed date table (CALENDAR) still get boundary dates: they stay inside its range
            into_unfaked_date = low in many and dtp.startswith("date") and names.get(many[low][0].lower()) is None
            if low in keys or (low in many and not into_unfaked_date) or low in sort_cols:
                continue
            if dtp in NUMERIC:
                base = rows[1][i] if isinstance(rows[1][i], (int, float)) and not isinstance(rows[1][i], bool) else 0
                rows[0][i] = 0 if dtp == "int64" else 0.0
                rows[1][i] = -(abs(int(base)) or 7) if dtp == "int64" else -(abs(float(base)) or 12.5)
                rows[2][i] = None
                note(t, f"{cname}: zero, negative and blank values")
            elif dtp == "string":
                if t.lower() in one_tables:
                    rows[1][i] = rows[0][i]
                    rows[2][i] = None
                    note(t, f"{cname}: a label shared by two keys, a blank label")
                else:
                    rows[0][i] = None
                    rows[1][i] = ""
                    note(t, f"{cname}: blank and empty text")
            elif dtp.startswith("date"):
                marks = [lo]
                if dt.date(lo.year, 12, 31) <= hi:
                    marks.append(dt.date(lo.year, 12, 31))
                if dt.date(lo.year + 1, 1, 1) <= hi:
                    marks.append(dt.date(lo.year + 1, 1, 1))
                feb = next((dt.date(y, 3, 1) - dt.timedelta(days=1) for y in range(lo.year, hi.year + 1)
                            if lo <= dt.date(y, 3, 1) - dt.timedelta(days=1) <= hi), None)
                if feb:
                    marks.append(feb)
                marks.append(hi)
                taken = {d.isoformat()[:7] for d in marks}
                gap = after = None
                m = (lo.replace(day=1) + dt.timedelta(days=40)).replace(day=1)
                while m <= hi:  # the first month after the start that holds no boundary date
                    nxt = (m + dt.timedelta(days=40)).replace(day=1)
                    if m.isoformat()[:7] not in taken and nxt <= hi:
                        gap, after = m, nxt
                        break
                    m = nxt
                for r in rows:
                    v = r[i]
                    if gap and isinstance(v, str) and v[:7] == gap.isoformat()[:7]:
                        r[i] = after.isoformat()
                for k, d in enumerate(marks):
                    if k + 1 < len(rows):
                        rows[k + 1][i] = d.isoformat()
                rows[0][i] = None
                note(t, f"{cname}: blank, first and last day, year and month boundaries"
                        + (f", no rows in {gap.isoformat()[:7]}" if gap else ""))
    return out, notes


def fake_dax_tokens(model, text: str) -> str:
    """Bare mirror tokens in a DAX expression (outside strings) become the fake numbers or dates the
    synthetic model uses for them, so checks written against the mirror run on the synthetic copy."""
    lo, hi = _date_range(model)
    pools = _Pools(model, lo, hi)
    edits = []
    for t in lexers.lex_dax(text):
        if t.kind != "pbztok":
            continue
        m = LABEL_RE.match(t.text)
        key = pools.col(m.group(1), m.group(2)) if m else None
        if key and pools.dtype(key).startswith("date"):
            d = _fake_date(t.text, lo, hi)
            edits.append((t.start, t.end, f"DATE({d.year}, {d.month}, {d.day})"))
        elif key and pools.dtype(key) == "string":
            edits.append((t.start, t.end, lexers.dax_string(t.text)))
        else:
            edits.append((t.start, t.end, str(fake_int(t.text))))
    return apply_edits(text, edits) if edits else text


def _pk(keys: dict, target: tuple) -> list:
    """Generated key values of a relationship's one side (case-insensitive), [] when that side is not faked."""
    want = (target[0].lower(), target[1].lower())
    return next((v for k, v in keys.items() if (k[0].lower(), k[1].lower()) == want), [])


def _coerce(v, dtp: str):
    if dtp == "int64":
        return v if isinstance(v, int) and not isinstance(v, bool) else 0
    if dtp in ("double", "decimal"):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    if dtp == "boolean":
        return bool(v)
    return v if isinstance(v, str) else str(v)


def _datatable(columns, rows) -> str:
    ind, sub, row = "\t\t\t\t", "\t\t\t\t    ", "\t\t\t\t        "
    head = [f"{sub}{lexers.dax_string(n)}, {DAX_TYPES.get(d, 'STRING')}," for n, d in columns]
    body = [row + "{" + ", ".join(_fmt(v, d) for v, (_n, d) in zip(r, columns)) + "}" for r in rows]
    return (f"{ind}DATATABLE(\n" + "\n".join(head) + f"\n{sub}{{\n" + ",\n".join(body) + f"\n{sub}}}\n{ind})\n")


# ---------------------------------------------------------------- build


def _token_edits(doc, skip: list[tuple[int, int]], pools: _Pools) -> list:
    """Bare mirror tokens in DAX (tokenized numbers) become fake numbers the fake data also contains."""
    edits = []
    for reg in doc.regions:
        if reg.kind != "dax" or any(s <= reg.start < e for s, e in skip):
            continue
        for t in lexers.lex_dax(doc.text[reg.start:reg.end], reg.start):
            if t.kind == "pbztok":
                m = LABEL_RE.match(t.text)
                key = pools.col(m.group(1), m.group(2)) if m else None
                dtp = pools.dtype(key) if key else ""
                if dtp.startswith("date"):
                    d = _fake_date(t.text, pools.lo, pools.hi)
                    rep = f"DATE({d.year}, {d.month}, {d.day})"
                else:
                    rep = str(fake_int(t.text))
                edits.append((t.start, t.end, rep))
    return edits


def _rewrite_tmdl(model, pools: _Pools, gen: dict, plan: list) -> dict[str, str]:
    """{relpath: new text} for the synthetic model (lineage tags not yet replaced)."""
    out = {}
    rewritten = {t.name for t in plan if t.name in gen}
    for rel, doc in model.docs.items():
        text = doc.text
        edits, skip = [], []
        for node in doc.nodes:
            table = node.parent.name if node.parent is not None and node.parent.kind == "table" else None
            if node.kind == "partition" and table in rewritten:
                s, e = tmdl.block_span(doc, node)
                skip.append((s, e))
                t = model.tables[table]
                if node is t.partition_nodes[0]:
                    cols, rows = gen[table]
                    block = (f"\tpartition {lexers.tmdl_name(node.name)} = calculated\n\t\tmode: import\n\t\tsource =\n"
                             + _datatable(cols, rows) + "\n")
                    edits.append((s, e, block))
                else:
                    edits.append((s, e, ""))
            elif node.kind == "refreshPolicy" and table in rewritten:
                s, e = tmdl.block_span(doc, node)
                skip.append((s, e))
                edits.append((s, e, ""))
            elif node.kind == "column" and table in rewritten:
                c = model.tables[table].columns.get(node.name)
                if c is not None and not c.calculated:
                    s, e = tmdl.block_span(doc, node)
                    blk = doc.text[s:e]
                    for m in re.finditer(r"(?m)^\t\t(sourceColumn|sourceProviderType|sourceLineageTag)\s*:[^\r\n]*", blk):
                        rep = "\t\tsourceColumn: [" + node.name.replace("]", "]]") + "]" if m.group(1) == "sourceColumn" else ""
                        end = m.end()
                        if not rep and blk[end:end + 2] == "\r\n":
                            end += 2
                        elif not rep and blk[end:end + 1] == "\n":
                            end += 1
                        edits.append((s + m.start(), s + end, rep))
                    if "sourceColumn" not in node.props:
                        edits += tmdl.prop_edit(doc, node, "sourceColumn", "[" + node.name.replace("]", "]]") + "]")
            elif node.kind == "member" and node.parent is not None and node.parent.kind == "role":
                s, e = tmdl.block_span(doc, node)
                edits.append((s, e, ""))
        edits += _token_edits(doc, skip, pools)
        if rel == "model.tmdl":
            for m in re.finditer(r"(?m)^ref (cultureInfo|expression) [^\r\n]*\r?\n?", text):
                edits.append((m.start(), m.end(), ""))
        new = apply_edits(text, edits)
        new = re.sub(r"(?m)^\t*sourceLineageTag:[^\r\n]*\r?\n", "", new)
        out[rel] = new
    return out


def _new_lineage(texts: dict[str, str]) -> dict[str, str]:
    return {rel: re.sub(r"(?m)^(\t*lineageTag:[ \t]*)\S+", lambda m: m.group(1) + str(uuid.uuid4()), text)
            for rel, text in texts.items()}


def marker_value(key: bytes, table_tags: list[str]) -> str:
    """HMAC the engine recomputes from the loaded model's non-private table lineage tags."""
    return dpapi.hmac_hex(key, "pbz-synthetic:" + ",".join(sorted(t.strip().lower() for t in table_tags)))


def _table_tags(texts: dict[str, str]) -> list[str]:
    model = tmdl.load_model("", texts=texts)
    tags = []
    for t in model.tables.values():
        if t.node is None or t.private:
            continue
        tag = t.node.props.get("lineageTag", "").strip()
        if tag:
            tags.append(tag)
    return tags


def _report_edits(text: str, pools: _Pools) -> str:
    """Bare tokens standing for numbers / dates in PBIR literals become typed fake literals."""
    try:
        root = pbir.parse_json(text)
    except pbir.JsonError:
        return text
    edits = []
    for key, node in pbir.string_nodes(root):
        if key != "Value" or not TOKEN_RE.fullmatch(node.value):
            continue
        m = LABEL_RE.match(node.value)
        col = pools.col(m.group(1), m.group(2)) if m else None
        dtp = pools.dtype(col) if col else ""
        if dtp.startswith("date"):
            lit = f"datetime'{_fake_date(node.value, pools.lo, pools.hi).isoformat()}T00:00:00'"
        elif dtp in ("double", "decimal"):
            lit = f"{fake_int(node.value)}D"
        elif dtp == "string":
            lit = "'" + node.value + "'"
        else:
            lit = f"{fake_int(node.value)}L"
        edits.append((node.start, node.end, json.dumps(lit, ensure_ascii=False)))
    return apply_edits(text, edits) if edits else text


def build(ws, project, rows: int = 200, seed: int = 7, edge: bool = False) -> dict:
    """edge: the edge-case profile (see `_edge`); literal calculated tables are faked as well."""
    src = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(src):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    if not os.path.isdir(project.mirror_report):
        raise PbzError("UNSUPPORTED", "the mirror has no report; Desktop opens a PBIP only with one")
    rows = max(10, min(int(rows), 5000))
    sp = synth_project(ws, project)
    root = sp.ws.root
    if os.path.exists(root):
        _require_synthetic(root)
        if desktop.find_instance(sp):
            raise PbzError("DESKTOP_OPEN", "close the synthetic model first (`pbz synth close`)")
        shutil.rmtree(root)
    model = tmdl.load_model(src)
    lo, hi = _date_range(model)
    pools = _collect_pools(model, project, lo, hi)
    plan = [t for t in model.tables.values() if _rewrite_table(t) or (edge and _literal_table(t))]
    gen = _generate(model, pools, rows, seed, plan)
    edge_notes: dict = {}
    if edge:
        gen, edge_notes = _edge(model, gen, seed, lo, hi)
    texts = _new_lineage(_rewrite_tmdl(model, pools, gen, plan))
    key = dpapi.load_key(ws.private)
    marker = marker_value(key, _table_tags(texts))
    mt = texts.get("model.tmdl", "")
    eol = "\r\n" if "\r\n" in mt else "\n"
    line = f"annotation {MARKER} = {marker}{eol}{eol}"
    first_ref = re.search(r"(?m)^ref ", mt)
    texts["model.tmdl"] = (mt[:first_ref.start()] + line + mt[first_ref.start():]) if first_ref else \
        (mt.rstrip("\r\n") + eol + eol + line)
    # ---- write the synthetic project
    os.makedirs(os.path.join(root, ".pbz"), exist_ok=True)
    shutil.copytree(project.mirror_model, sp.model_dir, ignore=shutil.ignore_patterns("*.tmdl"))
    for rel, text in texts.items():
        write_text(os.path.join(sp.model_dir, "definition", *rel.split("/")), text)
    expr = os.path.join(sp.model_dir, "definition", "expressions.tmdl")
    if os.path.exists(expr):
        os.remove(expr)  # shared M queries: nothing uses them once every partition is calculated
    shutil.copytree(project.mirror_report, sp.report_dir)
    for path in pbir.report_files(sp.report_dir):
        text, bom = read_text(path)
        new = _report_edits(text, pools)
        if new != text:
            write_text(path, new, bom)
    pbir_file = os.path.join(sp.report_dir, "definition.pbir")
    if os.path.exists(pbir_file):
        ref, bom = read_text(pbir_file)
        data = json.loads(ref)
        data["datasetReference"] = {"byPath": {"path": f"../{sp.name}.SemanticModel"}}
        write_text(pbir_file, json.dumps(data, indent=2) + "\n", bom)
    write_text(sp.pbip, json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": f"{sp.name}.Report"}}],
        "settings": {"enableAutoRecovery": False}}, indent=2) + "\n")
    with open(os.path.join(root, ".pbz", "policy.json"), "w", encoding="utf-8") as f:
        json.dump({"synthetic": True}, f, indent=1)
    rows_by_table = {t: len(r) for t, (_c, r) in gen.items()}
    with open(os.path.join(root, ".pbz", "synth.json"), "w", encoding="utf-8") as f:
        json.dump({"source_project": project.name, "built": dt.datetime.now().isoformat(timespec="seconds"),
                   "rows": rows, "seed": seed, "tables": rows_by_table, "profile": "edge" if edge else "standard"},
                  f, indent=1)
    res = engine_call("tmdl-validate", {"private_dir": ws.private, "folder": os.path.join(sp.model_dir, "definition")},
                      timeout=300)
    errors = [{"file": e.get("file"), "line": e.get("line"), "class": e.get("class")} for e in res.get("errors", [])]
    errors += [{"file": f, "class": "MISSING_FILE"} for f in res.get("missing_refs", [])]
    valid = bool(res.get("ok")) and not errors
    out = {"verb": "synth-build", "project": sp.name, "root": root, "pbip": sp.pbip, "valid": valid,
           "tables_faked": rows_by_table, "date_range": [lo.isoformat(), hi.isoformat()],
           "profile": "edge" if edge else "standard"}
    if edge:
        out["edge_cases"] = edge_notes
    if not res.get("ok"):
        out["validation"] = res.get("class", "ENGINE_UNAVAILABLE")
    if errors:
        out["errors"] = errors[:20]
    out["next"] = "pbz synth open, then pbz synth query \"EVALUATE ...\" (fake data: values are shown)"
    return out


# ---------------------------------------------------------------- lifecycle


def build_info(ws, project) -> dict:
    """Settings of the current synthetic build (source_project, built, rows, seed, tables); refuses a
    folder that is not a pbz synthetic copy."""
    root = synth_project(ws, project).ws.root
    _require_synthetic(root)
    with open(os.path.join(root, ".pbz", "synth.json"), encoding="utf-8") as f:
        return json.load(f)


def status(ws, project) -> dict:
    sp = synth_project(ws, project)
    root = sp.ws.root
    if not _is_synthetic_root(root):
        return {"verb": "synth-status", "built": False, "root": root, "next": "pbz synth build"}
    with open(os.path.join(root, ".pbz", "synth.json"), encoding="utf-8") as f:
        info = json.load(f)
    return {"verb": "synth-status", "built": True, "root": root, "pbip": sp.pbip, "built_at": info.get("built"),
            "tables_faked": info.get("tables"), "desktop": desktop.status(sp)}


def open_(ws, project, wait: int = 480) -> dict:
    sp = synth_project(ws, project)
    _require_synthetic(sp.ws.root)
    res = desktop.open_(sp, wait)
    inst = desktop.find_instance(sp)
    port = desktop.engine_port(int(inst["pid"])) if inst and inst.get("pid") else None
    if port:  # calculated tables are computed on open; recalculate in case Desktop deferred it
        engine_call("refresh", {"private_dir": sp.ws.private, "port": port}, timeout=1800)
    return {**res, "verb": "synth-open", "project": sp.name}


def close(ws, project) -> dict:
    sp = synth_project(ws, project)
    _require_synthetic(sp.ws.root)
    return {**desktop.close(sp), "verb": "synth-close", "project": sp.name}


def remove(ws, project) -> dict:
    sp = synth_project(ws, project)
    root = sp.ws.root
    if not os.path.exists(root):
        return {"verb": "synth-remove", "result": "NOTHING_TO_REMOVE"}
    _require_synthetic(root)
    if desktop.find_instance(sp):
        raise PbzError("DESKTOP_OPEN", "close the synthetic model first (`pbz synth close`)")
    shutil.rmtree(root)
    return {"verb": "synth-remove", "result": "REMOVED", "root": root}


def query(ws, project, dax: str, max_rows: int = 500) -> dict:
    if not dax or not dax.strip():
        raise PbzError("BAD_REQUEST", "pass an EVALUATE statement")
    sp = synth_project(ws, project)
    _require_synthetic(sp.ws.root)
    inst = desktop.find_instance(sp)  # the instance must hold the synthetic PBIP itself
    if not inst or not inst.get("pid"):
        raise PbzError("ENGINE_UNAVAILABLE", "open the synthetic model first (`pbz synth open`)")
    port = desktop.engine_port(int(inst["pid"]))
    if not port:
        raise PbzError("ENGINE_UNAVAILABLE", "the synthetic model's engine is not ready yet")
    res = engine_call("synth-query", {"private_dir": ws.private, "port": port, "dax": dax, "max_rows": max_rows},
                      timeout=300)
    if not res.get("ok"):
        raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"),
                       "refused or failed (synthetic marker, calculated partitions and DAX are checked)")
    return {"verb": "synth-query", "project": sp.name, "columns": res.get("columns", []), "rows": res.get("rows", []),
            "truncated": res.get("truncated", False), "data": "synthetic"}


def run(ws, project, a) -> dict:
    if a.action == "build":
        return build(ws, project, a.rows, a.seed, bool(getattr(a, "edge", False)))
    if a.action == "check":  # spec from --spec, else the positional argument (JSON or a .json file)
        from . import synthtest
        return synthtest.check(ws, project, getattr(a, "spec", None) or a.dax)
    if a.action == "status":
        return status(ws, project)
    if a.action == "open":
        return open_(ws, project, a.wait)
    if a.action == "close":
        return close(ws, project)
    if a.action == "remove":
        return remove(ws, project)
    if a.action == "query":
        return query(ws, project, a.dax or "")
    raise PbzError("BAD_REQUEST", a.action)
