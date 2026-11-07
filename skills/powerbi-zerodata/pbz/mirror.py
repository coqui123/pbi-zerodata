"""pbz-workspace mirror: pull (real -> sanitized), diff, push (sanitized -> real, validated)."""
from __future__ import annotations

import datetime as dt
import copy
import difflib
import hashlib
import json
import os
import shutil
import uuid

from . import desktop, policy as policy_mod, tmdl, tokens, validate
from .classify import Classifier
from .common import PbzError, Project, Workspace, commit_bytes, file_sha, read_text, sha, write_text

SKIP_DIRS = {".pbi", "daxqueries", "tmdlscripts", "customvisuals", "node_modules"}  # cultures: mirrored, see tokens
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
    write_text(manifest_path(ws, project), json.dumps(man, indent=1))


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


def _read_prepared(path: str, rel: str):
    """Read one pull input. A decode or IO failure names the mirror-relative path and nothing else."""
    try:
        return read_text(path)
    except (OSError, UnicodeError, ValueError):
        raise PbzError("REFUSED", rel) from None


def pull(ws: Workspace, project: Project, force: bool = False, rebase: bool = False) -> dict:
    # Force and rebase do not bypass an interrupted commit.
    gate = _pull_blocked(ws, project)
    if gate:
        return gate
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
    # Phase 1: read and tokenize every selected file, plus every rebase input merge will consume.
    # Nothing is written here, so a later decode failure leaves the mirror untouched.
    prepared = []
    for rel, real in project_files(project):
        text, bom = _read_prepared(real, rel)
        mtext = _tokenize(rel, text, model, clf, tm)
        back, unknown = _detokenize(rel, mtext, tm)
        if back != text or unknown:
            lossy.append(rel)
            mtext = None
        real_h = file_sha(real)
        old = man.get("files", {}).get(rel, {})
        item = {"rel": rel, "bom": bom, "mtext": mtext, "real_h": real_h, "old": old}
        if rel in keep and old.get("real") != real_h:
            mine = os.path.join(ws.mirror, *rel.split("/"))
            base = os.path.join(bdir, *rel.split("/"))
            item["mine_text"], item["mine_bom"] = _read_prepared(mine, rel)
            item["base_text"] = _read_prepared(base, rel)[0] if os.path.exists(base) else None
        prepared.append(item)
    # Phase 2 reuses those strings. It does not read the same files again.
    for item in prepared:
        rel, bom, mtext = item["rel"], item["bom"], item["mtext"]
        real_h, old = item["real_h"], item["old"]
        mpath = os.path.join(ws.mirror, *rel.split("/"))
        if rel in keep:
            if old.get("real") == real_h:
                new_files[rel] = old  # real unchanged; keep agent edits as they are
                continue
            merged = _merge3(ws, project, rel, mtext, item["mine_text"], item["mine_bom"], item.get("base_text"))
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
    warnings = []
    try:
        snapshot(ws, project, "pull")
    except (OSError, ValueError, TypeError, PbzError):
        warnings.append({"code": "POST_PULL_HISTORY", "message": "pull succeeded; history snapshot was not recorded"})
    out = {"verb": "pull", "project": project.name, "result": "PULLED", "files": len(new_files),
           "classification": clf.mode, "token_occurrences": counts, "lossy_files": lossy, "conflicts": conflicts}
    if warnings:
        out["warnings"] = warnings
    return out


