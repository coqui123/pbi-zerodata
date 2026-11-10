"""`pbz unused` / `pbz lineage`: object usage states and dependency trees from the usage graph.
`pbz unused --remove` deletes unused objects from the mirror (then `pbz diff` / `pbz push` as usual) and
keeps a restore point in pbz-workspace/.pbz-trash/. Names only, never values."""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from . import lexers, pbir, tmdl, usage
from .common import PbzError, commit_bytes, file_sha, read_text, sha
from .usage import key

KIND_ORDER = ["table", "column", "measure", "hierarchy", "calc_item", "relationship", "m_query", "function",
              "report_measure", "role"]
TRASH = ".pbz-trash"
_STAMP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def _index_summary(ws) -> dict:
    try:
        with open(os.path.join(ws.private, "index-summary.json"), encoding="utf-8") as f:
            return {t.get("table"): t for t in json.load(f).get("tables", [])}
    except (OSError, ValueError):
        return {}


def translated(project) -> set[tuple]:
    """Keys of objects with translations in real culture files the mirror does not hold yet (mirrors
    pulled before cultures were mirrored): those removals stay manual. Names only."""
    from .governance import cultures_mirrored
    if cultures_mirrored(project):
        return set()  # translations are pruned in the mirror together with the object
    out = set()
    cdir = os.path.join(project.model_dir, "definition", "cultures")
    for f in sorted(os.listdir(cdir)) if os.path.isdir(cdir) else []:
        if not f.lower().endswith(".tmdl"):
            continue
        try:
            doc = tmdl.parse(f, read_text(os.path.join(cdir, f))[0])
        except (OSError, UnicodeDecodeError):
            continue
        for n in doc.nodes:
            if n.kind in ("measure", "column", "hierarchy") and n.parent is not None and n.parent.kind == "table":
                out.add(key(n.kind, n.parent.name, n.name))
            elif n.kind == "table" and n.parent is not None and n.parent.kind == "model":
                out.add(key("table", "", n.name))
    return out


def _removal(g, k, model) -> tuple[str, str]:
    """('auto' | 'manual', reason) for deleting an unused object."""
    o = g.objects[k]
    if k in getattr(g, "translated", ()):
        return "manual", "has translations in cultures/: remove them in Desktop first"
    if o.kind == "column":
        t = model.table(o.table)
        if t is not None and t.calculated:
            return "manual", "column of a calculated table: edit the table expression"
        if o.calculated:
            return "auto", ""
        if t is not None and any(ch.kind == "refreshPolicy" for ch in (t.node.children if t.node else [])):
            return "manual", "table has an incremental refresh policy"
        return "auto", ""
    if o.kind == "m_query" and o.extra.get("parameter"):
        return "manual", "parameter: deployment pipelines or the service may set it"
    if o.kind == "calc_item":
        return "manual", "calculation item: changes the calculation group"
    if o.kind == "role":
        return "manual", "security role"
    return "auto", ""


