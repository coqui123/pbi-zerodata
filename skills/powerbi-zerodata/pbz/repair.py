"""`pbz repair`: fix the structural PBIR breakage `pbz validate` reports (page order, object names,
groups, interactions, bookmark lists and stale bookmark targets) in the mirror. Never deletes a page or
visual. `--fields` also drops visual projections whose model field no longer exists. Names only."""
from __future__ import annotations

import json
import os

from . import pbir, tmdl
from .common import PbzError, commit_bytes, read_definition_text


def _rel(ws, path: str) -> str:
    return os.path.relpath(path, ws.mirror).replace("\\", "/")


def _load(ws, path):
    """Parse a report document. Missing files are absent; a present file that cannot be read is refused."""
    if not os.path.lexists(path):
        return None, None, b""
    rel = _rel(ws, path)
    try:
        text, bom = read_definition_text(path)
        data = json.loads(text)
    except (OSError, ValueError, UnicodeDecodeError):
        raise PbzError("REFUSED", rel)
    if not isinstance(data, dict):
        raise PbzError("REFUSED", rel)
    return data, text, bom


def _dump(data, old_text: str) -> str:
    out = json.dumps(data, indent=2, ensure_ascii=False)
    return out + ("\n" if old_text.endswith("\n") else "")


def _lineage(project, fix, writes) -> None:
    """Give every object that copies another object's lineageTag a fresh one (the first keeps it)."""
    import re
    import uuid
    mdef = os.path.join(project.mirror_model, "definition")
    model = tmdl.load_model(mdef)
    seen: set[str] = set()
    for rel in sorted(model.docs):
        doc = model.docs[rel]
        new_text, changed = doc.text, False
        edits = []
        for node in doc.nodes:
            tag = node.props.get("lineageTag", "").strip()
            if not tag:
                continue
            if tag.lower() not in seen:
                seen.add(tag.lower())
                continue
            s, e = tmdl.block_span(doc, node)
            for m in re.finditer(r"(?m)^(\t+lineageTag:\s*)(\S+)", doc.text[s:e]):
                if len(m.group(1)) - len(m.group(1).lstrip("\t")) == node.indent + 1 and m.group(2).lower() == tag.lower():
                    edits.append((s + m.start(2), s + m.end(2), str(uuid.uuid4())))
                    fix("DUPLICATE_LINEAGE_TAG", rel, f"new lineageTag for {node.kind} {node.name}")
                    break
        if edits:
            from .tokens import apply_edits
            new_text, changed = apply_edits(doc.text, sorted(edits)), True
        if changed:
            path = os.path.join(mdef, *rel.split("/"))
            writes[path] = (new_text, read_definition_text(path)[1])


def _format_conflicts(project, fix, writes) -> None:
    """Drop the static formatString of measures that also have a formatStringDefinition (Desktop refuses to
    open such a model although TOM accepts it). The dynamic format already decides the format."""
    from .props import _remove_prop
    from .tokens import apply_edits
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        return
    model = tmdl.load_model(mdef)
    for rel in sorted(model.docs):
        path = os.path.join(mdef, *rel.split("/"))
        doc = tmdl.parse(rel, writes[path][0]) if path in writes else model.docs[rel]  # after --lineage edits
        edits = []
        for node in doc.nodes:
            if node.kind == "measure" and "formatString" in node.props and any(
                    c.kind == "formatStringDefinition" for c in node.children):
                ed = _remove_prop(doc, node, "formatString")
                if ed:
                    edits += ed
                    fix("FORMAT_STRING_CONFLICT", rel, f"dropped the static formatString of measure {node.name}")
        if edits:
            writes[path] = (apply_edits(doc.text, sorted(edits)), read_definition_text(path)[1])