def merge3(base: str, mine: str, theirs: str) -> tuple[str, bool]:
    """Line-based three-way merge in the style of `git merge-file` (no git needed).
    Returns (merged text, clean). Overlapping or adjacent edits from both sides that differ become
    conflict blocks with <<<<<<< mine / ======= / >>>>>>> theirs markers."""
    b, a, t = base.splitlines(keepends=True), mine.splitlines(keepends=True), theirs.splitlines(keepends=True)

    def hunks(other):
        sm = difflib.SequenceMatcher(None, b, other, autojunk=False)
        return [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]

    def apply(hs, s, e):
        out, pos = [], s
        for hs_, he_, lines in hs:
            out += b[pos:hs_] + lines
            pos = he_
        return out + b[pos:e]

    ha, hb = hunks(a), hunks(t)
    out, pos, clean, ia, ib = [], 0, True, 0, 0
    while ia < len(ha) or ib < len(hb):
        first_a = ia < len(ha) and (ib >= len(hb) or ha[ia][0] <= hb[ib][0])
        s, e = (ha[ia] if first_a else hb[ib])[:2]
        ga, gb = [], []
        grew = True
        while grew:  # absorb every hunk from either side that overlaps or touches the region
            grew = False
            while ia < len(ha) and ha[ia][0] <= e:
                ga.append(ha[ia])
                s, e = min(s, ha[ia][0]), max(e, ha[ia][1])
                ia += 1
                grew = True
            while ib < len(hb) and hb[ib][0] <= e:
                gb.append(hb[ib])
                s, e = min(s, hb[ib][0]), max(e, hb[ib][1])
                ib += 1
                grew = True
        out += b[pos:s]
        va, vb = apply(ga, s, e), apply(gb, s, e)
        if not gb or va == vb:
            out += va
        elif not ga:
            out += vb
        else:
            clean = False
            nl = "\r\n" if any(x.endswith("\r\n") for x in va + vb) else "\n"
            fix = lambda xs: xs[:-1] + [xs[-1] + nl] if xs and not xs[-1].endswith(("\n", "\r")) else xs
            out += [f"<<<<<<< mine{nl}"] + fix(va) + [f"======={nl}"] + fix(vb) + [f">>>>>>> theirs{nl}"]
        pos = e
    out += b[pos:]
    return "".join(out), clean


def _merge3(ws: Workspace, project: Project, rel: str, theirs: str | None,
            mine_text: str | None = None, mine_bom: bytes | None = None, base_text: str | None = None) -> str | None:
    """Three-way merge agent edits (mirror) with a new upstream mirror text. All inputs sanitized.

    Pull passes the phase-1 strings so this write cannot fail on a late re-read.
    """
    if theirs is None:
        return None
    mine = os.path.join(ws.mirror, *rel.split("/"))
    base = os.path.join(base_dir(ws, project), *rel.split("/"))
    if mine_text is None:
        mine_text, mine_bom = read_text(mine)
        base_text = read_text(base)[0] if os.path.exists(base) else theirs
    elif base_text is None:
        base_text = theirs
    merged, clean = merge3(base_text, mine_text, theirs)
    write_text(mine, merged, mine_bom or b"")
    write_text(base, theirs, mine_bom or b"")
    return merged if clean else None


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


def _select(ch: dict, only: list[str] | None) -> tuple[dict, list[str]]:
    """Keep the pending changes whose mirror path matches one of `only` (globs, or a path prefix)."""
    if not only:
        return ch, []
    import fnmatch
    pats = [o.replace("\\", "/").strip("/") for o in only]

    def hit(rel):
        low = rel.lower()
        return any(fnmatch.fnmatch(low, p.lower()) or low.startswith(p.lower() + "/") or low == p.lower()
                   or fnmatch.fnmatch(low, "*/" + p.lower()) for p in pats)

    picked = {k: [r for r in v if hit(r)] for k, v in ch.items()}
    held = sorted(r for v in ch.values() for r in v if not hit(r))
    return picked, held


def _transaction_dir(ws, project) -> str:
    return os.path.join(ws.private, "transactions", project.name)


def _transaction_path(ws, project, entry) -> str:
    """Resolve a journal target, rejecting traversal and symlink escapes before mutation."""
    if entry.get("scope") not in ("workspace", "private"):
        raise PbzError("RECOVERY_PATH", "unknown transaction path scope")
    root = ws.root if entry["scope"] == "workspace" else ws.private
    path = os.path.abspath(os.path.join(root, *entry["path"].split("/")))
    allowed = ((project.model_dir, project.report_dir) if entry["scope"] == "workspace" else
               (base_dir(ws, project), manifest_path(ws, project)))
    canonical = os.path.normcase(os.path.realpath(path))
    if not any(canonical == os.path.normcase(os.path.realpath(a)) or
               canonical.startswith(os.path.normcase(os.path.realpath(a)) + os.sep) for a in allowed):
        raise PbzError("RECOVERY_PATH", "transaction path is outside the project")
    return path


_JOURNAL_STATES = {"PREPARING", "PREPARED", "APPLYING", "COMMITTED", "ROLLED_BACK", "RECOVERY_REQUIRED"}


