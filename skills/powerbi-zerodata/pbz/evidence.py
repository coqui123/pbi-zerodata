"""Revision-bound evidence and `pbz review`: what changed, what it touches, how it ships, which checks
ran on which revision, and what is still unverified.

Every verb that proves something (validate, push, desktop open/reload, health, check, snapshot
compare, smoke, synth test run, handoff) appends a line to `.pbz/evidence.jsonl`: the stage, verdict
counts and ids only (never values), and the revision it ran on: a fingerprint of the mirror and of the
real files as pbz last wrote them. `pbz review` turns pending edits plus that log into a review packet
(`pbz-workspace/REVIEW.md` and JSON). Evidence from another revision is STALE, never PASS.
`pbz review --accept` records the human's acceptance of the current revision (terminal only).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os

from .common import PbzError, read_text, write_text

STAGES = ("PARSED", "BOUND", "LOADED", "EXECUTED", "SYNTHETIC", "HANDOFF", "HUMAN_ACCEPTED")
LOG = "evidence.jsonl"
CLASS_ORDER = ["SECURITY_REVIEW", "REFRESH_REQUIRED", "RECALC_REQUIRED", "LOGIC", "METADATA_ONLY", "REPORT_ONLY", "UNKNOWN"]


# ---------------------------------------------------------------- revisions

def mirror_rev(ws, project) -> str:
    from . import mirror
    h = hashlib.sha256()
    for rel, text in sorted(mirror.mirror_texts(ws, project).items()):
        h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(text.encode("utf-8")).digest())
    return "m" + h.hexdigest()[:12]


def real_rev(ws, project) -> str:
    from . import mirror
    man = mirror.load_manifest(ws, project) or {}
    h = hashlib.sha256()
    for rel, rec in sorted((man.get("files") or {}).items()):
        h.update(f"{rel}\0{rec.get('real')}\n".encode("utf-8"))
    return "r" + h.hexdigest()[:12]


def _log_path(ws) -> str:
    return os.path.join(ws.pbz_dir, LOG)


def _entries(ws, project) -> list[dict]:
    out = []
    try:
        with open(_log_path(ws), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("project") == project.name:
                    out.append(e)
    except OSError:
        pass
    return out


def _append(ws, entry: dict) -> None:
    os.makedirs(ws.pbz_dir, exist_ok=True)
    with open(_log_path(ws), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- recording

def _counts(items, key="status") -> dict:
    out: dict = {}
    for it in items or []:
        if isinstance(it, dict):
            v = str(it.get(key, "?"))
            out[v] = out.get(v, 0) + 1
    return out


def summarize(verb: str, action: str | None, out: dict) -> tuple[str, dict] | None:
    """(stage, verdict summary) for verbs that prove something; None otherwise. Ids and counts only."""
    if not isinstance(out, dict) or out.get("ok") is False:
        return None
    if verb == "validate":
        f = out.get("findings") or []
        return "PARSED", {"errors": sum(1 for x in f if x.get("severity") == "error"),
                          "warnings": sum(1 for x in f if x.get("severity") == "warning")}
    if verb == "push" and out.get("result") in ("PUSHED", "DRY_RUN_OK", "INVALID"):
        f = out.get("findings") or []
        return "BOUND", {"result": out["result"], "errors": sum(1 for x in f if x.get("severity") == "error"),
                         "tom": not any(str(x.get("code", "")).startswith("TOM_") for x in f)}
    if verb == "desktop" and action in ("open", "reload"):
        ok = out.get("engine") == "READY" or (out.get("reload") or {}).get("ok") is True or out.get("ok") is True
        return "LOADED", {"action": action, "ready": bool(ok)}
    if verb == "health":
        bad = out.get("errors") or out.get("objects_in_error") or []
        return "EXECUTED", {"health_errors": len(bad) if isinstance(bad, list) else bad}
    if verb == "check":
        res = out.get("results") or []
        return "EXECUTED", {"checks": _counts(res), "ids": [r.get("id") for r in res if isinstance(r, dict)][:50]}
    if verb == "snapshot" and action == "compare":
        return "EXECUTED", {"snapshot": out.get("name"), "queries": _counts(out.get("queries"), "result")}
    if verb == "smoke":
        return "EXECUTED", {"smoke": out.get("summary") if isinstance(out.get("summary"), dict) else _counts(out.get("visuals"))}
    if verb == "synth-test" and action == "run":
        return "SYNTHETIC", {"test": out.get("name"), "result": out.get("result")}
    if verb == "handoff":
        return "HANDOFF", {"file": "HANDOFF.md"}
    return None


def record(ws, project, verb: str, action: str | None, out: dict) -> None:
    s = summarize(verb, action, out)
    if s is None or project is None:
        return
    stage, summary = s
    try:
        entry = {"ts": dt.datetime.now().replace(microsecond=0).isoformat(), "project": project.name, "stage": stage,
                 "verb": verb + (f" {action}" if action else ""), "mirror_rev": mirror_rev(ws, project),
                 "real_rev": real_rev(ws, project), "summary": summary}
    except (OSError, ValueError, PbzError):
        return
    _append(ws, entry)


# ---------------------------------------------------------------- release classes

def classify(rec: dict, side: str) -> str:
    if side == "report":
        return "REPORT_ONLY"
    kind = rec.get("kind", "")
    if kind in ("role", "tablePermission", "columnPermission", "member") or "role" in str(rec.get("file", "")).lower():
        return "SECURITY_REVIEW"
    if kind in ("partition", "expression", "refreshPolicy", "dataSource", "parameter"):
        return "REFRESH_REQUIRED"
    parts = {p.get("part", "") for p in rec.get("expressions") or []}
    props = {p.get("prop", "").rsplit(".", 1)[-1] for p in rec.get("props") or []}
    if any("partition" in p or p.endswith("source") for p in parts) or "sourceColumn" in props or "refreshPolicy" in props:
        return "REFRESH_REQUIRED"
    if kind == "table" and rec.get("status") in ("added", "removed"):
        return "REFRESH_REQUIRED"
    if kind == "column" and rec.get("status") in ("added", "removed") and not parts:
        return "REFRESH_REQUIRED"
    if kind == "relationship" or (kind in ("column", "table") and parts) or kind in ("calculationGroup", "calculationItem"):
        return "RECALC_REQUIRED"
    if kind in ("measure", "function") and (parts or rec.get("status") in ("added", "removed", "renamed", "moved")):
        return "LOGIC"
    if props or rec.get("description") or rec.get("annotations") or rec.get("status") in ("renamed", "moved"):
        return "METADATA_ONLY"
    return "UNKNOWN"


NEXT = {
    "SECURITY_REVIEW": "a human reviews role, RLS and OLS changes; test roles in Desktop (View as)",
    "REFRESH_REQUIRED": "after push: refresh in Desktop (the user's Refresh button), then pbz health",
    "RECALC_REQUIRED": "after push: pbz desktop open, pbz refresh (recalculates), pbz health",
    "LOGIC": "pbz check (compiles, then equal / additive checks), pbz snapshot compare, pbz synth test run",
    "METADATA_ONLY": "pbz validate and push are enough",
    "REPORT_ONLY": "pbz push --reload, pbz smoke --page <page>, then the user's visual review (pbz handoff)",
    "UNKNOWN": "inspect the change by hand",
}


# ---------------------------------------------------------------- review

def _status(e: dict | None, cur_m: str, cur_r: str, pending: bool, stage: str) -> str:
    if e is None:
        return "NOT_RUN"
    if stage == "PARSED":
        return "CURRENT" if e.get("mirror_rev") == cur_m else "STALE"
    if e.get("real_rev") != cur_r:
        return "STALE"
    if stage in ("LOADED", "EXECUTED", "HANDOFF", "HUMAN_ACCEPTED") and pending:
        return "STALE (pending edits not pushed)"
    return "CURRENT"


def _suggested_tests(ws, project, logic: list[str]) -> list[str]:
    out = []
    if logic:
        out.append("pbz check with a compiles check per changed measure: " + ", ".join(logic[:10]))
    snap = os.path.join(ws.pbz_dir, "snapshots")
    for f in sorted(os.listdir(snap)) if os.path.isdir(snap) else []:
        if f.endswith(".json"):
            out.append(f"pbz snapshot compare {f[:-5]}")
    tdir = os.path.join(ws.pbz_dir, "synth-tests")
    names = {n.lower().strip("[]") for n in logic}
    for f in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
        if not f.endswith(".json"):
            continue
        try:
            text = read_text(os.path.join(tdir, f))[0].lower()
        except OSError:
            continue
        if not names or any(f"[{n}]" in text for n in names):
            out.append(f"pbz synth test run {f[:-5]}")
    return out


def review(ws, project, accept: bool = False) -> dict:
    from . import changes, consumers
    cur_m, cur_r = mirror_rev(ws, project), real_rev(ws, project)
    if accept:
        from .cli import _is_human
        if not _is_human():
            raise PbzError("HUMAN_ONLY", "acceptance is the reviewer's: run `pbz review --accept` yourself in a terminal")
        _append(ws, {"ts": dt.datetime.now().replace(microsecond=0).isoformat(), "project": project.name,
                     "stage": "HUMAN_ACCEPTED", "verb": "review --accept", "mirror_rev": cur_m, "real_rev": cur_r,
                     "summary": {"accepted": True}})
    try:
        ch = changes.run(ws, project, "last", "mirror")
    except PbzError:
        ch = {"result": "NO_HISTORY"}
    model, report = ch.get("model") or [], ch.get("report") or []
    pending = ch.get("result") == "CHANGED"
    rows, by_class = [], {}
    for side, recs in (("model", model), ("report", report)):
        for rec in recs:
            cls = classify(rec, side)
            label = rec.get("name") or rec.get("id") or rec.get("kind")
            if side == "model" and rec.get("table") and rec.get("kind") in ("column", "measure", "hierarchy"):
                label = f"{rec['table']}[{rec['name']}]" if rec["kind"] == "column" else (
                    f"[{rec['name']}]" if rec["kind"] == "measure" else f"{rec['table']}.{rec['name']}")
            row = {"class": cls, "side": side, "kind": rec.get("kind"), "object": label, "status": rec.get("status")}
            if rec.get("expressions"):
                row["parts"] = [f"{p.get('part')}: {p.get('class')}" for p in rec["expressions"]][:6]
            if rec.get("props"):
                row["props"] = [p.get("prop") for p in rec["props"]][:8]
            if rec.get("impact"):
                row["impact"] = rec["impact"]
            if rec.get("page"):
                row["page"] = rec["page"]
            rows.append(row)
            by_class[cls] = by_class.get(cls, 0) + 1
    rows.sort(key=lambda r: (CLASS_ORDER.index(r["class"]), r["side"], str(r["object"]).lower()))
    logic = [r["object"] for r in rows if r["class"] == "LOGIC" and r["kind"] == "measure"]
    latest: dict = {}
    for e in _entries(ws, project):
        latest[e["stage"]] = e
    evidence = []
    for stage in STAGES:
        e = latest.get(stage)
        evidence.append({"stage": stage, "status": _status(e, cur_m, cur_r, pending, stage),
                         **({"verb": e["verb"], "when": e["ts"], "summary": e.get("summary")} if e else {})})
    try:
        reg = consumers.load_registry(ws)["consumers"]
    except (OSError, ValueError):
        reg = []
    coverage = {"external_consumers": "registered" if reg else "none registered",
                "human_visual_review": next((x["status"] for x in evidence if x["stage"] == "HUMAN_ACCEPTED"), "NOT_RUN")}
    out = {"verb": "review", "project": project.name, "revision": {"mirror": cur_m, "real": cur_r},
           "pending_edits": pending, "classes": {c: by_class[c] for c in CLASS_ORDER if c in by_class},
           "changes": rows[:300], "evidence": evidence, "coverage": coverage,
           "next": [f"{c}: {NEXT[c]}" for c in CLASS_ORDER if c in by_class],
           "suggested_tests": _suggested_tests(ws, project, logic)}
    path = os.path.join(ws.mirror, "REVIEW.md")
    write_text(path, _markdown(out, ws, project, rows))
    out["file"] = "pbz-workspace/REVIEW.md"
    return out


def _markdown(out: dict, ws, project, rows: list[dict]) -> str:
    L = [f"# Review packet: {project.name}", "",
         f"Revision: mirror `{out['revision']['mirror']}`, real files `{out['revision']['real']}`. "
         f"Pending edits: {'yes' if out['pending_edits'] else 'no'}.", ""]
    if rows:
        L += ["## Changes by release class", "", "| Class | Kind | Object | Change | Impact |", "|---|---|---|---|---|"]
        for r in rows:
            detail = "; ".join((r.get("parts") or []) + [f"prop {p}" for p in r.get("props") or []]) or r.get("status", "")
            imp = r.get("impact") or {}
            impact = (f"{imp.get('objects_total', 0)} dependents, {imp.get('visuals', 0)} visuals" if imp else "")
            L.append(f"| {r['class']} | {r['kind']} | {r['object']} | {r['status']}: {detail} | {impact} |")
        L += ["", "## Next steps", ""] + [f"- {n}" for n in out["next"]] + [""]
    else:
        L += ["No pending edits since the last pull or push.", ""]
    if out["suggested_tests"]:
        L += ["## Suggested verification", ""] + [f"- `{t}`" if t.startswith("pbz ") else f"- {t}" for t in out["suggested_tests"]] + [""]
    L += ["## Evidence", "", "| Stage | Status | Verb | When | Summary |", "|---|---|---|---|---|"]
    for e in out["evidence"]:
        L.append(f"| {e['stage']} | {e['status']} | {e.get('verb', '')} | {e.get('when', '')} | "
                 f"{json.dumps(e.get('summary'), ensure_ascii=False) if e.get('summary') else ''} |")
    L += ["", "## Coverage", "", f"- External consumers: {out['coverage']['external_consumers']}",
          f"- Human visual review: {out['coverage']['human_visual_review']} (`pbz review --accept` in a terminal)", ""]
    pages = sorted({r.get("page") for r in rows if r.get("side") == "report" and r.get("page")}
                   | {str(r["object"]) for r in rows if r.get("side") == "report" and r.get("kind") == "page"
                      and r.get("status") != "removed"})
    if pages:
        from . import layout
        L += ["## Changed pages (layout, no data)", ""]
        for pg in pages[:5]:
            try:
                lay = layout.run(ws, project, pg, show_map=True)
            except PbzError:
                continue
            for p in lay.get("pages", []):
                if p.get("map"):
                    L += [f"### {p['page']}", "", "```"] + p["map"] + ["```", ""]
    L.append("Real-data evidence above is verdicts only (counts and ids); values never enter this packet.")
    return "\n".join(L) + "\n"