def report(ws, project, extra_reports=(), include_all: bool = False, impact: bool = False,
           kinds: list[str] | None = None) -> dict:
    g = usage.build(ws, project, extra_reports)
    g.translated = translated(project)
    summ = _index_summary(ws) if impact else {}
    summary: dict = {}
    objects = []
    auto_date = 0
    for k, o in g.objects.items():
        if kinds and o.kind not in kinds:
            continue
        if o.private:
            auto_date += o.kind == "table"
            if not include_all:
                continue
        st = g.state[k]
        summary.setdefault(o.kind, {s: 0 for s in usage.STATES})[st] += 1
        if st == "used" and not include_all:
            continue
        rec = {"kind": o.kind, "table": o.table, "name": o.name, "state": st}
        if o.kind == "relationship":
            rec["name"] = o.label()
            rec["active"] = o.extra.get("active", True)
        if o.hidden:
            rec["hidden"] = True
        if st == "locked":
            rec["locks"] = g.locks.get(k) or [f"needed by {g.label(e.src)}" for e in g.inc.get(k, [])
                                              if g.state.get(e.src) in ("locked", "used")][:3] or ["needed by a locked object"]
        if st == "used_by_unused":
            rec["blocked_by"] = [g.label(b) for b in g.blocked_by.get(k, [])][:10]
        if st == "used_external":
            direct = sorted({u.report for u in g.ext_usages.get(k, [])})
            if direct:
                rec["consumers"] = direct[:10]
            else:
                rec["via"] = "dependency of an externally used object"
        if st == "unused":
            how, why = _removal(g, k, g.model)
            rec["remove"] = how
            if why:
                rec["note"] = why
        if st == "used" and include_all:
            rec["used_in"] = len(g.usages.get(k, []))
        if impact and o.kind == "column":
            t = summ.get(o.table, {})
            col = next((c for c in t.get("columns", []) if c.get("column") == o.name), {})
            if col.get("card_bucket") or t.get("rows_bucket"):
                rec["impact"] = {"distinct": col.get("card_bucket", "unknown"), "rows": t.get("rows_bucket", "unknown")}
        objects.append(rec)
    objects.sort(key=lambda r: (usage.STATES.index(r["state"]), KIND_ORDER.index(r["kind"]) if r["kind"] in KIND_ORDER else 99,
                                r["table"].lower(), r["name"].lower()))
    # bookmarks stay usable from the Bookmarks pane even when no button or navigator points at them
    rep = [dict(o, state="used" if o.get("referenced") else ("unreferenced" if o["kind"] == "bookmark" else "unused"))
           for o in g.report_objects if o["kind"] in ("bookmark", "custom_visual")]
    rep = [r for r in rep if include_all or r["state"] != "used"]
    for r in rep:
        r.pop("referenced", None)
        r.pop("file", None)
    issues = [{"code": "UNRESOLVED_REF", "object": o.label(), "refs": o.issues[:10]}
              for o in g.objects.values() if o.issues]
    scope = {"reports": g.reports, "unseen_consumers": usage.UNSEEN}
    if g.consumers:
        scope["consumers"] = g.consumers
        scope["external_coverage"] = "incomplete" if any(
            w.get("code", "").startswith("CONSUMER_") for w in g.warnings) else "registered consumers complete"
        scope["unseen_consumers"] = ["consumers not registered with `pbz consumers add`"] + usage.UNSEEN
    else:
        scope["external_coverage"] = "none registered (`pbz consumers add <path>`)"
    out = {"verb": "unused", "project": project.name, "scope": scope,
           "analysis": "incomplete" if g.incomplete else "complete",
           "summary": {k: summary[k] for k in KIND_ORDER if k in summary},
           "objects": objects, "report_objects": rep}
    if auto_date:
        out["auto_date_tables"] = auto_date
    if not g.reports:
        out["note"] = "no report bound to this model by path; pass --report <folder> for thin reports"
    if issues or g.warnings:
        out["warnings"] = (issues + g.warnings)[:200]
    return out


# ---------------------------------------------------------------- lineage


def lineage(ws, project, target: str, direction: str = "down", depth: int = 6, extra_reports=()) -> dict:
    g = usage.build(ws, project, extra_reports)
    hits = g.find(target)
    if not hits:
        raise PbzError("NOT_FOUND", target)
    trees = []
    for k in hits[:3]:
        trees.append(_tree(g, k, direction, depth, set()))
    return {"verb": "lineage", "project": project.name, "target": target, "direction": direction,
            "state": {g.label(k): g.state[k] for k in hits[:3]}, "trees": trees}


