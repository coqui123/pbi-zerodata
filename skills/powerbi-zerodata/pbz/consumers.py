"""External consumers of the semantic model: what `pbz unused` cannot see in the local PBIP.

Registered sources (.pbz/consumers.json, managed by `pbz consumers`):
- pbir     thin report folders elsewhere: PBIP exports, Fabric Git folders (PBIR or PBIR-Legacy)
- pbix     downloaded thin reports (.pbix / .pbit): Report/definition or the legacy Report/Layout
- xlsx     Excel: OLAP PivotTables (Analyze in Excel), slicers, timelines, CUBE formulas, DAX query tables
- rdl      paginated reports and shared datasets (.rdl / .rsd): DAX or MDX command text, fields
- model    downstream composite models (*.SemanticModel) chained onto this one by DirectQuery
- queries  query-text exports (Log Analytics, workspace monitoring, traces): DAX / MDX text
- scan     an admin scanner result (JSON): which service reports, paginated reports, composite
           models and tiles consume this model (coverage only; their fields need their definitions)
- manual   objects you know are used elsewhere

Rules:
- Evidence only keeps an object: whatever a consumer references becomes used_external, never removable.
- Missing coverage blocks deletion: a missing, unreadable or opaque consumer (or a scanned consumer
  without a definition) makes the analysis incomplete, so `unused` reports unknown instead of unused.
- Consumer files are read inside this process only. Just identifiers are kept, and only those that
  resolve to this model's objects are ever shown. Cached pivot items, cell values, member keys,
  parameter values, filter and query literals are never stored or printed; unresolved names are counted.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile

from . import lexers, pbir, tmdl
from .common import PbzError, read_text, write_text

REG = (".pbz", "consumers.json")
CACHE = "consumers"
KINDS = ("pbir", "pbix", "xlsx", "rdl", "model", "queries", "scan", "manual")
MAX_TEXT = 512 * 2 ** 20
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
IMPLICIT = re.compile(r"^(?:sum|count|average|min|max|distinct count|median) of (.+)$", re.I)
IMPLICIT_SUFFIX = re.compile(r"^(.+)\.(?:sum|count|average|min|max|distinctcount)$", re.I)
_MDX_CHAIN = re.compile(r"&?\[(?:[^\]]|\]\])+\](?:\s*\.\s*&?\[(?:[^\]]|\]\])+\])*")
_MDX_SEG = re.compile(r"(&?)\[((?:[^\]]|\]\])+)\]")
_XL_STR = re.compile(r'"((?:[^"]|"")*)"')
_DAXISH = re.compile(r"(?i)\b(?:evaluate|define)\b")


# ---------------------------------------------------------------- registry

def _reg_path(ws) -> str:
    return os.path.join(ws.root, *REG)


def load_registry(ws) -> dict:
    try:
        with open(_reg_path(ws), encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError):
        reg = {}
    if not isinstance(reg, dict) or not isinstance(reg.get("consumers"), list):
        reg = {"version": 1, "consumers": []}
    return reg


def _save_registry(ws, reg: dict) -> None:
    write_text(_reg_path(ws), json.dumps(reg, indent=1, ensure_ascii=False) + "\n")


def detect_kind(path: str) -> str:
    p = path.rstrip("\\/")
    low = p.lower()
    if os.path.isdir(p):
        if low.endswith(".report") or os.path.exists(os.path.join(p, "definition.pbir")):
            return "pbir"
        if low.endswith(".semanticmodel") or os.path.exists(os.path.join(p, "definition", "model.tmdl")):
            return "model"
        return "pbir"  # a folder of reports (Fabric Git export): every *.Report inside
    ext = os.path.splitext(low)[1]
    if ext in (".pbix", ".pbit"):
        return "pbix"
    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        return "xlsx"
    if ext in (".rdl", ".rsd"):
        return "rdl"
    if ext == ".json":
        try:
            with open(p, "rb") as f:
                head = f.read(4096).decode("utf-8", "replace")
        except OSError:
            head = ""
        return "scan" if '"workspaces"' in head else "queries"
    if ext in (".dax", ".mdx", ".txt", ".csv", ".tsv", ".kql", ".log", ".trc", ".xml"):
        return "queries"
    raise PbzError("BAD_REQUEST", "unknown consumer type: pass --kind " + "|".join(KINDS))


def add(ws, project, path: str | None = None, kind: str = "auto", label: str | None = None,
        objects: list[str] | None = None, model_names: list[str] | None = None) -> dict:
    names = _mirror_names(project)
    reg = load_registry(ws)
    if objects:
        kind, entry = "manual", {"objects": [o for o in objects if o.strip()]}
        if not label:
            raise PbzError("BAD_REQUEST", "a manual consumer needs --label")
    else:
        if not path:
            raise PbzError("BAD_REQUEST", "give a path (file or folder) or --object for a manual consumer")
        ap = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(ap):
            raise PbzError("NOT_FOUND", "consumer path does not exist")
        mirror = os.path.normcase(os.path.abspath(ws.mirror))
        if os.path.normcase(ap) == mirror or os.path.normcase(ap).startswith(mirror + os.sep):
            raise PbzError("BAD_REQUEST", "register the real consumer, not a file in pbz-workspace")
        kind = detect_kind(ap) if kind in (None, "", "auto") else kind
        if kind not in KINDS or kind == "manual":
            raise PbzError("BAD_REQUEST", "--kind " + "|".join(k for k in KINDS if k != "manual"))
        for e in reg["consumers"]:
            if e.get("path") and os.path.normcase(e["path"]) == os.path.normcase(ap):
                raise PbzError("NAME_TAKEN", f"already registered as {e['id']}")
        entry = {"path": ap}
    ids = [int(e["id"][1:]) for e in reg["consumers"] if re.match(r"^c\d+$", str(e.get("id", "")))]
    entry = {"id": f"c{max(ids, default=0) + 1}", "kind": kind,
             "label": (label or os.path.basename(entry.get("path", "")) or kind)[:120], **entry,
             "added": dt.datetime.now().replace(microsecond=0).isoformat()}
    if model_names:
        entry["model_names"] = model_names
    reg["consumers"].append(entry)
    _save_registry(ws, reg)
    r = evaluate(ws, project, entry, force=True, names=names)
    out = {"verb": "consumers", "action": "add", "id": entry["id"], "kind": kind, "label": entry["label"],
           **_summary(entry, r, None, None)}
    out["next"] = "pbz unused (external usage now counts), pbz consumers show " + entry["id"]
    return out


def remove(ws, cid: str) -> dict:
    """Human only (`pbz admin consumer-remove <id>`): dropping a consumer loosens protection."""
    reg = load_registry(ws)
    keep = [e for e in reg["consumers"] if e.get("id") != cid]
    if len(keep) == len(reg["consumers"]):
        raise PbzError("NOT_FOUND", cid)
    reg["consumers"] = keep
    _save_registry(ws, reg)
    try:
        os.remove(os.path.join(ws.private, CACHE, cid + ".json"))
    except OSError:
        pass
    return {"verb": "admin", "consumer_removed": cid}


# ---------------------------------------------------------------- evaluation (cached by signature)

def _signature(entry: dict) -> str:
    if entry["kind"] == "manual":
        return hashlib.sha256(json.dumps(entry.get("objects", [])).encode("utf-8")).hexdigest()
    p = entry.get("path", "")
    if os.path.isfile(p):
        st = os.stat(p)
        return f"f:{st.st_size}:{st.st_mtime_ns}"
    if os.path.isdir(p):
        h = hashlib.sha256()
        for cur, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d.lower() not in (".pbi", "customvisuals", "staticresources", ".git"))
            for f in sorted(files):
                if f.lower().endswith((".json", ".pbir", ".tmdl", ".platform")):
                    st = os.stat(os.path.join(cur, f))
                    h.update(f"{os.path.relpath(os.path.join(cur, f), p)}:{st.st_size}:{st.st_mtime_ns};".encode("utf-8"))
        return "d:" + h.hexdigest()
    return "missing"


def model_names(model) -> set[str]:
    """Lowercase names a consumer reference may keep: tables, columns, measures, hierarchies, levels."""
    names = {"measures"}
    for t in model.tables.values():
        names.add(t.name.lower())
        names |= {c.lower() for c in t.columns} | {m.lower() for m in t.measures}
        for h, levels in t.hierarchies.items():
            names.add(h.lower())
            names |= {lv.lower() for lv in levels}
    return names


def _mirror_names(project) -> set[str]:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    return model_names(tmdl.load_model(mdef))


def _keep(r: dict, names: set[str]) -> dict | None:
    """Keep a reference only when its names are this model's names; member-like parts are dropped, so
    nothing but model object names is ever cached."""
    k = r["k"]
    if k == "object":
        return r
    t, n, lv = (r.get("t") or "").lower(), (r.get("n") or "").lower(), (r.get("l") or "").lower()
    if k == "mdx":
        if t == "measures":
            im = IMPLICIT.match(n) or IMPLICIT_SUFFIX.match(n)
            return r if n in names or (im and im.group(1).lower() in names) else None
        if t not in names:
            return None
        return r if not n or n in names else dict(r, n="")
    if k == "table":
        return r if t in names else None
    if k == "bare":
        return r if n in names else None
    if k == "hier":
        if t not in names:
            return None
        r = r if n in names else dict(r, n="")
        return r if not lv or lv in names else {x: v for x, v in r.items() if x != "l"}
    if k in ("measure", "col_or_measure"):
        if n not in names:
            return None
        return r if not t or t in names else dict(r, t="")
    return None


def evaluate(ws, project, entry: dict, force: bool = False, names: set[str] | None = None) -> dict:
    names = names if names is not None else _mirror_names(project)
    nh = hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()[:16]
    sig = _signature(entry)
    cpath = os.path.join(ws.private, CACHE, entry["id"] + ".json")
    if not force and sig != "missing":
        try:
            with open(cpath, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("sig") == f"{sig}|{nh}":
                return cached["result"]
        except (OSError, ValueError):
            pass
    if sig == "missing":
        return {"status": "missing", "coverage": "none", "refs": [], "gaps": {}, "binding": {}}
    try:
        res = PARSERS[entry["kind"]](entry, project)
        res.setdefault("status", "ok")
    except Exception as e:  # noqa: BLE001  untrusted file of any shape: unreadable blocks removal, never crashes
        res = {"status": "unreadable", "error": type(e).__name__, "coverage": "none", "refs": [], "gaps": {}, "binding": {}}
    raw = res.get("refs", [])
    kept = [x for x in (_keep(r, names) for r in raw) if x is not None]
    res["dropped"] = len(raw) - len(kept)  # names that match nothing in the model: counted, never kept
    res["refs"] = _dedupe(kept)
    res.setdefault("gaps", {})
    res.setdefault("binding", {})
    res.setdefault("coverage", "field" if res["refs"] else "none")
    res["scanned"] = dt.datetime.now().replace(microsecond=0).isoformat()
    write_text(cpath, json.dumps({"sig": f"{sig}|{nh}", "result": res}, ensure_ascii=False))
    return res


def _dedupe(refs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in refs:
        k = (r.get("k"), (r.get("t") or "").lower(), (r.get("n") or "").lower(), (r.get("l") or "").lower(), r.get("v"))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# ---------------------------------------------------------------- identifier extraction (no values)

def _ref(k: str, t: str = "", n: str = "", via: str = "", level: str = "") -> dict:
    d = {"k": k, "t": t, "n": n, "v": via}
    if level:
        d["l"] = level
    return d


def dax_refs(text: str, via: str) -> list[dict]:
    """'T'[C], T[C], [X] and 'T' identifiers of DAX text; strings, numbers and comments are skipped."""
    out = []
    sig = [t for t in lexers.significant(lexers.lex_dax(text)) if t.kind != "pbztok"]
    i = 0
    while i < len(sig):
        t = sig[i]
        nxt = sig[i + 1] if i + 1 < len(sig) else None
        if nxt is not None and nxt.kind == "bracket" and (t.kind == "table" or (t.kind == "ident" and nxt.start == t.end)):
            out.append(_ref("col_or_measure", t.value, nxt.value, via))
            i += 2
            continue
        if t.kind == "bracket":
            out.append(_ref("bare", "", t.value, via))
        elif t.kind == "table":
            out.append(_ref("table", t.value, "", via))
        i += 1
    return out


def mdx_refs(text: str, via: str) -> list[dict]:
    """[Measures].[X], [Table].[Column] and [Table].[Hierarchy] prefixes of MDX unique names: every
    member segment (.&[key] and later) is cut before anything is kept."""
    out = []
    for m in _MDX_CHAIN.finditer(text):
        names = []
        for amp, seg in _MDX_SEG.findall(m.group(0)):
            if amp:
                break
            names.append(seg.replace("]]", "]"))
            if len(names) == 2:
                break
        if names:
            out.append(_ref("mdx", names[0], names[1] if len(names) > 1 else "", via))
    return out


def _field(f, via: str) -> dict:
    if f.kind == "measure":
        return _ref("measure", f.table, f.name, via)
    if f.kind == "hierarchy":
        return _ref("hier", f.table, f.hierarchy or f.name, via, f.level)
    return _ref("col_or_measure", f.table, f.name, via)


def _json_fields(text: str, via: str, refs: list) -> None:
    root = pbir.parse_json(text)
    for f in pbir.walk_fields(root):
        refs.append(_field(f, via))
    _embedded(pbir.to_py(root), via, refs, 0)


def _embedded(obj, via: str, refs: list, depth: int) -> None:
    """Legacy layouts keep config, filters and queries as JSON inside strings."""
    if isinstance(obj, str):
        s = obj.strip()
        if depth < 6 and len(s) > 1 and s[0] in "{[" and s[-1] in "}]":
            try:
                root = pbir.parse_json(s)
            except pbir.JsonError:
                return
            for f in pbir.walk_fields(root):
                refs.append(_field(f, via))
            _embedded(pbir.to_py(root), via, refs, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            _embedded(v, via, refs, depth)
    elif isinstance(obj, list):
        for v in obj:
            _embedded(v, via, refs, depth)


def _extension_measures(text: str, refs: list) -> None:
    try:
        data = json.loads(text)
    except ValueError:
        return
    for ent in data.get("entities") or []:
        for m in (ent or {}).get("measures") or []:
            if isinstance(m, dict) and isinstance(m.get("expression"), str):
                refs += dax_refs(m["expression"], "report_measure")


def _kv(conn: str, key: str) -> str:
    m = re.search(r"(?i)(?:^|;)\s*" + re.escape(key) + r"\s*=\s*(\"[^\"]*\"|'[^']*'|[^;]*)", conn or "")
    return m.group(1).strip().strip("\"'") if m else ""


def _binding_match(catalogs: list[str], entry: dict, project) -> str:
    names = {n.lower() for n in (entry.get("model_names") or [project.name])}
    cats = {c.lower() for c in catalogs if c}
    if not cats:
        return "unverified"
    return "model name" if cats & names else "other catalog (kept: ids are not names)"


# ---------------------------------------------------------------- parsers

def _parse_pbir(entry: dict, project) -> dict:
    root = entry["path"]
    reports = [root] if (root.lower().rstrip("\\/").endswith(".report") or
                         os.path.exists(os.path.join(root, "definition.pbir"))) else []
    if not reports:
        for cur, dirs, _files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in (".git", ".pbi", "node_modules")]
            for d in list(dirs):
                if d.lower().endswith(".report"):
                    reports.append(os.path.join(cur, d))
                    dirs.remove(d)
    if not reports:
        return {"status": "not_bound", "coverage": "none", "refs": [], "note": "no *.Report folder found"}
    refs, catalogs, by_path, names = [], [], [], []
    for rdir in reports:
        names.append(os.path.basename(rdir.rstrip("\\/"))[:-7])
        for path in pbir.report_files(rdir):
            rel = os.path.relpath(path, os.path.join(rdir, "definition")).replace("\\", "/")
            text = read_text(path)[0]
            via = "visual" if rel.endswith("visual.json") else ("bookmark" if "bookmarks/" in rel else "report")
            _json_fields(text, via, refs)
            if rel == "reportExtensions.json":
                _extension_measures(text, refs)
        legacy = os.path.join(rdir, "report.json")
        if not os.path.isdir(os.path.join(rdir, "definition")) and os.path.exists(legacy):
            _json_fields(read_text(legacy)[0], "legacy_layout", refs)
        try:
            ref = json.loads(read_text(os.path.join(rdir, "definition.pbir"))[0]).get("datasetReference") or {}
        except (OSError, ValueError):
            ref = {}
        if (ref.get("byPath") or {}).get("path"):
            by_path.append(os.path.basename(os.path.normpath(ref["byPath"]["path"].replace("\\", "/"))))
        conn = (ref.get("byConnection") or {}).get("connectionString") or ""
        if conn:
            catalogs.append(_kv(conn, "Initial Catalog"))
    want = (project.name + ".SemanticModel").lower()
    binding = {"reports": len(reports)}
    if by_path:
        binding["match"] = "path" if any(b.lower() == want for b in by_path) else "other local model (kept)"
    elif catalogs:
        binding["match"] = _binding_match(catalogs, entry, project)
    else:
        binding["match"] = "unverified"
    return {"refs": refs, "binding": binding, "report_names": names}


def _parse_pbix(entry: dict, project) -> dict:
    refs: list = []
    binding: dict = {}
    with zipfile.ZipFile(entry["path"]) as z:
        names = z.namelist()
        parts = [n for n in names if n.startswith("Report/definition/") and n.lower().endswith(".json")]
        if parts:
            for n in parts:
                text = z.read(n).decode("utf-8-sig")
                _json_fields(text, "visual" if n.endswith("visual.json") else "report", refs)
                if n.endswith("/reportExtensions.json"):
                    _extension_measures(text, refs)
        elif "Report/Layout" in names:
            raw = z.read("Report/Layout")
            text = raw.decode("utf-16-le" if not raw.startswith(b"\xef\xbb\xbf") else "utf-8-sig").lstrip("﻿")
            _json_fields(text, "legacy_layout", refs)
        else:
            return {"status": "partial", "coverage": "none", "refs": [], "gaps": {"no_report_definition": 1}}
        if "Connections" in names:  # never DataModel: that is the cached data
            conn = json.loads(z.read("Connections").decode("utf-8-sig") or "{}")
            cats = [_kv(c.get("ConnectionString", ""), "Initial Catalog") for c in conn.get("Connections") or []
                    if isinstance(c, dict)]
            binding["match"] = _binding_match(cats, entry, project)
            binding["report_ids"] = sorted({str(a.get("ReportId")) for a in conn.get("RemoteArtifacts") or []
                                            if isinstance(a, dict) and a.get("ReportId")})
        else:
            binding["match"] = "own model (kept)" if "DataModel" in names else "unverified"
    return {"refs": refs, "binding": binding}


def _xml(data: bytes):
    return ET.fromstring(data)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xlsx(entry: dict, project) -> dict:
    refs: list = []
    gaps: dict = {}
    with zipfile.ZipFile(entry["path"]) as z:
        names = set(z.namelist())
        conns: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        if "xl/connections.xml" in names:
            for c in _xml(z.read("xl/connections.xml")).iter(NS + "connection"):
                db = c.find(NS + "dbPr")
                cs = db.get("connection", "") if db is not None else ""
                info = {"olap": "msolap" in cs.lower() or c.find(NS + "olapPr") is not None,
                        "internal": "$embedded$" in cs.lower() or c.get("name") == "ThisWorkbookDataModel",
                        "catalog": _kv(cs, "Initial Catalog")}
                conns[c.get("id", "")] = info
                by_name[(c.get("name") or "").lower()] = info
                cmd = db.get("command", "") if db is not None else ""
                if info["olap"] and not info["internal"] and cmd and _DAXISH.search(cmd):
                    refs += dax_refs(cmd, "query_table")
        external = [c for c in conns.values() if c["olap"] and not c["internal"]]
        caches: dict[str, list] = {}
        for n in sorted(names):
            m = re.match(r"xl/pivotCache/pivotCacheDefinition\d+\.xml$", n)
            if not m:
                continue
            root = _xml(z.read(n))
            src = root.find(NS + "cacheSource")
            conn = conns.get(src.get("connectionId", "") if src is not None else "")
            if conn is None or not conn["olap"] or conn["internal"]:
                continue  # worksheet ranges and the workbook's own data model are not this model
            hiers = []
            for ch in root.iter(NS + "cacheHierarchy"):
                un = ch.get("uniqueName") or ""
                hiers.append(un)
                if ch.find(NS + "fieldsUsage") is not None:
                    refs += mdx_refs(un, "pivot")
            for cf in root.iter(NS + "cacheField"):  # placed levels and measures; sharedItems never read
                refs += mdx_refs(cf.get("name") or "", "pivot")
            for cm in root.iter(NS + "calculatedMember"):
                refs += mdx_refs(cm.get("mdx") or "", "pivot_calculation")
            for s in root.iter(NS + "set"):
                refs += mdx_refs(s.get("formula") or "", "pivot_set")
            caches["xl/pivotCache/" + os.path.basename(n)] = hiers
        for n in sorted(names):
            if not re.match(r"xl/pivotTables/pivotTable\d+\.xml$", n):
                continue
            rels = f"xl/pivotTables/_rels/{os.path.basename(n)}.rels"
            target = None
            if rels in names:
                for r in _xml(z.read(rels)):
                    t = (r.get("Target") or "").replace("\\", "/")
                    if "pivotCacheDefinition" in t:
                        target = "xl/pivotCache/" + os.path.basename(t)
            hiers = caches.get(target or "")
            if hiers is None:
                continue
            root = _xml(z.read(n))
            idx = set()
            for tag, attr in (("rowHierarchyUsage", "hierarchyUsage"), ("colHierarchyUsage", "hierarchyUsage"),
                              ("pageField", "hier")):
                for e in root.iter(NS + tag):
                    try:
                        idx.add(int(e.get(attr, "-1")))
                    except ValueError:
                        pass
            for i in sorted(x for x in idx if 0 <= x < len(hiers)):
                refs += mdx_refs(hiers[i], "pivot")
        if external:
            for n in sorted(names):
                low = n.lower()
                if low.startswith(("xl/slicercaches/", "xl/timelinecaches/")) and low.endswith(".xml"):
                    root = _xml(z.read(n))
                    if root.get("sourceName"):
                        refs += mdx_refs(root.get("sourceName"), "slicer")
                    for e in root.iter():
                        if _local(e.tag) == "level" and e.get("uniqueName"):
                            refs += mdx_refs(e.get("uniqueName"), "slicer")
        formulas = [n for n in sorted(names) if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
        for n in formulas:  # formula text only: cell values and shared strings are never kept
            with z.open(n) as f:
                for _ev, el in ET.iterparse(f, events=("end",)):
                    if el.tag == NS + "f" and el.text and "CUBE" in el.text.upper():
                        _cube_formula(el.text, by_name, refs)
                    if el.tag in (NS + "c", NS + "row"):
                        el.clear()
        if "xl/workbook.xml" in names:
            for dn in _xml(z.read("xl/workbook.xml")).iter(NS + "definedName"):
                if dn.text and "CUBE" in dn.text.upper():
                    _cube_formula(dn.text, by_name, refs)
    binding = {"match": _binding_match([c["catalog"] for c in external], entry, project) if external else "no external model connection"}
    status = "ok" if external else "not_bound"
    return {"status": status, "refs": refs, "gaps": gaps, "binding": binding}


def _cube_formula(text: str, by_name: dict, refs: list) -> None:
    for call in re.finditer(r"(?i)\bCUBE[A-Z]+\s*\(", text):
        rest = text[call.end():]
        lits = _XL_STR.findall(rest[:4000])
        if not lits:
            continue
        conn = by_name.get(lits[0].replace('""', '"').lower())
        if conn is not None and conn["internal"]:
            continue
        for lit in lits[1:]:
            refs += mdx_refs(lit.replace('""', '"'), "cube_formula")


def _parse_rdl(entry: dict, project) -> dict:
    refs: list = []
    gaps: dict = {}
    root = ET.parse(entry["path"]).getroot()
    catalogs, providers = [], []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "DataProvider" and el.text:
            providers.append(el.text.strip())
        elif tag == "ConnectString" and el.text:
            catalogs.append(_kv(el.text, "Initial Catalog"))
        elif tag == "CommandText" and el.text:
            q = el.text.strip()
            if q.startswith("="):
                gaps["expression_query"] = gaps.get("expression_query", 0) + 1
            else:
                refs += dax_refs(q, "rdl_query") + mdx_refs(q, "rdl_query")
        elif tag == "DataField" and el.text:
            t = el.text
            for un in re.findall(r'UniqueName="([^"]+)"', t):
                refs += mdx_refs(un, "rdl_field")
            if "<" not in t:
                refs += dax_refs(t, "rdl_field")
    binding = {"match": _binding_match(catalogs, entry, project), "providers": sorted(set(providers))[:5]}
    return {"status": "partial" if gaps else "ok", "refs": refs, "gaps": gaps, "binding": binding,
            "coverage": "partial" if gaps else None}


def _parse_model(entry: dict, project) -> dict:
    p = entry["path"]
    mdef = os.path.join(p, "definition") if os.path.isdir(os.path.join(p, "definition")) else p
    m = tmdl.load_model(mdef)
    refs: list = []
    upstream = []
    for t in m.tables.values():
        for node in t.partition_nodes:
            ent = tmdl.unquote(node.props.get("entityName", "").strip()) if node.props.get("entityName") else ""
            if node.decl_value.strip().lower() != "entity" or not ent:
                continue
            src = tmdl.unquote(node.props.get("expressionSource", "").strip())
            expr = m.expressions.get(src)
            text = m.text_of(expr.expr) if expr is not None and expr.expr is not None else ""
            if text and "analysisservices.database" not in text.lower():
                continue  # Direct Lake or another source, not a semantic model
            upstream.append(ent)
            refs.append(_ref("table", ent, "", "composite_table"))
            for c in t.columns.values():
                if not c.calculated:
                    refs.append(_ref("col_or_measure", ent, c.source_column or c.name, "composite_column"))
            for mm in t.measures.values():
                refs.append(_ref("bare", "", mm.name, "composite_measure"))
    if not upstream:
        return {"status": "not_bound", "coverage": "none", "refs": [], "binding": {"match": "no DirectQuery to a semantic model"}}
    for t in m.tables.values():
        for mm in t.measures.values():
            refs += dax_refs(mm.expression, "composite_dax")
    return {"refs": refs, "binding": {"match": "composite", "tables": len(set(upstream))}}


def _read_any(path: str) -> str:
    size = os.path.getsize(path)
    if size > MAX_TEXT:
        raise ValueError("too large")
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def _parse_queries(entry: dict, project) -> dict:
    p = entry["path"]
    text = _read_any(p)
    refs: list = []
    cells: list[str]
    low = p.lower()
    if low.endswith((".csv", ".tsv")):
        cells = [c for row in csv.reader(io.StringIO(text), delimiter="\t" if low.endswith(".tsv") else ",") for c in row]
    elif low.endswith(".json"):
        cells = []

        def walk(o):
            if isinstance(o, str):
                cells.append(o)
            elif isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(json.loads(text))
    else:
        cells = [text]
    n = 0
    for c in cells:
        if "[" not in c:
            continue
        n += 1
        refs += dax_refs(c, "query_text") + mdx_refs(c, "query_text")
    return {"refs": refs, "binding": {"match": "unverified", "texts": n}, "coverage": "observed"}


def _parse_scan(entry: dict, project, registry: dict | None = None) -> dict:
    data = json.loads(_read_any(entry["path"]))
    names = {n.lower() for n in (entry.get("model_names") or [project.name])}
    ids, gaps = set(), {}
    workspaces = data.get("workspaces") or []
    for wsp in workspaces:
        for ds in wsp.get("datasets") or []:
            if isinstance(ds, dict) and str(ds.get("name", "")).lower() in names and ds.get("id"):
                ids.add(str(ds["id"]).lower())
    if not ids:
        return {"status": "not_bound", "coverage": "none", "refs": [], "binding": {"match": "model not in scan"}}
    reports = []
    for wsp in workspaces:
        for r in wsp.get("reports") or []:
            if isinstance(r, dict) and str(r.get("datasetId", "")).lower() in ids:
                kind = "paginated_report" if str(r.get("reportType", "")).lower().startswith("paginated") else "service_report"
                reports.append({"id": str(r.get("id", "")).lower(), "name": str(r.get("name", "")).lower(), "kind": kind})
        for ds in wsp.get("datasets") or []:
            ups = [str(u.get("targetDatasetId", u.get("datasetId", ""))).lower() for u in ds.get("upstreamDatasets") or []
                   if isinstance(u, dict)]
            if ids & set(ups):
                reports.append({"id": str(ds.get("id", "")).lower(), "name": str(ds.get("name", "")).lower(),
                                "kind": "composite_model"})
        for d in wsp.get("dashboards") or []:
            for tile in d.get("tiles") or []:
                if isinstance(tile, dict) and str(tile.get("datasetId", "")).lower() in ids and not tile.get("reportId"):
                    reports.append({"id": str(tile.get("id", "")).lower(), "name": "", "kind": "dataset_tile"})
    return {"refs": [], "coverage": "consumers", "binding": {"match": "scan", "datasets": len(ids)},
            "known": reports, "gaps": gaps}


def _parse_manual(entry: dict, project) -> dict:
    return {"refs": [_ref("object", "", o, "declared") for o in entry.get("objects") or []],
            "binding": {"match": "declared"}}


PARSERS = {"pbir": _parse_pbir, "pbix": _parse_pbix, "xlsx": _parse_xlsx, "rdl": _parse_rdl,
           "model": _parse_model, "queries": _parse_queries, "scan": _parse_scan, "manual": _parse_manual}


# ---------------------------------------------------------------- resolution against this model

def _resolve(g, res, model, r: dict) -> list[tuple]:
    from .usage import key
    k, t, n = r.get("k"), r.get("t") or "", r.get("n") or ""
    out: list[tuple] = []

    def measure(name):
        hit = res.measure(name)
        return [key("measure", hit[0], hit[1])] if hit else []

    def columns_named(name):
        return [key("column", tab, res.column(tab, name)) for tab in res.by_column.get(name.lower(), [])]

    if k == "measure":
        out = measure(n)
    elif k == "col_or_measure":
        tab = res.table(t) if t else None
        col = res.column(tab, n) if tab else None
        if col:
            out = [key("column", tab, col)]
        else:
            out = measure(n) or ([key("hierarchy", tab, h) for h in model.tables[tab].hierarchies if h.lower() == n.lower()]
                                 if tab and tab in model.tables else [])
    elif k == "hier":
        tab = res.table(t)
        if tab and tab in model.tables:
            hs = {h.lower(): h for h in model.tables[tab].hierarchies}
            h = hs.get(n.lower())
            if h:
                out.append(key("hierarchy", tab, h))
                col = model.tables[tab].hierarchies[h].get(r.get("l", ""), "")
                if col and res.column(tab, col):
                    out.append(key("column", tab, res.column(tab, col)))
            elif res.column(tab, n):
                out.append(key("column", tab, res.column(tab, n)))
    elif k == "bare":
        out = measure(n) + columns_named(n)
    elif k == "table":
        tab = res.table(t)
        out = [key("table", "", tab)] if tab else []
    elif k == "mdx":
        if t.lower() == "measures":
            out = measure(n)
            if not out and n:
                im = IMPLICIT.match(n) or IMPLICIT_SUFFIX.match(n)
                if im:
                    out = columns_named(im.group(1))
        else:
            tab = res.table(t)
            if tab and not n:
                out = [key("table", "", tab)]
            elif tab:
                col = res.column(tab, n)
                hs = {h.lower(): h for h in model.tables[tab].hierarchies} if tab in model.tables else {}
                if col:
                    out = [key("column", tab, col)]
                elif n.lower() in hs:
                    out = [key("hierarchy", tab, hs[n.lower()])]
    elif k == "object":
        out = g.find(n)
    return [x for x in out if x in g.objects]


def apply(g, ws, project, model, res) -> None:
    """Add external usage evidence to the usage graph (before states are computed)."""
    from .usage import Usage
    reg = load_registry(ws)
    if not reg["consumers"]:
        return
    names = model_names(model)
    results = {e["id"]: evaluate(ws, project, e, names=names) for e in reg["consumers"]}
    covered_ids, covered_names = set(), set()
    for e in reg["consumers"]:
        r = results[e["id"]]
        covered_ids |= {x.lower() for x in (r.get("binding") or {}).get("report_ids", [])}
        covered_names |= {x.lower() for x in r.get("report_names", [])}
    for e in reg["consumers"]:
        r = results[e["id"]]
        hits, unresolved, seen = set(), 0, set()
        for ref in r.get("refs", []):
            keys = _resolve(g, res, model, ref)
            if not keys:
                unresolved += 1
            for k in keys:
                hits.add(k)
                if (k, ref.get("v")) not in seen:
                    seen.add((k, ref.get("v")))
                    g.use_external(k, Usage(e["id"], "", e.get("label", ""), ref.get("v", ""), e["kind"]))
        gaps = dict(r.get("gaps") or {})
        for item in r.get("known") or []:  # scanner: consumers whose definitions are not registered
            if item["id"] in covered_ids or (item["name"] and item["name"] in covered_names):
                continue
            gaps[item["kind"]] = gaps.get(item["kind"], 0) + 1
        rec = _summary(e, r, len(hits), unresolved, gaps)
        g.consumers.append(rec)
        if r.get("status") in ("missing", "unreadable") or gaps:
            g.incomplete = True
            g.warnings.append({"code": "CONSUMER_" + ("GAPS" if gaps and r.get("status") not in ("missing", "unreadable")
                                                      else str(r.get("status")).upper()),
                               "consumer": e["id"], "kind": e["kind"], **({"gaps": gaps} if gaps else {})})


def _summary(e: dict, r: dict, objects: int | None, unresolved: int | None, gaps: dict | None = None) -> dict:
    out = {"id": e["id"], "kind": e["kind"], "label": e.get("label", ""), "status": r.get("status", "ok"),
           "coverage": r.get("coverage", "none"), "binding": (r.get("binding") or {}).get("match", "unverified"),
           "refs": len(r.get("refs", []))}
    if objects is not None:
        out["objects"] = objects
    if unresolved or r.get("dropped"):
        # counted only: names that match nothing in this model are never kept or shown
        out["unresolved"] = (unresolved or 0) + (r.get("dropped") or 0)
    g = gaps if gaps is not None else r.get("gaps")
    if g:
        out["gaps"] = g
    if r.get("known") is not None:
        out["consumers_in_scan"] = len(r["known"])
    if r.get("error"):
        out["error"] = r["error"]
    if r.get("scanned"):
        out["scanned"] = r["scanned"]
    return out


# ---------------------------------------------------------------- verbs

def _graph(ws, project):
    from . import usage
    return usage.build(ws, project)


def list_(ws, project) -> dict:
    reg = load_registry(ws)
    if not reg["consumers"]:
        return {"verb": "consumers", "action": "list", "consumers": [],
                "note": "none registered: `pbz unused` sees only local reports. Add thin reports, Excel files, "
                        "paginated reports, composite models or query exports with `pbz consumers add <path>`"}
    g = _graph(ws, project)
    return {"verb": "consumers", "action": "list", "consumers": g.consumers,
            "coverage": "incomplete" if any(w.get("code", "").startswith("CONSUMER_") for w in g.warnings) else "complete"}


def scan(ws, project) -> dict:
    names = _mirror_names(project)
    for e in load_registry(ws)["consumers"]:
        evaluate(ws, project, e, force=True, names=names)
    out = list_(ws, project)
    out["action"] = "scan"
    return out


def show(ws, project, cid: str) -> dict:
    reg = load_registry(ws)
    if not any(e.get("id") == cid for e in reg["consumers"]):
        raise PbzError("NOT_FOUND", cid)
    g = _graph(ws, project)
    rec = next(c for c in g.consumers if c["id"] == cid)
    objs: dict[tuple, set] = {}
    for k, uses in g.ext_usages.items():
        for u in uses:
            if u.report == cid:
                objs.setdefault(k, set()).add(u.via)
    rows = sorted(({"object": g.label(k), "kind": g.objects[k].kind, "via": sorted(v)} for k, v in objs.items()),
                  key=lambda r: (r["kind"], r["object"].lower()))
    return {"verb": "consumers", "action": "show", **rec, "uses": rows[:500]}


GUIDISH = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SKIP_DIRS = {".git", "node_modules", ".pbi", "pbz-workspace", ".pbz", "customvisuals", "staticresources",
             "$recycle.bin", "appdata"}


def _binding_only(path: str, kind: str) -> tuple[list[str], str]:
    """(catalog names the consumer connects to, note) from connection parts only: never data parts."""
    if kind == "xlsx":
        with zipfile.ZipFile(path) as z:
            if "xl/connections.xml" not in z.namelist():
                return [], "no external model connection"
            cats = []
            for c in _xml(z.read("xl/connections.xml")).iter(NS + "connection"):
                db = c.find(NS + "dbPr")
                cs = db.get("connection", "") if db is not None else ""
                olap = "msolap" in cs.lower() or c.find(NS + "olapPr") is not None
                if olap and "$embedded$" not in cs.lower() and c.get("name") != "ThisWorkbookDataModel":
                    cats.append(_kv(cs, "Initial Catalog"))
            return cats, "" if cats else "no external model connection"
    if kind == "pbix":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "Connections" not in names:
                return [], "own model" if "DataModel" in names else "no connection"
            conn = json.loads(z.read("Connections").decode("utf-8-sig") or "{}")
            return [_kv(c.get("ConnectionString", ""), "Initial Catalog") for c in conn.get("Connections") or []
                    if isinstance(c, dict)], ""
    if kind == "rdl":
        cats = [_kv(el.text, "Initial Catalog") for el in ET.parse(path).getroot().iter()
                if _local(el.tag) == "ConnectString" and el.text]
        return cats, "" if cats else "no connection string (shared data source)"
    if kind == "pbir":
        ref = json.loads(read_text(os.path.join(path, "definition.pbir"))[0]).get("datasetReference") or {}
        p = (ref.get("byPath") or {}).get("path") or ""
        if p:
            b = os.path.basename(os.path.normpath(p.replace("\\", "/")))
            return [b[:-len(".SemanticModel")] if b.lower().endswith(".semanticmodel") else b], "by path"
        return [_kv((ref.get("byConnection") or {}).get("connectionString") or "", "Initial Catalog")], ""
    if kind == "model":
        mdef = os.path.join(path, "definition")
        m = tmdl.load_model(mdef)
        cats = []
        for node in m.expressions.values():
            text = m.text_of(node.expr) if node.expr is not None else ""
            hit = re.search(r'(?i)AnalysisServices\.Database\(\s*"[^"]*"\s*,\s*"([^"]*)"', text)
            if hit:
                cats.append(hit.group(1))
        return cats, "" if cats else "no DirectQuery to a semantic model"
    return [], "unsupported"


def discover(ws, project, folder: str, add_matches: bool = False, model_names: list[str] | None = None,
             depth: int = 6, limit: int = 20000) -> dict:
    """Find files and folders under `folder` that connect to this model, reading connection parts only
    (Excel connections.xml, PBIX Connections, RDL connect strings, definition.pbir, composite model
    expressions). With add_matches, register every one bound to this model by name."""
    base = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(base):
        raise PbzError("NOT_FOUND", "folder does not exist")
    want = {n.lower() for n in (model_names or [project.name])}
    reg = load_registry(ws)
    known = {os.path.normcase(e["path"]) for e in reg["consumers"] if e.get("path")}
    own = {os.path.normcase(os.path.abspath(p)) for p in (project.report_dir, project.model_dir, ws.mirror)}
    found, seen = [], 0
    root_depth = base.rstrip("\\/").count(os.sep)
    for cur, dirs, files in os.walk(base):
        if cur.count(os.sep) - root_depth >= depth:
            dirs[:] = []
        keep = []
        for d in sorted(dirs):
            full = os.path.join(cur, d)
            low = d.lower()
            if low in SKIP_DIRS or low.startswith(".") or os.path.normcase(full) in own:
                continue
            if low.endswith(".report") and os.path.exists(os.path.join(full, "definition.pbir")):
                found.append((full, "pbir"))
            elif low.endswith(".semanticmodel") and os.path.isdir(os.path.join(full, "definition")):
                found.append((full, "model"))
            else:
                keep.append(d)
        dirs[:] = keep
        for f in sorted(files):
            seen += 1
            if seen > limit:
                break
            ext = os.path.splitext(f)[1].lower()
            kind = {".xlsx": "xlsx", ".xlsm": "xlsx", ".pbix": "pbix", ".pbit": "pbix", ".rdl": "rdl", ".rsd": "rdl"}.get(ext)
            if kind and not f.startswith("~$"):
                found.append((os.path.join(cur, f), kind))
        if seen > limit:
            break
    out_rows, added = [], []
    for path, kind in found:
        try:
            cats, note = _binding_only(path, kind)
        except Exception as e:  # noqa: BLE001  untrusted file: report the class only
            cats, note = [], f"unreadable ({type(e).__name__})"
        cats = [c for c in cats if c]
        if cats and {c.lower() for c in cats} & want:
            match = "model name"
        elif cats and all(GUIDISH.match(c) for c in cats):
            match = "dataset id (unverified)"
        elif cats:
            match = "other model"
        else:
            match = note or "no connection"
        row = {"path": os.path.relpath(path, base).replace("\\", "/"), "kind": kind, "match": match}
        if os.path.normcase(path) in known:
            row["registered"] = True
        out_rows.append(row)
        if add_matches and match == "model name" and os.path.normcase(path) not in known:
            r = add(ws, project, path, kind, model_names=model_names)
            added.append(r["id"])
            row["registered"] = r["id"]
    rank = {"model name": 0, "dataset id (unverified)": 1}
    out_rows.sort(key=lambda r: (rank.get(r["match"], 2), r["path"].lower()))
    out = {"verb": "consumers", "action": "discover", "folder": base, "scanned_files": min(seen, limit),
           "candidates": [r for r in out_rows if r["match"] in rank][:500],
           "other": len([r for r in out_rows if r["match"] not in rank])}
    if seen > limit:
        out["note"] = f"stopped after {limit} files: narrow the folder"
    if added:
        out["added"] = added
    elif any(r["match"] == "model name" and not r.get("registered") for r in out_rows):
        out["next"] = "pbz consumers discover <folder> --add registers the model-name matches"
    if any(r["match"].startswith("dataset id") for r in out_rows):
        out["unverified"] = ("files bound by dataset id: if they use this model, register them with "
                             "`pbz consumers add <path>` (ids are not names)")
    return out


def users_of(ws, project, kind: str, table: str, name: str) -> list[str]:
    """Consumer ids that reference one object directly (for rename warnings)."""
    if not load_registry(ws)["consumers"]:
        return []
    from .usage import key
    g = _graph(ws, project)
    k = key(kind, table if kind != "table" else "", name)
    return sorted({u.report for u in g.ext_usages.get(k, [])})
