"""pbz-workspace mirror: pull (real -> sanitized), diff, push (sanitized -> real, validated)."""
from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import shutil
import subprocess

from . import desktop, policy as policy_mod, tmdl, tokens, validate
from .classify import Classifier
from .common import PbzError, Project, Workspace, file_sha, read_text, sha, write_text

SKIP_DIRS = {".pbi", "daxqueries", "tmdlscripts", "cultures", "customvisuals", "node_modules"}
JSON_EXT = (".json", ".pbir", ".pbism", ".platform")


def _scope(base: str, kind: str) -> list[str]:
    """Relative paths (forward slashes) of mirrored files under a .SemanticModel / .Report folder."""
    out = []
    if not os.path.isdir(base):
        return out
    for cur, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
        relcur = os.path.relpath(cur, base).replace("\\", "/")
        for f in files:
            rel = f if relcur == "." else f"{relcur}/{f}"
            low = f.lower()
            if low.endswith(".tmdl") or low.endswith(JSON_EXT) or low == ".platform":
                if kind == "report" and rel.lower().startswith("staticresources/") and not low.endswith(".json"):
                    continue
                out.append(rel)
    return sorted(out)


def project_files(project: Project) -> list[tuple[str, str]]:
    """[(mirror-relative path, absolute real path)] for one project."""
    out = []
    for base, kind in ((project.model_dir, "model"), (project.report_dir, "report")):
        top = os.path.basename(base)
        for rel in _scope(base, kind):
            out.append((f"{top}/{rel}", os.path.join(base, *rel.split("/"))))
    return out


def manifest_path(ws: Workspace, project: Project) -> str:
    return os.path.join(ws.private, f"manifest-{project.name}.json")


def base_dir(ws: Workspace, project: Project) -> str:
    return os.path.join(ws.private, "mirror-base", project.name)