def what_if(ws, project) -> dict:
    """Share bands of unused columns from the saved memory document. No byte totals."""
    from . import memory
    doc = memory.load(ws, project)
    if not doc:
        return {"verb": "unused", "action": "what-if", "project": project.name,
                "status": "UNKNOWN", "reason": "NO_BANDS"}
    bands = {}
    for table in doc.get("tables") or []:
        tname = table.get("name")
        if not isinstance(tname, str):
            continue
        for col in table.get("columns") or []:
            cname = col.get("name")
            share = col.get("share_band")
            if isinstance(cname, str) and share in memory.SHARE_BANDS:
                bands[(tname.lower(), cname.lower())] = share
    g = usage.build(ws, project)
    columns = []
    for key, obj in g.objects.items():
        if obj.kind != "column" or g.state.get(key) != "unused":
            continue
        table = g.model.table(obj.table) if g.model is not None else None
        if table is not None and table.private:
            continue
        rec = {"object": obj.label(), "table": obj.table}
        share = bands.get((obj.table.lower(), obj.name.lower()))
        if share:
            rec["share_band"] = share
            if share == ">=20%":
                rec["impact"] = "MOST"
        columns.append(rec)
    columns.sort(key=lambda row: (row["object"].lower(), row["object"]))
    return {"verb": "unused", "action": "what-if", "project": project.name, "status": "OK",
            "columns": columns[:500]}


def _tree(g, k, direction, depth, path) -> dict:
    o = g.objects[k]
    node = {"object": o.label(), "kind": o.kind}
    if o.kind in ("column", "measure", "hierarchy", "calc_item") and o.table:
        node["table"] = o.table
    if k in path:
        node["cycle"] = True
        return node
    if depth <= 0:
        node["truncated"] = True
        return node
    path = path | {k}
    kids = []
    if direction == "up":
        for e in g.out.get(k, []):
            if e.via == "home":
                continue
            kids.append(dict(_tree(g, e.dst, direction, depth - 1, path), via=e.via))
        if o.kind == "column" and o.extra.get("source_column"):
            node["source_column"] = o.extra["source_column"]
    else:
        for e in g.inc.get(k, []):
            if e.via in ("home", "calc_item"):
                continue
            kids.append(dict(_tree(g, e.src, direction, depth - 1, path), via=e.via))
        uses = g.usages.get(k, [])
        if uses:
            by_page: dict = {}
            for u in uses:
                by_page.setdefault(f"{u.report} | {u.page or '(report level)'}", set()).add(
                    f"{u.visual or '-'} ({u.via})")
            node["used_in"] = {p: sorted(v)[:20] for p, v in sorted(by_page.items())}
        ext = g.ext_usages.get(k, [])
        if ext:
            by_consumer: dict = {}
            for u in ext:
                by_consumer.setdefault(f"{u.report} ({u.file})", set()).add(u.via)
            node["used_externally"] = {c: sorted(v) for c, v in sorted(by_consumer.items())}
        if k in g.locks:
            node["locks"] = g.locks[k]
    if kids:
        node["children"] = kids
    return node


# ---------------------------------------------------------------- remove


