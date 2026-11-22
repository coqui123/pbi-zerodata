"""`pbz similar`: duplicate and near-duplicate measures from normalized DAX. Names and scores only.

canon(): significant tokens with functions uppercased, names casefolded, bare [x] resolved to T[x] or
the measure, numbers as exact decimals, strings and mirror tokens as opaque literal slots, VAR names renamed
$v1.., outer parentheses and a trivial `VAR x = E RETURN x` stripped. A filterless top-level
CALCULATE(E) is stripped for candidate discovery only (it performs context transition), never for
"identical". Groups: identical (same canon, literals compared exactly), alias (the body is one other
measure), same_shape (same canon once literals, columns and measures become slots: a calculation
group candidate). Fuzzy pairs (difflib) are blocked by shared token 3-grams. Literal values are never
shown, only that a literal differs.
"""
from __future__ import annotations

import difflib
from decimal import Decimal, InvalidOperation
import hashlib
import os
from dataclasses import dataclass, field

from . import deps, lexers, tmdl
from .common import PbzError

LIT_KINDS = ("str", "num", "pbztok")
SLOT_KINDS = ("lit", "col", "meas")
MIN_TOKENS = 5
MIN_SHAPE_TOKENS = 8


@dataclass
class Canon:
    toks: list = field(default_factory=list)   # [(token, kind, value)]; kind "" | lit | col | meas
    uncertain: bool = False

    def words(self, literals: str = "hash") -> list[str]:
        """Token strings. literals: "hash" (private exact compare), "slot" (any literal), "shape" (all slots)."""
        out = []
        for tok, kind, val in self.toks:
            if kind == "lit":
                out.append("LIT:" + val if literals == "hash" else "LIT")
            elif kind in ("col", "meas") and literals == "shape":
                out.append(kind.upper())
            else:
                out.append(tok)
        return out

    def refs(self) -> set[str]:
        return {val for _t, kind, val in self.toks if kind in ("col", "meas")}

    def measures(self) -> set[str]:
        return {val for _t, kind, val in self.toks if kind == "meas"}


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _num(text: str) -> str:
    try:
        sign, digits, exponent = Decimal(text).as_tuple()
        digits = list(digits)
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        if not digits:
            return "N0"
        return "N" + ("-" if sign else "") + "".join(map(str, digits)) + "e" + str(exponent)
    except InvalidOperation:
        return "N" + text