def run(ws, project, dry_run: bool = False, fields: bool = False, lineage: bool = False) -> dict:
    ddir = os.path.join(project.mirror_report, "definition")
    if not os.path.isdir(ddir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first (no report in the mirror)")
    fixes: list[dict] = []
    writes: dict[str, tuple] = {}

    def fix(code, where, what):
        fixes.append({"code": code, "where": where, "fix": what})

    def stage(path, data, old_text, bom):
        writes[path] = (_dump(data, old_text), bom)

    pdir = os.path.join(ddir, "pages")
    page_ids = sorted(d for d in os.listdir(pdir) if os.path.isfile(os.path.join(pdir, d, "page.json"))) \
        if os.path.isdir(pdir) else []
    visuals: dict[str, set] = {}
    model = tmdl.load_model(os.path.join(project.mirror_model, "definition")) if fields else None
    for pid in page_ids:
        pp = os.path.join(pdir, pid, "page.json")
        page, ptext, pbom = _load(ws, pp)
        if page is None:
            continue
        changed = False
        if page.get("name") != pid:
            page["name"] = pid
            changed = True
            fix("PAGE_NAME_MISMATCH", f"pages/{pid}/page.json", "name set to the folder name")
        vdir = os.path.join(pdir, pid, "visuals")
        vids = sorted(d for d in os.listdir(vdir) if os.path.isfile(os.path.join(vdir, d, "visual.json"))) \
            if os.path.isdir(vdir) else []
        visuals[pid] = set(vids)
        groups = set()
        loaded = {}
        for vid in vids:
            v, vtext, vbom = _load(ws, os.path.join(vdir, vid, "visual.json"))
            if v is not None:
                loaded[vid] = (v, vtext, vbom)
                if "visualGroup" in v:
                    groups.add(vid)
        for vid, (v, vtext, vbom) in loaded.items():
            vchanged = False
            if v.get("name") != vid:
                v["name"] = vid
                vchanged = True
                fix("VISUAL_NAME_MISMATCH", f"pages/{pid}/visuals/{vid}", "name set to the folder name")
            if v.get("parentGroupName") and v["parentGroupName"] not in groups:
                del v["parentGroupName"]
                vchanged = True
                fix("GROUP_MISSING", f"pages/{pid}/visuals/{vid}", "removed parentGroupName of a missing group")
            if fields and isinstance(v.get("visual"), dict):
                qs = ((v["visual"].get("query") or {}).get("queryState") or {})
                for role, bucket in qs.items():
                    keep = []
                    for proj in (bucket or {}).get("projections") or []:
                        refs = pbir.walk_fields(pbir.parse_json(json.dumps(proj.get("field", {}))), model)
                        ok = all(model.measure(r.name) if r.kind == "measure" else
                                 (model.table(r.table) is not None and (r.kind == "hierarchy" or model.column(r.table, r.name)))
                                 for r in refs)
                        if ok:
                            keep.append(proj)
                        else:
                            fix("UNKNOWN_FIELD", f"pages/{pid}/visuals/{vid}", f"dropped a {role} projection on a missing field")
                    if len(keep) != len((bucket or {}).get("projections") or []):
                        bucket["projections"] = keep
                        vchanged = True
            if vchanged:
                stage(os.path.join(vdir, vid, "visual.json"), v, vtext, vbom)
        inter = page.get("visualInteractions")
        if isinstance(inter, list):
            keep = [i for i in inter if not isinstance(i, dict) or (i.get("source") in visuals[pid] and i.get("target") in visuals[pid])]
            if len(keep) != len(inter):
                page["visualInteractions"] = keep
                changed = True
                fix("INTERACTION_MISSING_VISUAL", f"pages/{pid}/page.json", f"dropped {len(inter) - len(keep)} stale interaction(s)")
        if changed:
            stage(pp, page, ptext, pbom)
    mp = os.path.join(pdir, "pages.json")
    meta, mtext, mbom = _load(ws, mp)
    if isinstance(meta, dict):
        order = meta.get("pageOrder") or []
        new = []
        for pid in order:
            if pid in page_ids and pid not in new:
                new.append(pid)
        new += [p for p in page_ids if p not in new]
        mchanged = new != order
        if mchanged:
            meta["pageOrder"] = new
            fix("PAGE_ORDER", "pages/pages.json", "pageOrder rebuilt from the page folders (order kept)")
        if meta.get("activePageName") and meta["activePageName"] not in page_ids and new:
            meta["activePageName"] = new[0]
            mchanged = True
            fix("ACTIVE_PAGE_MISSING", "pages/pages.json", "activePageName set to the first page")
        if mchanged:
            stage(mp, meta, mtext, mbom)
    bdir = os.path.join(ddir, "bookmarks")
    if os.path.isdir(bdir):
        files = sorted(f[:-len(".bookmark.json")] for f in os.listdir(bdir) if f.endswith(".bookmark.json"))
        bp = os.path.join(bdir, "bookmarks.json")
        bm, btext, bbom = _load(ws, bp)
        if isinstance(bm, dict):
            items, listed, changed = [], set(), False
            for it in bm.get("items") or []:
                if not isinstance(it, dict):
                    continue
                if it.get("children") is not None:
                    kids = [c for c in it["children"] if c in files]
                    if len(kids) != len(it["children"]):
                        it["children"] = kids
                        changed = True
                    listed |= set(kids)
                    items.append(it)
                elif it.get("name") in files:
                    listed.add(it["name"])
                    items.append(it)
                else:
                    changed = True
            for f in files:
                if f not in listed:
                    items.append({"name": f})
                    changed = True
            if changed:
                bm["items"] = items
                stage(bp, bm, btext, bbom)
                fix("BOOKMARK_LIST", "bookmarks/bookmarks.json", "bookmark list matched to the bookmark files")
        for f in files:
            path = os.path.join(bdir, f + ".bookmark.json")
            b, text, bom = _load(ws, path)
            if not isinstance(b, dict):
                continue
            es = b.get("explorationState") or {}
            changed = False
            secs = es.get("sections") or {}
            for pid in list(secs):
                if pid not in page_ids:
                    del secs[pid]
                    changed = True
                    continue
                vc = (secs[pid] or {}).get("visualContainers")
                if isinstance(vc, dict):
                    for vid in [k for k in vc if k not in visuals.get(pid, set())]:
                        del vc[vid]
                        changed = True
            if es.get("activeSection") and es["activeSection"] not in page_ids and page_ids:
                es["activeSection"] = next(iter(secs), page_ids[0])
                changed = True
            opts = b.get("options") or {}
            tv = opts.get("targetVisualNames")
            if isinstance(tv, list):
                allv = set().union(*visuals.values()) if visuals else set()
                keep = [x for x in tv if x in allv]
                if len(keep) != len(tv):
                    opts["targetVisualNames"] = keep
                    changed = True
            if changed:
                stage(path, b, text, bom)
                fix("BOOKMARK_STALE", f"bookmarks/{f}.bookmark.json", "removed targets of deleted pages or visuals")
    if lineage:
        _lineage(project, fix, writes)
    _format_conflicts(project, fix, writes)
    if writes and not dry_run:
        commit_bytes([("write", path, text, bom) for path, (text, bom) in writes.items()])
    return {"verb": "repair", "project": project.name, "result": ("DRY_RUN" if dry_run else "REPAIRED") if fixes else "NOTHING_TO_REPAIR",
            "fixes": fixes, "files": sorted(os.path.relpath(p, ws.mirror).replace("\\", "/") for p in writes),
            **({"next": "pbz validate, pbz diff, pbz push"} if fixes and not dry_run else {})}