def remove(ws, project, extra_reports=(), m_step: str = "remove", cascade: bool = False,
           only: list[str] | None = None, dry_run: bool = False) -> dict:
    """Delete unused objects from the mirror. Each cascade round is one commit_bytes."""
    if m_step not in ("remove", "keep", "none"):
        raise PbzError("BAD_REQUEST", "--m-step must be remove, keep or none")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    tdir = os.path.join(ws.mirror, TRASH, stamp)
    if not dry_run and os.path.lexists(tdir):
        raise PbzError("BAD_REQUEST", "restore point already exists")
    originals: dict[str, tuple[str | None, bytes]] = {}
    manifest_files: dict[str, dict] = {}
    backed: set[str] = set()
    touched: list[str] = []
    removed, skipped = [], []
    committed = 0
    while True:
        g = usage.build(ws, project, extra_reports)
        g.translated = translated(project)
        if g.incomplete:
            if committed:
                return _partial(project, removed, skipped, committed, stamp)
            raise PbzError("INCOMPLETE", "a report file or a registered consumer could not be read or fully "
                                         "covered (see `pbz consumers list`); nothing removed")
        todo = []
        for k, o in g.objects.items():
            if g.state[k] != "unused" or o.private or (only and o.kind not in only):
                continue
            how, why = _removal(g, k, g.model)
            label = f"{o.kind} {o.label()}" + (f" ({o.table})" if o.kind == "measure" else "")
            if how != "auto":
                if label not in {s["object"] for s in skipped}:
                    skipped.append({"object": label, "reason": why})
                continue
            todo.append(k)
        rep = [r for r in g.report_objects if not r.get("referenced") and (
            (r["kind"] == "custom_visual" and (not only or "custom_visual" in only)) or
            (r["kind"] == "bookmark" and only and "bookmark" in only))]
        if committed and rep:
            rep = []  # report objects go in the first round only
        if not todo and not rep:
            break
        changes = _plan(ws, project, g, todo, rep, m_step, skipped)
        if not changes:
            break
        whole = {g.objects[k].name.lower() for k in todo if k[0] == "table"}
        labels = [f"{g.objects[k].kind} {g.objects[k].label()}" for k in todo
                  if k[0] == "table" or g.objects[k].table.lower() not in whole] + \
                 [f"{r['kind']} {r['id']}" for r in rep]
        for rel in changes:
            if rel not in touched:
                touched.append(rel)
        if dry_run:
            removed += labels
            committed += 1
            break
        ops = _round_ops(ws, tdir, stamp, changes, originals, manifest_files, backed, removed + labels)
        try:
            commit_bytes(ops)
        except PbzError as e:
            if e.code == "FAILED" and committed:
                return _partial(project, removed, skipped, committed, stamp)
            raise
        removed += labels
        committed += 1
        if not cascade:
            break
    if not removed:
        return {"verb": "unused-remove", "project": project.name, "result": "NOTHING_TO_REMOVE", "skipped": skipped}
    out = {"verb": "unused-remove", "project": project.name, "result": "DRY_RUN" if dry_run else "REMOVED",
           "removed": removed, "skipped": skipped, "rounds": committed,
           "files": sorted(touched)}
    if not dry_run:
        out["restore_point"] = stamp
        out["next"] = "pbz diff, then pbz push (or `pbz unused --restore " + stamp + "`)"
    return out


def _partial(project, removed, skipped, rounds, stamp) -> dict:
    return {"verb": "unused-remove", "project": project.name, "result": "PARTIAL",
            "removed": removed, "skipped": skipped, "rounds": rounds, "restore_point": stamp,
            "note": "earlier rounds stay applied; this restore point returns pre-command bytes",
            "next": "pbz unused --restore " + stamp}


def _parts(rel: str) -> list[str]:
    parts = rel.replace("\\", "/").split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise PbzError("BAD_REQUEST", "mirror path is not inside the project")
    return parts


def _under(root: str, path: str) -> bool:
    root_n = os.path.normcase(os.path.abspath(root))
    path_n = os.path.normcase(os.path.abspath(path))
    return path_n == root_n or path_n.startswith(root_n + os.sep)


def _round_ops(ws, tdir, stamp, changes, originals, manifest_files, backed, removed) -> list:
    """Mirror edits, first-seen backups and the manifest, for one commit_bytes."""
    mirror_ops, backup_ops = [], []
    for rel, new in changes.items():
        parts = _parts(rel)
        path = os.path.join(ws.mirror, *parts)
        if not _under(ws.mirror, path):
            raise PbzError("BAD_REQUEST", "mirror path is not inside the project")
        if rel not in originals:
            if os.path.isfile(path):
                originals[rel] = read_text(path)
            else:
                originals[rel] = (None, b"")
        text0, bom0 = originals[rel]
        bom_write = read_text(path)[1] if os.path.isfile(path) else b""
        if new is None:
            mirror_ops.append(("delete", path))
            after = None
        else:
            mirror_ops.append(("write", path, new, bom_write))
            after = sha(bom_write + new.encode("utf-8"))
        manifest_files[rel] = {
            "existed": text0 is not None,
            "before": sha(bom0 + text0.encode("utf-8")) if text0 is not None else None,
            "after": after,
        }
        if text0 is not None and rel not in backed:
            bpath = os.path.join(tdir, "files", *parts)
            backup_ops.append(("write", bpath, text0, bom0))
            backed.add(rel)
    body = json.dumps({"stamp": stamp, "removed": removed, "files": manifest_files},
                      indent=1, ensure_ascii=False)
    manifest = ("write", os.path.join(tdir, "manifest.json"), body, b"")
    return mirror_ops + backup_ops + [manifest]