def canon(expr: str, home: str, res: deps.Resolver) -> Canon:
    sig = lexers.significant(lexers.lex_dax(expr))
    bindings = deps.dax_bindings(expr, home, res)
    by_span = {(b.start, b.end): b for b in bindings.bindings}
    var_names: dict[str, str] = {}
    var_rhs: dict[str, list] = {}
    for i, t in enumerate(sig):
        if t.kind == "ident" and t.text.upper() == "VAR" and i + 1 < len(sig) and sig[i + 1].kind == "ident":
            var_names.setdefault(sig[i + 1].text.lower(), f"$v{len(var_names) + 1}")
            if i + 2 < len(sig) and sig[i + 2].kind == "op" and sig[i + 2].text == "=":
                rhs, depth = [], 0
                for u in sig[i + 3:]:
                    if u.kind == "op" and u.text in ("(", "{"):
                        depth += 1
                    elif u.kind == "op" and u.text in (")", "}"):
                        depth -= 1
                    if depth == 0 and u.kind == "ident" and u.text.upper() in ("RETURN", "VAR"):
                        break
                    rhs.append(u)
                var_rhs[sig[i + 1].text.lower()] = rhs

    TABLE_FUNCS = {"ALL", "ALLEXCEPT", "ALLNOBLANKROW", "ALLSELECTED", "VALUES", "DISTINCT",
                   "FILTER", "CALCULATETABLE", "SUMMARIZE", "SUMMARIZECOLUMNS", "TOPN", "UNION",
                   "INTERSECT", "EXCEPT", "TREATAS", "GENERATE", "GENERATEALL", "GENERATESERIES",
                   "ADDCOLUMNS", "SELECTCOLUMNS", "GROUPBY", "ROW", "DATATABLE",
                   "NATURALINNERJOIN", "NATURALLEFTOUTERJOIN", "SAMPLE", "INDEX", "OFFSET", "WINDOW"}

    def _rhs_is_table(rhs) -> bool:
        for j, u in enumerate(rhs):
            nxt = rhs[j + 1] if j + 1 < len(rhs) else None
            if nxt is not None and nxt.kind == "bracket":
                continue
            if u.kind == "table":
                return True
            if u.kind == "ident" and res.table(u.text):
                return True
            if u.kind == "ident" and u.text.upper() in TABLE_FUNCS \
                    and nxt is not None and nxt.kind == "op" and nxt.text == "(":
                return True
        return False

    def _single_scalar_call(rhs) -> bool:
        if len(rhs) < 3 or rhs[0].kind != "ident" or rhs[0].text.upper() in TABLE_FUNCS \
                or rhs[1].kind != "op" or rhs[1].text != "(":
            return False
        depth = 0
        for j, u in enumerate(rhs):
            if u.kind == "op" and u.text in ("(", "{"):
                depth += 1
            elif u.kind == "op" and u.text in (")", "}"):
                depth -= 1
                if depth == 0:
                    return j == len(rhs) - 1
        return False

    table_vars = {name for name, rhs in var_rhs.items() if _rhs_is_table(rhs)}
    changed = True
    while changed:
        changed = False
        for name, rhs in var_rhs.items():
            if name not in table_vars and not _single_scalar_call(rhs) \
                    and any(u.kind == "ident" and u.text.lower() in table_vars for u in rhs):
                table_vars.add(name)
                changed = True
    extension = {t.value.lower() for t in sig if t.kind == "str"}
    toks: list[tuple[str, str, str]] = []
    uncertain = False
    stack: list[dict] = []   # {"fn", "arg", "row_table"}: mirrors the deps.dax_bindings frames

    def learn(table) -> None:
        if table:
            for fr in stack:
                if fr["fn"] in deps.ITERATORS and fr["arg"] == 0 and fr["row_table"] is None \
                        and not fr["poisoned"]:
                    fr["row_table"] = table

    i, n = 0, len(sig)
    while i < n:
        t = sig[i]
        nxt = sig[i + 1] if i + 1 < n else None
        if t.kind == "op" and t.text == "(":
            prev = sig[i - 1] if i else None
            fn = prev.text.upper() if prev is not None and prev.kind == "ident" else ""
            stack.append({"fn": fn, "arg": 0, "row_table": None, "poisoned": False})
        elif t.kind == "op" and t.text == ")":
            if stack:
                stack.pop()
        elif t.kind == "op" and t.text == "," and stack:
            stack[-1]["arg"] += 1
        if t.kind in LIT_KINDS:
            val = _num(t.text) if t.kind == "num" else ("T" + t.text if t.kind == "pbztok" else "S" + t.value)
            toks.append(("LIT", "lit", val))
        elif t.kind in ("table", "ident") and nxt is not None and nxt.kind == "bracket" and (
                t.kind == "table" or t.text.lower() in var_names or res.table(t.text)):
            binding = by_span.get((nxt.start, nxt.end))
            if binding is not None:
                uncertain |= binding.uncertain
            else:
                uncertain = True
            if t.kind == "ident" and t.text.lower() in var_names:
                toks.append((f"{var_names[t.text.lower()]}[{nxt.value.lower()}]", "", ""))
            else:
                qual = t.value if t.kind == "table" else t.text
                tab = res.table(qual)
                col = res.column(tab, nxt.value) if tab else None
                if col:
                    lab = f"{tab}[{col}]"
                    toks.append((lab.lower(), "col", lab))
                    learn(tab)
                elif res.measure(nxt.value):
                    lab = f"[{res.measure(nxt.value)[1]}]"
                    toks.append((lab.lower(), "meas", lab))
                else:
                    lab = f"{qual}[{nxt.value}]"
                    toks.append((lab.lower(), "col", lab))
            i += 2
            continue
        elif t.kind == "bracket":
            binding = by_span.get((t.start, t.end))
            if binding is not None:
                ref = binding.ref
                uncertain |= binding.uncertain
            else:
                uncertain = True
                ref = deps._bare(t.value, home, res, False, [], extension)
            if any(fr["fn"] in deps.ROW_CONTEXT and fr["arg"] >= 1 and fr["row_table"] is None
                   for fr in stack):
                uncertain = True
            if ref.kind == "column":
                learn(ref.table)
            if ref.kind == "measure":
                lab = f"[{ref.name}]"
                toks.append((lab.lower(), "meas", lab))
            elif ref.kind == "column":
                lab = f"{ref.table}[{ref.name}]"
                toks.append((lab.lower(), "col", lab))
            else:
                toks.append((f"[{t.value.lower()}]", "", ""))
        elif t.kind == "table":
            binding = by_span.get((t.start, t.end))
            if binding is not None:
                uncertain |= binding.uncertain
            tab = res.table(t.value) or t.value
            toks.append(("t:" + tab.lower(), "", ""))
            learn(res.table(t.value))
        elif t.kind == "ident":
            low = t.text.lower()
            if nxt is not None and nxt.kind == "op" and nxt.text == "(":
                toks.append(("fn:" + low if low in res.functions else t.text.upper(), "", ""))
            elif low in var_names:
                toks.append((var_names[low], "", ""))
                prev = sig[i - 1] if i else None
                if low in table_vars and not (prev is not None and prev.kind == "ident"
                                              and prev.text.upper() == "VAR"):
                    for fr in stack:
                        if fr["fn"] in deps.ITERATORS and fr["arg"] == 0:
                            fr["row_table"] = None
                            fr["poisoned"] = True
            elif res.table(t.text):
                toks.append(("t:" + res.table(t.text).lower(), "", ""))
                learn(res.table(t.text))
            else:
                toks.append((t.text.upper(), "", ""))
        else:
            toks.append((t.text, "", ""))
        i += 1
    toks = _strip_parens(toks)
    words = [x[0] for x in toks]
    if words.count("VAR") == 1 and len(words) >= 6 and words[0] == "VAR" and words[-2] == "RETURN" \
            and words[-1] == words[1] and words[2] == "=":
        toks = _strip_parens(toks[3:-2])
    return Canon(toks, uncertain)