def _pull_blocked(ws, project) -> dict | None:
    """Ordinary, forced and rebased pulls all stop while a commit lock or pending recovery exists."""
    lock = os.path.join(_transaction_dir(ws, project), ".commit-lock")
    pending = pending_recovery(ws, project)
    if pending or os.path.exists(lock):
        return {"verb": "pull", "project": project.name, "result": "RECOVERY_REQUIRED", "transactions": pending}
    return None


def _journal_ok(journal) -> bool:
    """Known states and entry shapes only. Invalid journals never contribute their payload."""
    if not isinstance(journal, dict) or journal.get("version") != 1:
        return False
    if journal.get("state") not in _JOURNAL_STATES:
        return False
    entries = journal.get("entries")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("scope") not in ("workspace", "private"):
            return False
        path = entry.get("path")
        if not isinstance(path, str) or not path or ".." in path.replace("\\", "/").split("/"):
            return False
        for key in ("before", "after"):
            if key in entry and entry[key] is not None and not isinstance(entry[key], str):
                return False
        if "backup" in entry and not isinstance(entry["backup"], str):
            return False
    return True


def pending_recovery(ws, project) -> list[dict]:
    """Closed metadata only; journal payloads and backups remain private."""
    out = []
    directory = _transaction_dir(ws, project)
    for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
        if not os.path.isdir(os.path.join(directory, name)):
            continue
        path = os.path.join(directory, name, "journal.json")
        try:
            with open(path, encoding="utf-8") as f:
                journal = json.load(f)
        except (OSError, ValueError):
            out.append({"id": name, "state": "UNREADABLE"})
            continue
        if not _journal_ok(journal):
            out.append({"id": name, "state": "INVALID"})
            continue
        if journal["state"] not in ("COMMITTED", "ROLLED_BACK"):
            out.append({"id": name, "state": journal["state"]})
    return out


def _journal_save(directory, journal):
    write_text(os.path.join(directory, "journal.json"), json.dumps(journal, indent=1))


def _tree_files(directory):
    return {os.path.relpath(os.path.join(cur, name), directory).replace("\\", "/")
            for cur, _dirs, files in os.walk(directory) for name in files}