def load_manifest(ws: Workspace, project: Project) -> dict:
    try:
        with open(manifest_path(ws, project), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_manifest(ws: Workspace, project: Project, man: dict) -> None:
    os.makedirs(ws.private, exist_ok=True)
    with open(manifest_path(ws, project), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)


def real_model(project: Project) -> tmdl.Model:
    return tmdl.load_model(os.path.join(project.model_dir, "definition"))


def _tokenize(rel: str, text: str, model, clf, tm) -> str:
    if rel.lower().endswith(".tmdl"):
        return tokens.tokenize_tmdl(text, rel, model, clf, tm)
    return tokens.tokenize_pbir(text, rel, model, clf, tm)


def _detokenize(rel: str, text: str, tm) -> tuple[str, list[str]]:
    if rel.lower().endswith(".tmdl"):
        return tokens.detokenize_tmdl(text, tm)
    return tokens.detokenize_pbir(text, tm)


def mirror_changes(ws: Workspace, project: Project, man: dict) -> dict:
    files = man.get("files", {})
    changed, added, deleted = [], [], []
    prefixes = (project.name + ".SemanticModel/", project.name + ".Report/")
    for rel, info in files.items():
        p = os.path.join(ws.mirror, *rel.split("/"))
        h = file_sha(p)
        if h is None:
            deleted.append(rel)
        elif h != info.get("mirror"):
            changed.append(rel)
    for cur, _dirs, fs in os.walk(ws.mirror):
        for f in fs:
            rel = os.path.relpath(os.path.join(cur, f), ws.mirror).replace("\\", "/")
            if rel.startswith(prefixes) and rel not in files and (f.lower().endswith(".tmdl") or f.lower().endswith(JSON_EXT)):
                added.append(rel)
    return {"changed": sorted(changed), "added": sorted(added), "deleted": sorted(deleted)}


def pull(ws: Workspace, project: Project, force: bool = False, rebase: bool = False) -> dict:
    pol = policy_mod.load(ws)
    man = load_manifest(ws, project)
    if man and os.path.isdir(ws.mirror):
        pending = mirror_changes(ws, project, man)
        if any(pending.values()) and not (force or rebase):
            raise PbzError("UNPUSHED_EDITS", "mirror has edits; run `pbz push`, `pbz pull --rebase` or `pbz pull --force`")
    else:
        pending = {"changed": [], "added": [], "deleted": []}
    model = real_model(project)
    clf = Classifier(model, ws.private, pol)
    tm = tokens.TokenMap(ws.private)
    new_files, lossy, counts = {}, [], {}
    keep = set(pending["changed"] + pending["added"]) if rebase else set()
    conflicts = []
    bdir = base_dir(ws, project)
    for rel, real in project_files(project):
        text, bom = read_text(real)
        mtext = _tokenize(rel, text, model, clf, tm)
        back, unknown = _detokenize(rel, mtext, tm)
        if back != text or unknown:
            lossy.append(rel)
            mtext = None
        mpath = os.path.join(ws.mirror, *rel.split("/"))
        real_h = file_sha(real)
        old = man.get("files", {}).get(rel, {})
        if rel in keep:
            if old.get("real") == real_h:
                new_files[rel] = old  # real unchanged; keep agent edits as they are
                continue
            merged = _merge3(ws, project, rel, mtext)
            if merged is None:
                conflicts.append(rel)
            new_files[rel] = {"real": real_h, "mirror": old.get("mirror"), "lossy": mtext is None}
            continue
        if mtext is None:
            if os.path.exists(mpath):
                os.remove(mpath)
            new_files[rel] = {"real": real_h, "mirror": None, "lossy": True}
            continue
        write_text(mpath, mtext, bom)
        write_text(os.path.join(bdir, *rel.split("/")), mtext, bom)
        new_files[rel] = {"real": real_h, "mirror": file_sha(mpath)}
        for t in tokens.TOKEN_RE.findall(mtext):
            lab = t[1:-1].rsplit("#", 1)[0]
            kind = "column" if "[" in lab else lab.split(":")[0]
            counts[kind] = counts.get(kind, 0) + 1
    for rel in man.get("files", {}):
        if rel not in new_files and rel not in keep:
            p = os.path.join(ws.mirror, *rel.split("/"))
            if os.path.exists(p):
                os.remove(p)
    tm.save()
    man = {"version": 1, "project": project.name, "pulled": dt.datetime.now().isoformat(timespec="seconds"),
           "classification": clf.mode, "files": new_files}
    save_manifest(ws, project, man)
    _write_guide(ws, clf.mode)
    snapshot(ws, project, "pull")
    return {"verb": "pull", "project": project.name, "files": len(new_files), "classification": clf.mode,
            "token_occurrences": counts, "lossy_files": lossy, "conflicts": conflicts}


def _merge3(ws: Workspace, project: Project, rel: str, theirs: str | None) -> str | None:
    """Three-way merge agent edits (mirror) with a new upstream mirror text. All inputs sanitized."""
    if theirs is None:
        return None
    mine = os.path.join(ws.mirror, *rel.split("/"))
    base = os.path.join(base_dir(ws, project), *rel.split("/"))
    tmp = os.path.join(ws.private, "merge-theirs.tmp")
    write_text(tmp, theirs)
    try:
        p = subprocess.run(["git", "merge-file", "-p", mine, base if os.path.exists(base) else tmp, tmp],
                           capture_output=True)
    except FileNotFoundError:
        return None
    finally:
        os.remove(tmp)
    merged = p.stdout.decode("utf-8", errors="replace")
    _text, bom = read_text(mine)
    write_text(mine, merged, bom)
    write_text(base, theirs, bom)
    return merged if p.returncode == 0 else None


def diff(ws: Workspace, project: Project, patch: bool = False) -> dict:
    man = load_manifest(ws, project)
    if not man:
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    ch = mirror_changes(ws, project, man)
    out = {"verb": "diff", "project": project.name, **ch}
    if patch:
        lines = []
        for rel in ch["changed"] + ch["added"]:
            a_path = os.path.join(base_dir(ws, project), *rel.split("/"))
            b_path = os.path.join(ws.mirror, *rel.split("/"))
            a = read_text(a_path)[0].splitlines(keepends=True) if os.path.exists(a_path) else []
            b = read_text(b_path)[0].splitlines(keepends=True)
            lines.extend(difflib.unified_diff(a, b, f"base/{rel}", f"mirror/{rel}"))
        out["patch"] = "".join(lines)
    return out


def _object_summary(rel: str, old: str, new: str) -> list[str]:
    if not rel.lower().endswith(".tmdl"):
        return [rel]
    def objs(text):
        if text is None:
            return {}
        doc = tmdl.parse(rel, text)
        res = {}
        for node in doc.nodes:
            if node.kind in ("measure", "column", "table", "partition", "relationship", "hierarchy", "expression",
                             "role", "calculationItem"):
                regs = [text[r.start:r.end] for r in doc.regions if r.node is node]
                res[f"{node.kind} {node.name}"] = sha(json.dumps([node.props, regs], sort_keys=True))
        return res
    a, b = objs(old), objs(new)
    out = [f"+ {k}" for k in b if k not in a] + [f"- {k}" for k in a if k not in b] + \
          [f"~ {k}" for k in b if k in a and a[k] != b[k]]
    return out or [f"~ {rel} (formatting only)"]


def push(ws: Workspace, project: Project, reload: bool = False, dry_run: bool = False) -> dict:
    man = load_manifest(ws, project)
    if not man:
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    ch = mirror_changes(ws, project, man)
    if not any(ch.values()):
        return {"verb": "push", "project": project.name, "result": "NOTHING_TO_PUSH"}
    files = man.get("files", {})
    touched = ch["changed"] + ch["added"] + ch["deleted"]
    conflicts = []
    for rel in ch["changed"] + ch["deleted"]:
        real = _real_path(ws, rel)
        if file_sha(real) != files[rel].get("real"):
            conflicts.append(rel)
        if files[rel].get("lossy"):
            conflicts.append(rel + " (not round-trippable, edit in Desktop)")
    for rel in ch["added"]:
        if os.path.exists(_real_path(ws, rel)):
            conflicts.append(rel)
    if conflicts:
        return {"verb": "push", "project": project.name, "result": "CONFLICT", "files": conflicts,
                "hint": "real files changed since pull (Desktop save?). Run `pbz pull --rebase`."}
    model_touched = any(r.startswith(project.name + ".SemanticModel/") for r in touched)
    open_inst = desktop.find_instance(project)
    if open_inst and (model_touched or not reload):
        return {"verb": "push", "project": project.name, "result": "DESKTOP_OPEN",
                "hint": "close Desktop first (`pbz desktop close`), or use --reload for report-only changes"}
    tm = tokens.TokenMap(ws.private)
    new_real: dict[str, tuple[str, bytes]] = {}
    unknown_all: list[str] = []
    for rel in ch["changed"] + ch["added"]:
        mtext, bom = read_text(os.path.join(ws.mirror, *rel.split("/")))
        rtext, unknown = _detokenize(rel, mtext, tm)
        unknown_all += unknown
        new_real[rel] = (rtext, bom)
    if unknown_all:
        return {"verb": "push", "project": project.name, "result": "UNKNOWN_TOKEN",
                "tokens": sorted(set(unknown_all))}
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = os.path.join(ws.private, "stage", stamp)
    try:
        for src in (project.model_dir, project.report_dir):
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(stage, os.path.basename(src)),
                                ignore=shutil.ignore_patterns(".pbi", "DAXQueries", "TMDLScripts"))
        if os.path.exists(project.pbip):
            shutil.copy2(project.pbip, os.path.join(stage, os.path.basename(project.pbip)))
        for rel, (rtext, bom) in new_real.items():
            write_text(os.path.join(stage, *rel.split("/")), rtext, bom)
        for rel in ch["deleted"]:
            p = os.path.join(stage, *rel.split("/"))
            if os.path.exists(p):
                os.remove(p)
        findings = validate.run_stage(ws, project, stage, tm)
        errors = [f for f in findings if f["severity"] == "error"]
        summary = []
        for rel in touched:
            old = None
            bpath = os.path.join(base_dir(ws, project), *rel.split("/"))
            if os.path.exists(bpath):
                old = read_text(bpath)[0]
            mp = os.path.join(ws.mirror, *rel.split("/"))
            new = read_text(mp)[0] if os.path.exists(mp) else None
            summary += _object_summary(rel, old, new)
        if errors or dry_run:
            return {"verb": "push", "project": project.name, "result": "INVALID" if errors else "DRY_RUN_OK",
                    "objects": summary, "findings": findings}
        backup = os.path.join(ws.private, "backups", stamp)
        for rel in ch["changed"] + ch["deleted"]:
            real = _real_path(ws, rel)
            dst = os.path.join(backup, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(real, dst)
        for rel, (rtext, bom) in new_real.items():
            write_text(_real_path(ws, rel), rtext, bom)
        for rel in ch["deleted"]:
            os.remove(_real_path(ws, rel))
        for rel in ch["changed"] + ch["added"]:
            mp = os.path.join(ws.mirror, *rel.split("/"))
            files[rel] = {"real": file_sha(_real_path(ws, rel)), "mirror": file_sha(mp)}
            mtext, bom = read_text(mp)
            write_text(os.path.join(base_dir(ws, project), *rel.split("/")), mtext, bom)
        for rel in ch["deleted"]:
            files.pop(rel, None)
            bp = os.path.join(base_dir(ws, project), *rel.split("/"))
            if os.path.exists(bp):
                os.remove(bp)
        man["files"] = files
        save_manifest(ws, project, man)
        _log_push(ws, project, stamp, summary)
        snapshot(ws, project, "push")
        result = {"verb": "push", "project": project.name, "result": "PUSHED", "objects": summary,
                  "findings": findings, "backup": stamp}
        if reload and open_inst:
            result["reload"] = desktop.reload(project)
        return result
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _real_path(ws: Workspace, rel: str) -> str:
    return os.path.join(ws.root, *rel.split("/"))


# ---------------------------------------------------------------- mirror history (sanitized snapshots)

HISTORY_KEEP = 20


def history_dir(ws: Workspace, project: Project) -> str:
    return os.path.join(ws.private, "mirror-history", project.name)


def mirror_texts(ws: Workspace, project: Project) -> dict[str, str]:
    """{mirror-relative path: text} of the project's current mirror files (sanitized)."""
    out = {}
    for base in (project.mirror_model, project.mirror_report):
        top = os.path.basename(base)
        for cur, _dirs, files in os.walk(base):
            for f in files:
                low = f.lower()
                if low.endswith(".tmdl") or low.endswith(JSON_EXT):
                    p = os.path.join(cur, f)
                    rel = f"{top}/" + os.path.relpath(p, base).replace("\\", "/")
                    out[rel] = read_text(p)[0]
    return out


def snapshot(ws: Workspace, project: Project, event: str) -> str:
    """Record the mirror as a history snapshot (content-addressed, deduplicated, last HISTORY_KEEP kept)."""
    hdir = history_dir(ws, project)
    blobs, snaps = os.path.join(hdir, "blobs"), os.path.join(hdir, "snapshots")
    os.makedirs(blobs, exist_ok=True)
    os.makedirs(snaps, exist_ok=True)
    files = {}
    for rel, text in mirror_texts(ws, project).items():
        h = sha(text)
        bp = os.path.join(blobs, h + ".txt")
        if not os.path.exists(bp):
            write_text(bp, text)
        files[rel] = h
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    n = 1
    while os.path.exists(os.path.join(snaps, f"{stamp}.json")):
        n += 1
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{n:03d}"  # sorts after the bare stamp
    with open(os.path.join(snaps, f"{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump({"stamp": stamp, "event": event, "files": files}, f)
    names = sorted(os.listdir(snaps))
    for old in names[:-HISTORY_KEEP]:
        os.remove(os.path.join(snaps, old))
    live = set()
    for name in sorted(os.listdir(snaps)):
        with open(os.path.join(snaps, name), encoding="utf-8") as f:
            live |= set(json.load(f).get("files", {}).values())
    for b in os.listdir(blobs):
        if b[:-4] not in live:
            os.remove(os.path.join(blobs, b))
    return stamp


def history_list(ws: Workspace, project: Project) -> list[dict]:
    snaps = os.path.join(history_dir(ws, project), "snapshots")
    out = []
    for name in sorted(os.listdir(snaps)) if os.path.isdir(snaps) else []:
        with open(os.path.join(snaps, name), encoding="utf-8") as f:
            s = json.load(f)
        out.append({"stamp": s["stamp"], "event": s.get("event", ""), "files": len(s.get("files", {}))})
    return out


def history_texts(ws: Workspace, project: Project, stamp: str) -> dict[str, str]:
    hdir = history_dir(ws, project)
    try:
        with open(os.path.join(hdir, "snapshots", f"{stamp}.json"), encoding="utf-8") as f:
            files = json.load(f)["files"]
    except (OSError, ValueError, KeyError):
        raise PbzError("NOT_FOUND", f"snapshot {stamp}")
    return {rel: read_text(os.path.join(hdir, "blobs", h + ".txt"))[0] for rel, h in files.items()}


def _log_push(ws: Workspace, project: Project, stamp: str, summary: list[str]) -> None:
    os.makedirs(ws.pbz_dir, exist_ok=True)
    with open(os.path.join(ws.pbz_dir, "push-log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"stamp": stamp, "project": project.name, "objects": summary}) + "\n")


def _write_guide(ws: Workspace, mode: str) -> None:
    text = f"""# pbz-workspace (sanitized mirror)

Edit these files instead of the real project folders. They are the real TMDL and PBIR files with
sensitive literal values replaced by tokens. Names and formulas are unchanged.

- `«Table[Column]#n»`: one specific value of that column (stable across pulls). Keep, move or copy it.
- `«lit#n»`: a sensitive text value not tied to a known column.
- `«pii#n»`: an email, user name or similar personal identifier.
- `«num#n»`: a number typed into static text.
- `«payload:...»`: embedded data (Enter Data table, inline chart values). Do not edit inside it.

Plain literals you type pass through unchanged. Invented tokens make `pbz push` fail.
Classification mode: **{mode}** ({'engine value index' if mode == 'index' else 'name heuristics only; open the model in Desktop and run `pbz index` for exact classification'}).

Loop: `pbz pull` -> edit -> `pbz diff` -> `pbz push` -> `pbz desktop open` -> `pbz health` / `pbz check` -> `pbz handoff`.
"""
    write_text(os.path.join(ws.mirror, "MIRROR.md"), text)