def _close_of(toks, i: int) -> int:
    depth = 0
    for j in range(i, len(toks)):
        if toks[j][0] in ("(", "{"):
            depth += 1
        elif toks[j][0] in (")", "}"):
            depth -= 1
            if depth == 0:
                return j
    return -1


def _strip_parens(toks: list) -> list:
    while len(toks) >= 2 and toks[0][0] == "(" and _close_of(toks, 0) == len(toks) - 1:
        toks = toks[1:-1]
    return toks


def discovery(c: Canon) -> Canon:
    """Canon with a filterless top-level CALCULATE(E) reduced to E (candidate discovery only)."""
    toks = c.toks
    while len(toks) >= 4 and toks[0][0] == "CALCULATE" and toks[1][0] == "(" and _close_of(toks, 1) == len(toks) - 1:
        inner, depth = toks[2:-1], 0
        top_comma = False
        for tok, _k, _v in inner:
            depth += tok in ("(", "{")
            depth -= tok in (")", "}")
            top_comma = top_comma or (tok == "," and depth == 0)
        if top_comma:
            break
        toks = _strip_parens(inner)
    return Canon(toks, c.uncertain)


def _grams(words: list[str]) -> set:
    return {tuple(words[i:i + 3]) for i in range(len(words) - 2)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _slot_diffs(a: Canon, b: Canon) -> list[str]:
    out, lit = [], 0
    sa = [(k, v) for _t, k, v in a.toks if k in SLOT_KINDS]
    sb = [(k, v) for _t, k, v in b.toks if k in SLOT_KINDS]
    for (ka, va), (kb, vb) in zip(sa, sb):
        if va == vb:
            continue
        if ka == "lit" or kb == "lit":
            lit += 1
        else:
            out.append(f"reference differs: {va} vs {vb}")
    if lit:
        out.insert(0, "literal differs" if lit == 1 else f"{lit} literals differ")
    return out


def run(ws, project, threshold: float = 0.80, max_pairs: int = 100) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef, root=os.path.dirname(project.mirror_model))
    res = deps.Resolver(model)
    ms = [m for m in model.all_measures() if m.expression.strip()]
    lab = {id(m): f"[{m.name}]" for m in ms}
    canons = {id(m): canon(m.expression, m.table, res) for m in ms}
    disc = {i: discovery(c) for i, c in canons.items()}

    # identical
    by_key: dict[tuple, list] = {}
    for m in ms:
        by_key.setdefault(tuple(canons[id(m)].words("hash")), []).append(m)
    identical = [grp for grp in by_key.values() if len(grp) > 1]
    group_of = {id(m): gi for gi, grp in enumerate(identical) for m in grp}
    # aliases
    aliases = []
    alias_pairs = set()
    for m in ms:
        d = disc[id(m)]
        if len(d.toks) == 1 and d.toks[0][1] == "meas" and d.toks[0][2].lower() != lab[id(m)].lower():
            aliases.append({"measure": lab[id(m)], "table": m.table, "alias_of": d.toks[0][2]})
            alias_pairs.add(frozenset((lab[id(m)].lower(), d.toks[0][2].lower())))
    # same shape (slots: literals, columns, measures)
    shapes: dict[tuple, list] = {}
    for m in ms:
        w = canons[id(m)].words("shape")
        if len(w) >= MIN_SHAPE_TOKENS:
            shapes.setdefault(tuple(w), []).append(m)
    same_shape = []
    shape_of: dict[int, int] = {}
    for grp in shapes.values():
        if len(grp) < 2 or len({group_of.get(id(m), -1 - k) for k, m in enumerate(grp)}) < 2:
            continue
        for m in grp:
            shape_of[id(m)] = len(same_shape)
        first = grp[0]
        diffs = []
        for m in grp[1:]:
            if group_of.get(id(m)) is not None and group_of.get(id(m)) == group_of.get(id(first)):
                continue
            diffs.append({"measure": lab[id(m)], "vs": lab[id(first)],
                          "slots": _slot_diffs(canons[id(first)], canons[id(m)])})
        same_shape.append({"measures": [lab[id(m)] for m in grp], "differences": diffs,
                           "hint": "same pattern over different inputs: calculation group or parameter candidate"})
    # fuzzy pairs blocked by shared 3-grams
    words = {id(m): disc[id(m)].words("slot") for m in ms}
    grams = {i: _grams(w) for i, w in words.items()}
    df: dict = {}
    for gset in grams.values():
        for gr in gset:
            df[gr] = df.get(gr, 0) + 1
    limit = max(3, int(0.2 * len(ms)))
    post: dict = {}
    for m in ms:
        if len(words[id(m)]) < MIN_TOKENS:
            continue
        for gr in grams[id(m)]:
            if df[gr] <= limit:
                post.setdefault(gr, []).append(m)
    shared: dict = {}
    objs: dict = {}
    order = {id(m): i for i, m in enumerate(ms)}
    for lst in post.values():
        for x in range(len(lst)):
            for y in range(x + 1, len(lst)):
                a, b = sorted((lst[x], lst[y]), key=lambda m: order[id(m)])
                k = (id(a), id(b))
                objs[k] = (a, b)
                shared[k] = shared.get(k, 0) + 1
    pairs = []
    for (pa, pb), cnt in shared.items():
        a, b = objs[(pa, pb)]
        if cnt < 2:
            continue
        if group_of.get(pa) is not None and group_of.get(pa) == group_of.get(pb):
            continue
        if shape_of.get(pa) is not None and shape_of.get(pa) == shape_of.get(pb):
            continue  # already reported as same_shape with its slot differences
        if frozenset((lab[pa].lower(), lab[pb].lower())) in alias_pairs:
            continue
        wa, wb = words[pa], words[pb]
        expr = 0.5 * difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio() + \
            0.25 * _jaccard(grams[pa], grams[pb]) + 0.25 * _jaccard(canons[pa].refs(), canons[pb].refs())
        dependent = lab[pb] in canons[pa].measures() or lab[pa] in canons[pb].measures()
        if dependent and expr < 0.95:
            continue
        name = difflib.SequenceMatcher(None, a.name.casefold(), b.name.casefold(), autojunk=False).ratio()
        score = 0.70 * expr + 0.12 * name + 0.06 * (a.format_string == b.format_string) + \
            0.06 * (a.folder == b.folder) + 0.06 * (a.table == b.table)
        if score >= max(threshold, 0.90) and expr >= 0.92:
            cls = "near_duplicate"
        elif score >= threshold and expr >= 0.80:
            cls = "similar"
        else:
            continue
        rec = {"a": lab[pa], "b": lab[pb], "class": cls, "score": round(score, 3), "expr": round(expr, 3)}
        diffs = _slot_diffs(canons[pa], canons[pb]) if canons[pa].words("shape") == canons[pb].words("shape") else []
        if diffs:
            rec["slots"] = diffs
        pairs.append(rec)
    pairs.sort(key=lambda p: (-p["score"], p["a"].lower(), p["b"].lower()))
    return {"verb": "similar", "project": project.name, "measures": len(ms), "threshold": threshold,
            "identical": [{"measures": [lab[id(m)] for m in grp], "tables": sorted({m.table for m in grp})}
                          for grp in identical],
            "aliases": aliases, "same_shape": same_shape, "pairs": pairs[:max_pairs],
            "pairs_total": len(pairs)}