def restore(ws, project, stamp: str) -> dict:
    if not isinstance(stamp, str) or not _STAMP_RE.fullmatch(stamp):
        raise PbzError("BAD_REQUEST", "restore point id is not a single name")
    trash = os.path.join(ws.mirror, TRASH)
    tdir = os.path.join(trash, stamp)
    if not _under(trash, tdir):
        raise PbzError("BAD_REQUEST", "restore point id is not a single name")
    try:
        with open(os.path.join(tdir, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        raise PbzError("NOT_FOUND", f"restore point {stamp}")
    files = man.get("files")
    if not isinstance(files, dict):
        raise PbzError("INVALID", "restore point is unreadable")
    planned = []
    seen = {}
    conflicts = []
    for rel, info in files.items():
        if not isinstance(rel, str) or not isinstance(info, dict):
            raise PbzError("INVALID", "restore point is unreadable")
        parts = _parts(rel)
        path = os.path.join(ws.mirror, *parts)
        if not _under(ws.mirror, path):
            raise PbzError("BAD_REQUEST", "restore path escapes the mirror")
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            raise PbzError("BAD_REQUEST", "restore paths collide")
        seen[norm] = rel
        if file_sha(path) != info.get("after"):
            conflicts.append(rel)
        planned.append((rel, parts, path, info))
    if conflicts:
        return {"verb": "unused-restore", "result": "CONFLICT", "files": conflicts,
                "hint": "these mirror files changed after the removal (pull or push since?)"}
    ops = []
    for rel, parts, path, info in planned:
        if info.get("existed"):
            bpath = os.path.join(tdir, "files", *parts)
            if not _under(os.path.join(tdir, "files"), bpath):
                raise PbzError("BAD_REQUEST", "restore path escapes the mirror")
            try:
                text, bom = read_text(bpath)
            except FileNotFoundError:
                raise PbzError("NOT_FOUND", "restore backup is missing")
            except (OSError, UnicodeError):
                raise PbzError("INVALID", "restore backup is unreadable")
            expect = info.get("before")
            if expect is not None and sha(bom + text.encode("utf-8")) != expect:
                raise PbzError("INVALID", "restore backup does not match the restore point")
            ops.append(("write", path, text, bom))
        else:
            ops.append(("delete", path))
    commit_bytes(ops)
    return {"verb": "unused-restore", "result": "RESTORED", "objects": man.get("removed", []),
            "next": "pbz diff (if the removal was already pushed, push again)"}


# ---------------------------------------------------------------- removal planning


class _Doc:
    """Pending edits to one mirror file (spans in the original text)."""

    def __init__(self, text: str):
        self.text = text
        self.cuts: list[tuple[int, int, str]] = []
        self.delete = False

    def cut(self, start: int, end: int, repl: str = "") -> None:
        self.cuts.append((start, end, repl))

    def result(self) -> str | None:
        if self.delete:
            return None
        out, pos = [], 0
        for s, e, r in sorted(set(self.cuts)):
            if s < pos:
                s = pos
                if e <= s:
                    continue
            out.append(self.text[pos:s])
            out.append(r)
            pos = e
        out.append(self.text[pos:])
        return "".join(out)


def _plan(ws, project, g, todo, rep, m_step, skipped) -> dict[str, str | None]:
    model = g.model
    mroot = f"{project.name}.SemanticModel/definition/"
    docs: dict[str, _Doc] = {}

    def doc(rel: str) -> _Doc:
        if rel not in docs:
            docs[rel] = _Doc(model.docs[rel].text if rel in model.docs else
                             read_text(os.path.join(ws.mirror, *rel.split("/")))[0])
        return docs[rel]

    tables = {g.objects[k].name for k in todo if k[0] == "table"}
    removed_names = {(g.objects[k].kind, g.objects[k].table.lower(), g.objects[k].name.lower()) for k in todo}
    m_cols: dict[str, list[str]] = {}
    for k in todo:
        o = g.objects[k]
        if o.kind == "table":
            t = model.table(o.name)
            doc(t.file).delete = True
            _drop_ref_line(doc, model, o.name)
            for c in t.columns.values():  # auto date/time tables of its date columns
                for ch in (x for x in (c.node.children if c.node else []) if x.kind == "variation"):
                    local = tmdl.split_dotted(ch.prop("defaultHierarchy"))[:1]
                    lt = model.table(local[0]) if local else None
                    if lt is not None and lt.private:
                        doc(lt.file).delete = True
                        _drop_ref_line(doc, model, lt.name)
                        for r in model.relationships:
                            if lt.name in (r.from_table, r.to_table) and r.node is not None:
                                doc(r.node.file).cut(*tmdl.block_span(model.docs[r.node.file], r.node))
            continue
        if o.table in tables:
            continue  # its table goes as a whole
        node = _node_of(model, o)
        if node is None:
            continue
        d = doc(node.file)
        s, e = tmdl.block_span(model.docs[node.file], node)
        d.cut(s, e)
        if o.kind == "column" and not o.calculated and o.extra.get("source_column"):
            m_cols.setdefault(o.table, []).append(o.extra["source_column"])
        if o.kind == "column":  # its auto date/time table and relationship go with it
            for ch in (c for c in node.children if c.kind == "variation"):
                local = tmdl.split_dotted(ch.prop("defaultHierarchy"))[:1]
                lt = model.table(local[0]) if local else None
                if lt is None or not lt.private:
                    continue
                doc(lt.file).delete = True
                _drop_ref_line(doc, model, lt.name)
                for r in model.relationships:
                    if lt.name in (r.from_table, r.to_table) and r.node is not None:
                        doc(r.node.file).cut(*tmdl.block_span(model.docs[r.node.file], r.node))
    for tname, cols in m_cols.items():
        if m_step == "none":
            continue
        t = model.table(tname)
        p = next((p for p in t.partition_nodes if (p.decl_value.strip().lower() or "m") == "m"), None)
        if p is None:
            continue
        reg = next((r for r in model.docs[p.file].regions if r.node is p and r.kind == "m"), None)
        if reg is None:
            continue
        keep = [c.source_column for c in t.columns.values()
                if c.source_column and not c.calculated and c.source_column not in cols]
        edit = _m_remove_step(model.docs[p.file].text[reg.start:reg.end], cols, keep, m_step)
        if edit is None:
            skipped.append({"object": f"M query of {tname}", "reason": "not a simple `let ... in <step>` query: "
                            "remove the columns in Power Query yourself, or rerun with --m-step none"})
            continue
        doc(p.file).cut(reg.start, reg.end, edit)
    # Power Query editor order lists query names: drop removed tables and queries
    gone_queries = {g.objects[k].name for k in todo if k[0] in ("table", "m_query")}
    for rel, d0 in model.docs.items():
        for node in d0.nodes:
            if node.kind == "annotation" and node.name == "PBI_QueryOrder" and node.parent is None and gone_queries:
                reg = next((r for r in d0.regions if r.node is node), None)
                try:
                    names = json.loads(d0.text[reg.start:reg.end]) if reg else None
                except ValueError:
                    names = None
                if isinstance(names, list) and any(n in gone_queries for n in names):
                    doc(rel).cut(reg.start, reg.end, json.dumps([n for n in names if n not in gone_queries],
                                                                separators=(",", ":"), ensure_ascii=False))
    # perspectives: drop entries of removed objects
    for pname, pnode in model.perspectives.items():
        pdoc = model.docs[pnode.file]
        for pt in (c for c in pnode.children if c.kind == "perspectiveTable"):
            if pt.name in tables:
                doc(pnode.file).cut(*tmdl.block_span(pdoc, pt))
                continue
            for ch in pt.children:
                kind = {"perspectiveColumn": "column", "perspectiveMeasure": "measure",
                        "perspectiveHierarchy": "hierarchy"}.get(ch.kind)
                if kind and any(r[0] == kind and r[2] == ch.name.lower() and (kind == "measure" or r[1] == pt.name.lower())
                                for r in removed_names):
                    doc(pnode.file).cut(*tmdl.block_span(pdoc, ch))
    out = {}
    for rel, d in docs.items():
        res = d.result()
        out[mroot + rel if rel in model.docs else rel] = res
    from .governance import culture_edits_drop
    gone = [(g.objects[k].kind, g.objects[k].table, g.objects[k].name) for k in todo
            if g.objects[k].kind in ("table", "column", "measure", "hierarchy")]
    for rel, text in culture_edits_drop(os.path.join(project.mirror_model, "definition"), gone).items():
        out[mroot + rel] = text
    _plan_report(ws, project, g, todo, rep, out)
    return out


def _drop_ref_line(doc, model, table: str) -> None:
    """Remove `ref table <name>` from model.tmdl."""
    if "model.tmdl" not in model.docs:
        return
    d = doc("model.tmdl")
    for m_ in re.finditer(r"(?m)^ref table ('(?:[^']|'')*'|\S+)[ \t]*(?:\r?\n|$)", d.text):
        if tmdl.unquote(m_.group(1)).lower() == table.lower():
            d.cut(m_.start(), m_.end())


def _node_of(model, o):
    if o.kind == "column":
        c = model.column(o.table, o.name)
        return c.node if c else None
    if o.kind == "measure":
        t = model.table(o.table)
        m = t.measures.get(o.name) if t else None
        return m.node if m else None
    if o.kind == "hierarchy":
        t = model.table(o.table)
        return next((h for h in (t.node.children if t and t.node else []) if h.kind == "hierarchy" and h.name == o.name), None)
    if o.kind == "relationship":
        return next((r.node for r in model.relationships if r.name == o.name), None)
    if o.kind == "m_query":
        return model.expressions.get(o.name)
    if o.kind == "function":
        return model.functions.get(o.name)
    return None


def _m_remove_step(src: str, cols: list[str], keep: list[str], mode: str) -> str | None:
    """Append a column removal step to a `let ... in <step>` query. None when the shape is not simple."""
    toks = lexers.significant(lexers.lex_m(src))
    if not toks or toks[0].kind != "ident" or toks[0].text != "let":
        return None
    depth, let_depth, in_tok = 0, 0, None
    for i, t in enumerate(toks):
        if t.kind == "op" and t.text in "([{":
            depth += 1
        elif t.kind == "op" and t.text in ")]}":
            depth -= 1
        elif t.kind == "ident" and t.text == "let" and depth == 0:
            let_depth += 1
        elif t.kind == "ident" and t.text == "in" and depth == 0:
            let_depth -= 1
            if let_depth == 0:
                in_tok = i
                break
    if in_tok is None or in_tok + 2 != len(toks) or toks[in_tok + 1].kind not in ("ident", "qident"):
        return None
    last = toks[in_tok + 1]
    taken = {(t.value or t.text) for t in toks if t.kind in ("ident", "qident")}
    n = 1
    name = "pbz Removed Columns"
    while name in taken:
        n += 1
        name = f"pbz Removed Columns {n}"
    line_start = src.rfind("\n", 0, toks[1].start) + 1   # indentation of the first step
    indent = re.match(r"[ \t]*", src[line_start:]).group(0)
    if mode == "keep":
        body = f"Table.SelectColumns({last.text}, {{{', '.join(_m_str(c) for c in keep)}}}, MissingField.Ignore)"
    else:
        body = f"Table.RemoveColumns({last.text}, {{{', '.join(_m_str(c) for c in cols)}}}, MissingField.Ignore)"
    prev_end = toks[in_tok - 1].end
    step = f",\n{indent}#\"{name}\" = {body}"
    return src[:prev_end] + step + src[prev_end:last.start] + f'#"{name}"' + src[last.end:]


def _m_str(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _plan_report(ws, project, g, todo, rep, out: dict) -> None:
    """Report-side removals: report-level measures, unreferenced bookmarks, unused custom visuals."""
    rms = [g.objects[k] for k in todo if k[0] == "report_measure"]
    by_report: dict[str, dict] = {}
    for o in rms:
        by_report.setdefault(o.extra.get("report", ""), {}).setdefault("measures", set()).add(o.name.lower())
    for r in rep:
        by_report.setdefault(r["report"], {}).setdefault(r["kind"], set()).add(r["id"])
    for rname, what in by_report.items():
        ddir = os.path.join(ws.mirror, f"{rname}.Report", "definition")
        base = f"{rname}.Report/definition/"
        if what.get("measures"):
            _json_drop(ddir, "reportExtensions.json", base, out,
                       lambda root: [m for e in _items(root.get("entities")) for m in _items(e.get("measures"))
                                     if _str(m.get("name")).lower() in what["measures"]])
        if what.get("bookmark"):
            ids = what["bookmark"]
            for bid in ids:
                rel = f"bookmarks/{bid}.bookmark.json"
                if os.path.exists(os.path.join(ddir, *rel.split("/"))):
                    out[base + rel] = None
            _json_drop(ddir, "bookmarks/bookmarks.json", base, out,
                       lambda root: [it for it in _items(root.get("items")) if _str(it.get("name")) in ids]
                       + [c for it in _items(root.get("items")) for c in _items(it.get("children")) if _str(c) in ids])
        if what.get("custom_visual"):
            cvs = what["custom_visual"]
            _json_drop(ddir, "report.json", base, out,
                       lambda root: [x for x in _items(root.get("publicCustomVisuals")) if _str(x) in cvs])


def _items(node) -> list:
    return node.items if isinstance(node, pbir.JArr) else []


def _str(node) -> str:
    return node.value if isinstance(node, pbir.JStr) else ""


def _json_drop(ddir: str, rel: str, base: str, out: dict, pick) -> None:
    """Remove array items (picked from the parsed file) with their separating commas; spans kept exact."""
    path = os.path.join(ddir, *rel.split("/"))
    if not os.path.exists(path):
        return
    text = out.get(base + rel) or read_text(path)[0]
    root = pbir.parse_json(text)
    victims = {id(n) for n in pick(root)}
    if not victims:
        return
    cuts = []

    def visit(node):
        if isinstance(node, pbir.JArr):
            keep = [it for it in node.items if id(it) not in victims]
            if len(keep) != len(node.items):
                inner_ws = text[node.start + 1:node.items[0].start] if node.items else ""
                if keep:
                    sep = text[node.items[0].end:node.items[1].start] if len(node.items) > 1 else ", "
                    body = sep.join(text[it.start:it.end] for it in keep)
                    close_ws = text[node.items[-1].end:node.end - 1]
                    cuts.append((node.start + 1, node.end - 1, inner_ws + body + close_ws))
                else:
                    cuts.append((node.start + 1, node.end - 1, ""))
                return
            for it in node.items:
                visit(it)
        elif isinstance(node, pbir.JObj):
            for _k, v in node.pairs:
                visit(v)

    visit(root)
    new, pos = [], 0
    for s, e, r in sorted(cuts):
        new.append(text[pos:s])
        new.append(r)
        pos = e
    new.append(text[pos:])
    out[base + rel] = "".join(new)
