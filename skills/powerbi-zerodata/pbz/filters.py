"""`pbz filter` and `pbz interaction`: report, page and visual filters and cross-visual interactions,
edited in the mirror.

Filter values are category labels, values you type, or mirror tokens (`«Table[Column]#n»`, which
replace the whole literal and become the real value on push); nothing here reads data. Kinds:
- basic: `--in v1 v2` / `--not-in v1 v2` (Categorical, In / Not In)
- advanced: `--where ">= 100"` once or twice (AND), on a column or, at visual level, a measure
- Top N: `--top N --by [Measure] [--bottom]` (TopN with an ordered subquery)
- empty: the field in the filter pane without a condition
Interactions set how one visual affects another on its page: filter, highlight, none or default.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
from datetime import datetime as _dt

from . import author, tmdl
from .common import PbzError, read_text, write_text
from .tokens import TOKEN_RE

OPS = {"=": 0, "==": 0, ">": 1, ">=": 2, "<": 3, "<=": 4}
OP_TEXT = {v: k for k, v in OPS.items() if k != "=="}
MODES = {"filter": "DataFilter", "highlight": "HighlightFilter", "none": "NoFilter"}


def _id() -> str:
    return secrets.token_hex(10)


def _alias(table: str, used: set) -> str:
    base = (re.sub(r"[^a-z]", "", table.lower())[:1] or "t")
    alias, i = base, 1
    while alias in used:
        i += 1
        alias = f"{base}{i}"
    used.add(alias)
    return alias


def literal(raw: str, dtype: str) -> str:
    """PBIR literal for a value of a column type; a mirror token stands for the whole literal."""
    raw = raw.strip()
    if TOKEN_RE.fullmatch(raw):
        return raw
    if TOKEN_RE.search(raw):
        raise PbzError("BAD_REQUEST", "a mirror token must be the whole value")
    t = (dtype or "string").lower()
    try:
        if t in ("int64", "integer", "whole"):
            return f"{int(raw)}L"
        if t in ("double", "number"):
            return _finite_literal(raw, "D", dtype)
        if t in ("decimal", "currency"):
            return _finite_literal(raw, "M", dtype)
    except ValueError:
        raise PbzError("BAD_REQUEST", f"value is not a number ({dtype} column)")
    if t == "boolean":
        if raw.lower() not in ("true", "false"):
            raise PbzError("BAD_REQUEST", "use true or false")
        return raw.lower()
    if t == "datetime":
        if not re.match(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$", raw):
            raise PbzError("BAD_REQUEST", "use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
        try:
            if "T" not in raw:
                _dt.strptime(raw, "%Y-%m-%d")
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", raw):
                _dt.strptime(raw, "%Y-%m-%dT%H:%M")
            else:
                _dt.strptime(raw, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            raise PbzError("BAD_REQUEST", "use a real calendar date as YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
        return f"datetime'{raw if 'T' in raw else raw + 'T00:00:00'}'"
    return "'" + raw.replace("'", "''") + "'"


def _finite_literal(raw: str, suffix: str, dtype: str) -> str:
    v = float(raw)
    if not math.isfinite(v):
        raise PbzError("BAD_REQUEST", f"value is not a finite number ({dtype} column)")
    return (str(int(v)) if v.is_integer() else repr(v)) + suffix


def _ref(kind: str, source_or_entity: str, name: str, by_source: bool) -> dict:
    key = "Measure" if kind == "measure" else "Column"
    sref = {"Source": source_or_entity} if by_source else {"Entity": source_or_entity}
    return {key: {"Expression": {"SourceRef": sref}, "Property": name}}


def build(model, field: str, values_in: list[str] | None = None, values_out: list[str] | None = None,
          where: list[str] | None = None, top: int | None = None, by: str | None = None,
          bottom: bool = False) -> dict:
    """One PBIR filter object for `field` ('T'[C] or [Measure])."""
    kind, table, name, dtype = author._parse_field(model, field)
    used: set = set()
    a = _alias(table, used)
    out = {"name": _id(), "field": _ref(kind, table, name, False)}
    if (by or bottom) and top is None:
        raise PbzError("BAD_REQUEST", "ranking modifiers need --top")
    if sum(bool(x) for x in (values_in, values_out, where, top is not None)) > 1:
        raise PbzError("BAD_REQUEST", "one condition kind per filter: --in, --not-in, --where or --top")
    if values_in or values_out:
        if kind == "measure":
            raise PbzError("BAD_REQUEST", "basic filters need a column; use --where for a measure")
        cond = {"In": {"Expressions": [_ref(kind, a, name, True)],
                       "Values": [[{"Literal": {"Value": literal(v, dtype)}}] for v in (values_in or values_out)]}}
        if values_out:
            cond = {"Not": {"Expression": cond}}
        out.update(type="Categorical", filter={"Version": 2, "From": [{"Name": a, "Entity": table, "Type": 0}],
                                                "Where": [{"Condition": cond}]})
    elif where:
        if len(where) > 2:
            raise PbzError("BAD_REQUEST", "at most two --where conditions (combined with AND)")
        comps = []
        for w in where:
            m = re.match(r"^\s*(>=|<=|==|=|>|<)\s*(.+?)\s*$", w)
            if not m:
                raise PbzError("BAD_REQUEST", "use an operator and a value, for example \">= 100\"")
            vt = "double" if kind == "measure" else dtype
            comps.append({"Comparison": {"ComparisonKind": OPS[m.group(1)], "Left": _ref(kind, a, name, True),
                                         "Right": {"Literal": {"Value": literal(m.group(2), vt)}}}})
        cond = comps[0] if len(comps) == 1 else {"And": {"Left": comps[0], "Right": comps[1]}}
        out.update(type="Advanced", filter={"Version": 2, "From": [{"Name": a, "Entity": table, "Type": 0}],
                                             "Where": [{"Condition": cond}]})
    elif top is not None:
        if kind == "measure":
            raise PbzError("BAD_REQUEST", "Top N filters a column by a measure: --top N --by [Measure]")
        if not by or isinstance(top, bool) or not isinstance(top, int) or top < 1:
            raise PbzError("BAD_REQUEST", "--top needs a positive count and --by [Measure]")
        bk, bt, bn, _bd = author._parse_field(model, by)
        if bk != "measure":
            raise PbzError("BAD_REQUEST", "--by takes a measure")
        sub_used: set = set()
        sa = _alias(table, sub_used)
        sub_from = [{"Name": sa, "Entity": table, "Type": 0}]
        ma = sa if bt == table else _alias(bt, sub_used)
        if ma != sa:
            sub_from.append({"Name": ma, "Entity": bt, "Type": 0})
        sub = {"Query": {"Version": 2, "From": sub_from,
                         "Select": [dict(_ref("column", sa, name, True), Name="field")],
                         "OrderBy": [{"Direction": 1 if bottom else 2, "Expression": _ref("measure", ma, bn, True)}],
                         "Top": int(top)}}
        out.update(type="TopN", filter={"Version": 2, "From": [{"Name": "subquery", "Expression": {"Subquery": sub}, "Type": 2},
                                                               {"Name": a, "Entity": table, "Type": 0}],
                                         "Where": [{"Condition": {"In": {"Expressions": [_ref(kind, a, name, True)],
                                                                         "Table": {"SourceRef": {"Source": "subquery"}}}}}]})
    else:
        out["type"] = "Advanced" if kind == "measure" else "Categorical"
    out["howCreated"] = "User"
    return out


# ---------------------------------------------------------------- scopes

def _scope_file(project, page: str | None, visual: str | None, report: bool) -> tuple[str, str]:
    """(json path, scope label)"""
    ddir = os.path.join(project.mirror_report, "definition")
    if report:
        if page or visual:
            raise PbzError("BAD_REQUEST", "--report takes no page or visual")
        return os.path.join(ddir, "report.json"), "report"
    if not page:
        raise PbzError("BAD_REQUEST", "give --report, or a page (and a visual)")
    from .style import targets
    if visual:
        t = targets(project, page, visual, None, False)[0]
        return t["path"], f"visual {t['id']}"
    pdir = os.path.join(ddir, "pages")
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        pj = os.path.join(pdir, pid, "page.json")
        if not os.path.isfile(pj):
            continue
        name = json.loads(read_text(pj)[0]).get("displayName") or pid
        if page.lower() in (pid.lower(), str(name).lower()):
            return pj, f"page {name}"
    raise PbzError("NOT_FOUND", f"page {page}")


def _all_filter_names(project) -> set:
    names = set()
    ddir = os.path.join(project.mirror_report, "definition")
    for cur, _d, fs in os.walk(ddir):
        for f in fs:
            if f in ("report.json", "page.json", "visual.json"):
                try:
                    d = json.loads(read_text(os.path.join(cur, f))[0])
                except ValueError:
                    continue
                names |= {x.get("name") for x in (d.get("filterConfig") or {}).get("filters") or [] if isinstance(x, dict)}
    return names


def _label(flt: dict) -> str:
    f = flt.get("field") or {}
    inner = f.get("Column") or f.get("Measure") or {}
    ent = ((inner.get("Expression") or {}).get("SourceRef") or {}).get("Entity", "")
    prop = inner.get("Property", "")
    return f"[{prop}]" if "Measure" in f else f"'{ent}'[{prop}]"


def _summary(flt: dict) -> str:
    where = ((flt.get("filter") or {}).get("Where") or [])
    if flt.get("type") == "TopN":
        sub = next((x for x in (flt.get("filter") or {}).get("From") or [] if x.get("Type") == 2), {})
        q = ((sub.get("Expression") or {}).get("Subquery") or {}).get("Query") or {}
        ob = (q.get("OrderBy") or [{}])[0]
        m = ((ob.get("Expression") or {}).get("Measure") or {}).get("Property", "?")
        return f"{'bottom' if ob.get('Direction') == 1 else 'top'} {q.get('Top')} by [{m}]"
    if not where:
        return "no condition"

    def cond(c):
        if "Not" in c:
            return "not " + cond((c["Not"] or {}).get("Expression") or {})
        if "In" in c:
            vals = [((v[0] or {}).get("Literal") or {}).get("Value", "?") for v in c["In"].get("Values") or [] if v]
            return "in (" + ", ".join(vals[:20]) + (", ..." if len(vals) > 20 else "") + ")"
        if "Comparison" in c:
            cc = c["Comparison"]
            return f"{OP_TEXT.get(cc.get('ComparisonKind'), '?')} {((cc.get('Right') or {}).get('Literal') or {}).get('Value', '?')}"
        if "And" in c:
            return cond(c["And"].get("Left") or {}) + " and " + cond(c["And"].get("Right") or {})
        return "(" + ", ".join(sorted(c)) + ")"
    return "; ".join(cond((w or {}).get("Condition") or {}) for w in where)


# ---------------------------------------------------------------- verbs

def add(ws, project, field: str, page: str | None = None, visual: str | None = None, report: bool = False,
        values_in=None, values_out=None, where=None, top=None, by=None, bottom=False) -> dict:
    model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
    path, scope = _scope_file(project, page, visual, report)
    flt = build(model, field, values_in, values_out, where, top, by, bottom)
    if "Measure" in flt["field"] and not visual:
        raise PbzError("BAD_REQUEST", "measure filters work at visual level only: name the visual")
    taken = _all_filter_names(project)
    while flt["name"] in taken:
        flt["name"] = _id()
    text, bom = read_text(path)
    doc = json.loads(text)
    filters = doc.setdefault("filterConfig", {}).setdefault("filters", [])
    filters.append(flt)
    write_text(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n", bom)
    return {"verb": "filter", "action": "add", "scope": scope, "name": flt["name"], "field": _label(flt),
            "type": flt["type"], "condition": _summary(flt),
            "files": [os.path.relpath(path, ws.mirror).replace("\\", "/")],
            "next": "pbz validate, then pbz push --reload (report-only change)"}


def remove(ws, project, target: str, page: str | None = None, visual: str | None = None, report: bool = False) -> dict:
    path, scope = _scope_file(project, page, visual, report)
    text, bom = read_text(path)
    doc = json.loads(text)
    filters = (doc.get("filterConfig") or {}).get("filters") or []
    low = target.strip().lower()
    keep = [f for f in filters if not (str(f.get("name", "")).lower() == low or _label(f).lower() == low
                                         or _label(f).lower().replace("'", "") == low.replace("'", ""))]
    if len(keep) == len(filters):
        raise PbzError("NOT_FOUND", f"no filter {target} at {scope} (`pbz filter list`)")
    if any(f.get("howCreated") == "Drillthrough" for f in filters if f not in keep):
        raise PbzError("UNSUPPORTED", "that is a drillthrough filter: change the drillthrough page instead")
    doc["filterConfig"]["filters"] = keep
    if not keep:
        doc["filterConfig"].pop("filters")
        if not doc["filterConfig"]:
            doc.pop("filterConfig")
    write_text(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n", bom)
    return {"verb": "filter", "action": "remove", "scope": scope, "removed": len(filters) - len(keep),
            "files": [os.path.relpath(path, ws.mirror).replace("\\", "/")]}


def list_(ws, project, page: str | None = None, visual: str | None = None, report: bool = False) -> dict:
    if not (page or visual or report):
        scopes = []
        ddir = os.path.join(project.mirror_report, "definition")
        for cur, _d, fs in os.walk(ddir):
            for f in sorted(fs):
                if f in ("report.json", "page.json", "visual.json"):
                    p = os.path.join(cur, f)
                    d = json.loads(read_text(p)[0])
                    fl = (d.get("filterConfig") or {}).get("filters") or []
                    if fl:
                        scopes.append({"file": os.path.relpath(p, ws.mirror).replace("\\", "/"),
                                       "filters": [{"name": x.get("name"), "field": _label(x), "type": x.get("type"),
                                                    "condition": _summary(x), **({"how": x["howCreated"]} if x.get("howCreated") else {})}
                                                   for x in fl]})
        return {"verb": "filter", "action": "list", "scopes": scopes}
    path, scope = _scope_file(project, page, visual, report)
    d = json.loads(read_text(path)[0])
    fl = (d.get("filterConfig") or {}).get("filters") or []
    return {"verb": "filter", "action": "list", "scope": scope,
            "filters": [{"name": x.get("name"), "field": _label(x), "type": x.get("type"), "condition": _summary(x),
                         **({"how": x["howCreated"]} if x.get("howCreated") else {})} for x in fl]}


def interaction(ws, project, page: str, source: str, target: str, mode: str) -> dict:
    """How `source` affects `target` on a page: filter, highlight, none, or default (Desktop's choice).
    target may be `all` (every other visual on the page)."""
    from .style import targets
    if mode not in (*MODES, "default"):
        raise PbzError("BAD_REQUEST", "mode: filter, highlight, none or default")
    src = targets(project, page, source, None, False)[0]
    page_dir = os.path.dirname(os.path.dirname(os.path.dirname(src["path"])))
    every = [t for t in targets(project, page, None, None, True) if t["id"] != src["id"]]
    tg = every if target.lower() == "all" else targets(project, page, target, None, False)
    if any(t["id"] == src["id"] for t in tg):
        raise PbzError("BAD_REQUEST", "a visual does not interact with itself")
    pj = os.path.join(page_dir, "page.json")
    text, bom = read_text(pj)
    doc = json.loads(text)
    inter = [i for i in doc.get("visualInteractions") or []
             if not (isinstance(i, dict) and i.get("source") == src["id"] and i.get("target") in {t["id"] for t in tg})]
    if mode != "default":
        inter += [{"source": src["id"], "target": t["id"], "type": MODES[mode]} for t in tg]
    if inter:
        doc["visualInteractions"] = inter
    else:
        doc.pop("visualInteractions", None)
    write_text(pj, json.dumps(doc, indent=2, ensure_ascii=False) + "\n", bom)
    return {"verb": "interaction", "page": src["page"], "source": src["id"], "targets": [t["id"] for t in tg],
            "mode": mode, "files": [os.path.relpath(pj, ws.mirror).replace("\\", "/")],
            "next": "pbz validate, then pbz push --reload"}