_STRUCT = ("tables", "columns", "measures", "relationships")


def _structure(project) -> dict[str, set[str]]:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef, root=os.path.dirname(project.mirror_model))
    tables, columns, measures, rels = set(), set(), set(), set()
    for table in model.tables.values():
        if table.private:
            continue
        tables.add(table.name.lower())
        columns.update(f"{table.name.lower()}[{name.lower()}]" for name in table.columns)
        measures.update(name.lower() for name in table.measures)
    for rel in model.relationships:
        rels.add(f"{rel.from_table.lower()}[{rel.from_column.lower()}]->"
                 f"{rel.to_table.lower()}[{rel.to_column.lower()}]")
    return {"tables": tables, "columns": columns, "measures": measures, "relationships": rels}


def _overlap_band(shared: int, left: int, right: int) -> str:
    if shared <= 0:
        return "NONE"
    base = max(1, min(left, right))
    ratio = shared / base
    if ratio < 0.34:
        return "LOW"
    if ratio < 0.67:
        return "MID"
    return "HIGH"


def models(ws, names, against=None) -> dict:
    """Structural overlap of projects in one workspace. Counts are shared names, never a similarity float."""
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)) or not names:
        raise PbzError("BAD_REQUEST", "name the projects to compare")
    wanted = list(names)
    if against:
        if not isinstance(against, str):
            raise PbzError("BAD_REQUEST", "against is a project name")
        wanted.append(against)
    by_name = {p.name.lower(): p for p in ws.projects}
    chosen = []
    for name in wanted:
        if not isinstance(name, str) or not name.strip():
            raise PbzError("BAD_REQUEST", "project names are strings")
        hit = by_name.get(name.strip().lower())
        if hit is None:
            raise PbzError("NO_PROJECT", name)
        chosen.append(hit)
    structs = {}
    for project in chosen:
        if project.name not in structs:
            structs[project.name] = _structure(project)
    if against:
        target = by_name[against.strip().lower()].name
        resolved = list(dict.fromkeys(by_name[a.strip().lower()].name for a in names))
        pairs = [(a, target) for a in resolved if a.lower() != target.lower()]
    else:
        resolved = list(dict.fromkeys(by_name[a.strip().lower()].name for a in names))
        pairs = [(resolved[i], resolved[j]) for i in range(len(resolved)) for j in range(i + 1, len(resolved))
                 if resolved[i] != resolved[j]]
    rows = []
    for left_name, right_name in pairs:
        left, right = structs[left_name], structs[right_name]
        shared = {kind: len(left[kind] & right[kind]) for kind in _STRUCT}
        total = sum(shared.values())
        rows.append({"a": left_name, "b": right_name, "overlap_band": _overlap_band(
            total, sum(len(left[k]) for k in _STRUCT), sum(len(right[k]) for k in _STRUCT)),
            "shared": shared})
    return {"verb": "similar", "action": "models", "pairs": rows}