def _copy_atomic(source, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temp = target + ".pbz-tmp"
    try:
        shutil.copyfile(source, temp)
        os.replace(temp, target)
    finally:
        if os.path.isfile(temp):
            os.remove(temp)


def _begin_transaction(ws, project, stamp, targets):
    directory = os.path.join(_transaction_dir(ws, project), stamp)
    os.makedirs(directory, exist_ok=False)
    entries = []
    _journal_save(directory, {"version": 1, "state": "PREPARING", "entries": []})
    for index, (scope, rel, expected) in enumerate(targets):
        entry = {"scope": scope, "path": rel, "after": expected}
        target = _transaction_path(ws, project, entry)
        entry["before"] = file_sha(target)
        if entry["before"] is not None:
            backup = os.path.join(directory, "before", str(index))
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(target, backup)
            entry["backup"] = str(index)
        entries.append(entry)
    journal = {"version": 1, "state": "PREPARED", "entries": entries}
    _journal_save(directory, journal)
    return directory, journal


def _restore_transaction(ws, project, directory, journal):
    if not _journal_ok(journal):
        return {"result": "RECOVERY_REQUIRED"}
    conflicts = []
    # Check all targets before restoring any. Never overwrite a third-party change.
    for entry in journal["entries"]:
        target = _transaction_path(ws, project, entry)
        if file_sha(target) not in (entry["before"], entry["after"]):
            conflicts.append(entry["path"])
        if "backup" in entry:
            backup = os.path.realpath(os.path.join(directory, "before", entry["backup"]))
            if not backup.startswith(os.path.realpath(directory) + os.sep) or file_sha(backup) != entry["before"]:
                raise PbzError("RECOVERY_BACKUP", "transaction backup is missing or invalid")
    if conflicts:
        journal["state"] = "RECOVERY_REQUIRED"
        _journal_save(directory, journal)
        return {"result": "RECOVERY_CONFLICT", "conflicts": conflicts}
    for entry in reversed(journal["entries"]):
        target = _transaction_path(ws, project, entry)
        if file_sha(target) == entry["before"]:
            continue
        if entry["before"] is None:
            if os.path.isfile(target):
                os.remove(target)
        else:
            with open(os.path.join(directory, "before", entry["backup"]), "rb") as f:
                content = f.read()
            os.makedirs(os.path.dirname(target), exist_ok=True)
            temp = target + ".pbz-recover"
            with open(temp, "wb") as f:
                f.write(content)
            os.replace(temp, target)
    journal["state"] = "ROLLED_BACK"
    _journal_save(directory, journal)
    return {"result": "ROLLED_BACK"}


def _process_alive(pid: int) -> bool:
    # On Windows os.kill(pid, 0) can terminate a process. Query a read-only process
    # handle instead; denied/unknown state remains conservatively alive.
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87  # invalid PID; all other errors are unknown/alive
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def recover(ws, project, transaction_id: str | None = None) -> dict:
    """Roll back interrupted, uncommitted pushes. Never resume a half-validated commit."""
    if desktop.find_instance(project):
        return {"verb": "recover", "result": "DESKTOP_OPEN"}
    lock = os.path.join(_transaction_dir(ws, project), ".commit-lock")
    stale_lock = False
    if os.path.exists(lock):
        try:
            with open(lock) as f:
                owner = int(f.read())
            if owner <= 0:
                raise ValueError()
            if _process_alive(owner):
                return {"verb": "recover", "result": "COMMIT_BUSY"}
            stale_lock = True
        except (OSError, ValueError):
            return {"verb": "recover", "result": "RECOVERY_REQUIRED"}
    pending = pending_recovery(ws, project)
    if transaction_id is not None:
        if not any(p["id"] == transaction_id for p in pending):
            return {"verb": "recover", "result": "NOT_FOUND", "transactions": []}
        pending = [p for p in pending if p["id"] == transaction_id]
    results = []
    for item in pending:
        directory = os.path.join(_transaction_dir(ws, project), item["id"])
        try:
            with open(os.path.join(directory, "journal.json"), encoding="utf-8") as f:
                journal = json.load(f)
            if not _journal_ok(journal):
                result = {"result": "RECOVERY_REQUIRED"}
            else:
                result = _restore_transaction(ws, project, directory, journal)
        except (OSError, ValueError, KeyError, PbzError):
            result = {"result": "RECOVERY_REQUIRED"}
        results.append({"id": item["id"], **result})
    failed = any(r["result"] != "ROLLED_BACK" for r in results)
    if stale_lock and not failed:
        os.remove(lock)
    return {"verb": "recover", "result": "RECOVERY_REQUIRED" if failed
            else "RECOVERED" if results or stale_lock else "NOTHING_TO_RECOVER", "transactions": results}


def _push(ws: Workspace, project: Project, reload: bool = False, dry_run: bool = False,
         only: list[str] | None = None, preflight_only: bool = False) -> dict:
    pending = pending_recovery(ws, project)
    if pending:
        return {"verb": "push", "result": "RECOVERY_REQUIRED", "transactions": pending}
    man = load_manifest(ws, project)
    if not man:
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    ch, held = _select(mirror_changes(ws, project, man), only)
    if not any(ch.values()) and not preflight_only:
        return {"verb": "push", "project": project.name, "result": "NOTHING_TO_PUSH",
                **({"held_back": held} if held else {})}
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
    if not preflight_only and open_inst and (model_touched or not reload):
        return {"verb": "push", "project": project.name, "result": "DESKTOP_OPEN",
                "hint": "close Desktop first (`pbz desktop close`), or use --reload for report-only changes"}
    if not preflight_only and open_inst and open_inst.get("unsaved"):
        return {"verb": "push", "project": project.name, "result": "DESKTOP_DIRTY",
                "hint": "Desktop has unsaved changes a reload would discard: ask the user to save (then pbz pull "
                        "--rebase) or close it"}
    if not preflight_only and model_touched and os.path.exists(os.path.join(project.model_dir, ".pbi", "unappliedChanges.json")):
        return {"verb": "push", "project": project.name, "result": "PENDING_PQ_CHANGES",
                "hint": "Desktop holds Power Query edits that were never applied; applying them later would "
                        "overwrite these model changes. Ask the user to apply or discard them in Desktop, save, "
                        "then pbz pull --rebase"}
    tm = tokens.TokenMap(ws.private)
    new_real: dict[str, tuple[str, bytes]] = {}
    captured_mirror: dict[str, tuple[str, bytes]] = {}
    unknown_all: list[str] = []
    for rel in ch["changed"] + ch["added"]:
        mtext, bom = read_text(os.path.join(ws.mirror, *rel.split("/")))
        captured_mirror[rel] = (mtext, bom)
        rtext, unknown = _detokenize(rel, mtext, tm)
        unknown_all += unknown
        new_real[rel] = (rtext, bom)
    if unknown_all:
        return {"verb": "push", "project": project.name, "result": "UNKNOWN_TOKEN",
                "tokens": sorted(set(unknown_all))}
    # Candidate bytes and the preconditions they were read against, before validation.
    pre_real = {rel: file_sha(_real_path(ws, rel)) for rel in ch["changed"] + ch["deleted"]}
    pre_mirror = {rel: file_sha(os.path.join(ws.mirror, *rel.split("/"))) for rel in captured_mirror}
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    stage = os.path.join(ws.private, "stage", stamp)
    transaction = None
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
                _prune_empty(p, os.path.join(stage, rel.split("/")[0]))
        from . import images, visuals  # private custom visuals and images staged since the last push travel with it
        srj = os.path.join(stage, project.name + ".Report", "definition", "report.json")
        new_rj = read_text(srj)[0] if os.path.exists(srj) else ""
        pkgs = visuals.pending_for(ws, new_rj) if new_rj else {}
        if pkgs:
            visuals.install(pkgs, os.path.join(stage, project.name + ".Report"))
        real_rj = os.path.join(project.report_dir, "definition", "report.json")
        imgs_new = images.staged(ws, project, new_rj) if new_rj else {}
        imgs_drop = images.dropped(read_text(real_rj)[0], new_rj) if new_rj and os.path.exists(real_rj) else []
        images.install(imgs_new, os.path.join(stage, project.name + ".Report"))
        images.delete(imgs_drop, os.path.join(stage, project.name + ".Report"))
        findings = validate.run_stage(ws, project, stage, tm)
        if any(file_sha(_real_path(ws, rel)) != pre_real[rel] for rel in pre_real) or \
                any(file_sha(os.path.join(ws.mirror, *rel.split("/"))) != pre_mirror[rel] for rel in pre_mirror):
            return {"verb": "push", "project": project.name, "result": "CONFLICT",
                    "hint": "project files changed during validation; pull and retry"}
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
        if errors or dry_run or preflight_only:
            return {"verb": "push", "project": project.name, "result": "INVALID" if errors else "DRY_RUN_OK",
                    "objects": summary, "findings": findings, **({"held_back": held} if held else {})}
        authored_before = {}
        authored_after = {}
        for rel in ch["changed"] + ch["added"]:
            bpath = os.path.join(base_dir(ws, project), *rel.split("/"))
            authored_before[rel] = read_text(bpath)[0] if os.path.exists(bpath) else None
            authored_after[rel] = captured_mirror[rel][0]
        # Include every resource file, not only its report.json registration, in recovery.
        resource_paths = set()
        for guid in pkgs:
            prefix = f"{project.name}.Report/CustomVisuals/{guid}"
            resource_paths.update(prefix + "/" + p for p in
                                  _tree_files(os.path.join(project.report_dir, "CustomVisuals", guid)) |
                                  _tree_files(os.path.join(stage, project.name + ".Report", "CustomVisuals", guid)))
        resource_paths.update(f"{project.name}.Report/StaticResources/RegisteredResources/{item}"
                              for item in set(imgs_new) | set(imgs_drop))
        real_targets = {}
        for rel in set(touched) | resource_paths:
            if rel in new_real:
                rtext, bom = new_real[rel]
                real_targets[rel] = hashlib.sha256(bom + rtext.encode("utf-8")).hexdigest()
            else:
                real_targets[rel] = file_sha(os.path.join(stage, *rel.split("/")))
        new_files = copy.deepcopy(files)
        private_targets = {}
        for rel in ch["changed"] + ch["added"]:
            mtext, bom = captured_mirror[rel]
            mirror_hash = hashlib.sha256(bom + mtext.encode("utf-8")).hexdigest()
            new_files[rel] = {"real": real_targets[rel], "mirror": mirror_hash}
            private_targets[os.path.relpath(os.path.join(base_dir(ws, project), *rel.split("/")), ws.private).replace("\\", "/")] = mirror_hash
        for rel in ch["deleted"]:
            new_files.pop(rel, None)
            private_targets[os.path.relpath(os.path.join(base_dir(ws, project), *rel.split("/")), ws.private).replace("\\", "/")] = None
        expected_manifest = {**man, "files": new_files}
        private_targets[os.path.relpath(manifest_path(ws, project), ws.private).replace("\\", "/")] = sha(json.dumps(expected_manifest, indent=1))
        transaction = _begin_transaction(ws, project, stamp,
                                         [("workspace", p, h) for p, h in sorted(real_targets.items())] +
                                         [("private", p, h) for p, h in sorted(private_targets.items())])
        directory, journal = transaction
        if any(file_sha(_transaction_path(ws, project, e)) != e["before"] for e in journal["entries"]):
            journal["state"] = "ROLLED_BACK"
            _journal_save(directory, journal)
            transaction = None
            return {"verb": "push", "result": "CONFLICT", "hint": "project files changed during staging; pull and retry"}
        journal["state"] = "APPLYING"
        _journal_save(directory, journal)
        # Conservative privacy accounting precedes mutation. Failed attempts may retain literal
        # hashes, but recovery must never loosen disclosure budgets or lose authored literals.
        from .checks import record_authored
        record_authored(ws, authored_before, authored_after)
        backup = os.path.join(ws.private, "backups", stamp)
        for rel in sorted(set(ch["changed"] + ch["deleted"]) | resource_paths):
            real = _real_path(ws, rel)
            if not os.path.isfile(real):
                continue
            dst = os.path.join(backup, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(real, dst)
        for rel, (rtext, bom) in new_real.items():
            write_text(_real_path(ws, rel), rtext, bom)
        for rel in ch["deleted"]:
            os.remove(_real_path(ws, rel))
            _prune_empty(_real_path(ws, rel), os.path.join(ws.root, rel.split("/")[0]))
        # Commit staged resources by atomic file replacement as well; directory replacement
        # could otherwise leave a partially copied package with an unrecognizable hash.
        for rel in sorted(resource_paths):
            target = _real_path(ws, rel)
            source = os.path.join(stage, *rel.split("/"))
            if os.path.isfile(source):
                _copy_atomic(source, target)
            elif os.path.isfile(target):
                os.remove(target)
                _prune_empty(target, project.report_dir)
        installed, img_in, img_out = sorted(pkgs), sorted(imgs_new), sorted(imgs_drop)
        for rel in ch["changed"] + ch["added"]:
            mtext, bom = captured_mirror[rel]
            mirror_hash = hashlib.sha256(bom + mtext.encode("utf-8")).hexdigest()
            files[rel] = {"real": real_targets[rel], "mirror": mirror_hash}
            write_text(os.path.join(base_dir(ws, project), *rel.split("/")), mtext, bom)
        for rel in ch["deleted"]:
            files.pop(rel, None)
            bp = os.path.join(base_dir(ws, project), *rel.split("/"))
            if os.path.exists(bp):
                os.remove(bp)
        man["files"] = files
        save_manifest(ws, project, man)
        journal["state"] = "COMMITTED"
        _journal_save(directory, journal)
        transaction = None  # Project and synchronization metadata have committed together.
        warnings = []
        for phase, operation in (
            ("RESOURCE_CLEANUP", lambda: (visuals.clear_pending(ws, installed), images.clear_pending(ws, project, img_in))),
            ("HISTORY", lambda: (_log_push(ws, project, stamp, summary), snapshot(ws, project, "push"))),
        ):
            try:
                operation()
            except (OSError, ValueError, PbzError):
                warnings.append({"code": "POST_COMMIT_" + phase, "message": "commit succeeded; follow-up work is incomplete"})
        result = {"verb": "push", "project": project.name, "result": "PUSHED", "objects": summary,
                  "findings": findings, "backup": stamp}
        if installed:
            result["custom_visuals_installed"] = installed
        if img_in:
            result["images_installed"] = img_in
        if img_out:
            result["images_removed"] = img_out
        if held:
            result["held_back"] = held  # still pending in the mirror
        if warnings:
            result["warnings"] = warnings
        if reload and open_inst:
            try:
                result["reload"] = desktop.reload(project)
            except Exception:
                result["reload"] = "RELOAD_FAILED"
        return result
    except BaseException:
        if transaction is None:
            raise
        directory, journal = transaction
        try:
            restored = _restore_transaction(ws, project, directory, journal)
        except (OSError, ValueError, PbzError):
            restored = {"result": "RECOVERY_REQUIRED"}
        return {"verb": "push", "project": project.name, **restored,
                "error": "COMMIT_FAILED", "transaction": stamp,
                "hint": "inspect recovery status before retrying; diagnostic content remains private"}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def push(ws: Workspace, project: Project, reload: bool = False, dry_run: bool = False,
         only: list[str] | None = None, preflight_only: bool = False) -> dict:
    if dry_run or preflight_only:
        return _push(ws, project, reload, dry_run, only, preflight_only)
    directory = _transaction_dir(ws, project)
    os.makedirs(directory, exist_ok=True)
    lock = os.path.join(directory, ".commit-lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {"verb": "push", "result": "RECOVERY_REQUIRED" if pending_recovery(ws, project) else "COMMIT_BUSY",
                "hint": "another commit or an interrupted commit owns the project lock"}
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return _push(ws, project, reload, dry_run, only, preflight_only)
    finally:
        os.remove(lock)


def preflight(ws: Workspace, project: Project, only: list[str] | None = None) -> dict:
    """Run the same private staged validators as push, without changing files or Desktop.

    Unlike commit, preflight is safe while Desktop is open/dirty. It validates the saved
    files plus mirror changes, not unsaved UI state. Conflict/token checks still apply.
    """
    result = push(ws, project, dry_run=True, only=only, preflight_only=True)
    result["verb"] = "preflight"
    if result.get("result") == "DRY_RUN_OK":
        result["result"] = "PREFLIGHT_OK"
    result["scope"] = "saved files plus selected mirror changes"
    return result


def _real_path(ws: Workspace, rel: str) -> str:
    return os.path.join(ws.root, *rel.split("/"))


def _prune_empty(path: str, stop: str) -> None:
    """Remove the folders a deleted file leaves empty (a deleted visual's folder), up to `stop`."""
    stop = os.path.normcase(os.path.abspath(stop))
    d = os.path.dirname(os.path.abspath(path))
    while os.path.normcase(d).startswith(stop + os.sep) and os.path.isdir(d) and not os.listdir(d):
        os.rmdir(d)
        d = os.path.dirname(d)


def revert(ws: Workspace, project: Project, paths: list[str] | None = None, obj: str | None = None) -> dict:
    """Discard mirror edits: whole files back to the last pulled / pushed text (`paths`, globs allowed), or
    one TMDL object (`obj` = "measure 'Table'[Name]" style: kind, table, name) back to its base block."""
    man = load_manifest(ws, project)
    if not man:
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    bdir = base_dir(ws, project)
    if obj:
        return _revert_object(ws, project, bdir, obj)
    ch, _held = _select(mirror_changes(ws, project, man), paths or ["*"])
    ops, done = [], []
    readable = []
    for rel in ch["changed"] + ch["deleted"]:
        bp = os.path.join(bdir, *rel.split("/"))
        if not os.path.exists(bp):
            continue
        readable.append((rel, bp))
    for rel, bp in readable:
        text, bom = _read_prepared(bp, rel)
        ops.append(("write", os.path.join(ws.mirror, *rel.split("/")), text, bom))
        done.append(rel)
    for rel in ch["added"]:
        ops.append(("delete", os.path.join(ws.mirror, *rel.split("/"))))
        done.append(rel)
    if ops:
        commit_bytes(ops)
    return {"verb": "revert", "project": project.name, "files": sorted(done),
            "result": "REVERTED" if done else "NOTHING_TO_REVERT"}


def _revert_object(ws: Workspace, project: Project, bdir: str, obj: str) -> dict:
    import re as _re
    m = _re.match(r"^\s*(measure|column|hierarchy|partition|calculationItem)\s+(?:'((?:[^']|'')+)'|([^\s\[]+))\s*\[(.+)\]\s*$", obj)
    if not m:
        raise PbzError("BAD_REQUEST", "object as: measure 'Table'[Name] (kinds: measure column hierarchy partition calculationItem)")
    kind, table, name = m.group(1), (m.group(2) or m.group(3)).replace("''", "'"), m.group(4)
    mdef = os.path.join(project.mirror_model, "definition")
    bdef = os.path.join(bdir, project.name + ".SemanticModel", "definition")

    def find(model):
        for rel, doc in model.docs.items():
            for n in doc.nodes:
                owner = n.parent.parent if kind == "calculationItem" and n.parent is not None else n.parent
                if n.kind == kind and n.name.lower() == name.lower() and owner is not None \
                        and owner.kind == "table" and owner.name.lower() == table.lower():
                    return rel, doc, n
        return None

    mine = find(tmdl.load_model(mdef))
    base = find(tmdl.load_model(bdef)) if os.path.isdir(bdef) else None
    if mine is None and base is None:
        raise PbzError("NOT_FOUND", obj)
    if base is None:  # added since the last pull: drop it
        rel, doc, n = mine
        new = apply_block(doc.text, tmdl.block_span(doc, n), "")
    elif mine is None:  # deleted in the mirror: put the base block back at the end of its table
        rel, bdoc, bn = base
        mt = tmdl.load_model(mdef)
        tdoc = next((d for r, d in mt.docs.items() if r == rel), None)
        if tdoc is None:
            raise PbzError("NOT_FOUND", f"table file {rel} (revert the whole file instead)")
        tnode = next(x for x in tdoc.nodes if x.kind == "table" and x.parent is None)
        s, e = tmdl.block_span(bdoc, bn)
        end = tmdl.block_span(tdoc, tnode)[1]
        block = bdoc.text[s:e]
        body = tdoc.text[:end].rstrip("\r\n")
        eol = "\r\n" if "\r\n" in tdoc.text else "\n"
        new = body + eol + eol + block.rstrip("\r\n") + eol + tdoc.text[end:]
        doc = tdoc
    else:
        rel, doc, n = mine
        _brel, bdoc, bn = base
        new = apply_block(doc.text, tmdl.block_span(doc, n), bdoc.text[slice(*tmdl.block_span(bdoc, bn))])
    path = os.path.join(mdef, *rel.split("/"))
    _text, bom = _read_prepared(path, f"{project.name}.SemanticModel/definition/{rel}")
    commit_bytes([("write", path, new, bom)])
    return {"verb": "revert", "project": project.name, "object": f"{kind} {table}[{name}]",
            "result": "REVERTED", "files": [f"{project.name}.SemanticModel/definition/{rel}"]}


def apply_block(text: str, span: tuple[int, int], new: str) -> str:
    return text[:span[0]] + new + text[span[1]:]


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
    write_text(os.path.join(snaps, f"{stamp}.json"),
               json.dumps({"stamp": stamp, "event": event, "files": files}))
    names = sorted(os.listdir(snaps))
    kept = names[-HISTORY_KEEP:]
    live, readable = set(), True
    for name in kept:
        try:
            with open(os.path.join(snaps, name), encoding="utf-8") as f:
                doc = json.load(f)
            blob_ids = doc.get("files") if isinstance(doc, dict) else None
            if not isinstance(blob_ids, dict):
                readable = False
                break
            live |= set(blob_ids.values())
        except (OSError, ValueError, TypeError):
            readable = False
            break
    if readable:
        for old in names[:-HISTORY_KEEP]:
            os.remove(os.path.join(snaps, old))
        for b in os.listdir(blobs):
            if b[:-4] not in live:
                os.remove(os.path.join(blobs, b))
    return stamp


def history_list(ws: Workspace, project: Project) -> list[dict]:
    snaps = os.path.join(history_dir(ws, project), "snapshots")
    out = []
    for name in sorted(os.listdir(snaps)) if os.path.isdir(snaps) else []:
        try:
            with open(os.path.join(snaps, name), encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s, dict) or not isinstance(s.get("stamp"), str) or not isinstance(s.get("files"), dict):
                raise ValueError("snapshot")
            out.append({"stamp": s["stamp"], "event": s.get("event", ""), "files": len(s.get("files", {}))})
        except (OSError, ValueError, TypeError):
            stamp = name[:-5] if name.endswith(".json") else name
            out.append({"stamp": stamp, "event": "UNREADABLE", "files": 0})
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
