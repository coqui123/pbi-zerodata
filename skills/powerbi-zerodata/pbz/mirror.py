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
from .common import (PbzError, Project, Workspace, _contained, _reparse_link, anchor_mirror, append_checked,
                    commit_bytes, file_sha, pbz_state_path, preflight_regular, read_checked, read_text, read_text_checked, remove_file,
                    replace_bytes, sha, write_text)

SKIP_DIRS = {".pbi", "daxqueries", "tmdlscripts", "customvisuals", "node_modules"}  # cultures: mirrored, see tokens
JSON_EXT = (".json", ".pbir", ".pbism", ".platform")


def _path_excluded(path: str, root: str) -> bool:
    """True when `path` is under a skipped directory, escapes `root`, or aliases one of those.

    The directory name and the resolved target are both checked, so a junction that is not
    itself named `.pbi` still stays out of the mirror when its target is.
    """
    try:
        rel = os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return True
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or any(p.lower() in SKIP_DIRS for p in parts):
        return True
    try:
        resolved = os.path.realpath(path)
        rrel = os.path.relpath(resolved, os.path.realpath(root)).replace("\\", "/")
    except (OSError, ValueError):
        return True
    rparts = [p for p in rrel.split("/") if p not in ("", ".")]
    return any(p == ".." for p in rparts) or any(p.lower() in SKIP_DIRS for p in rparts)


def _mirrored_name(name: str) -> bool:
    low = name.lower()
    return low.endswith(".tmdl") or low.endswith(JSON_EXT) or low == ".platform"


def _project_mirror_root(project: Project, rel: str) -> str | None:
    """Mirror folder that owns one project-relative path, or None when the path is out of scope."""
    sm = project.name + ".SemanticModel/"
    rp = project.name + ".Report/"
    if rel.startswith(sm) or rel == sm.rstrip("/"):
        return project.mirror_model
    if rel.startswith(rp) or rel == rp.rstrip("/"):
        return project.mirror_report
    return None


def _path_outside_project(path: str, rel: str, ws: Workspace, project: Project) -> bool:
    """True when `path` escapes the mirror or the owning project mirror tree (cross-project alias).

    An aliased owning project root (for example `Proj.Report` junctioned to `Other.Report`)
    is refused before that root can become its own trusted containment boundary.
    """
    if _path_excluded(path, ws.mirror):
        return True
    owned = _project_mirror_root(project, rel)
    if owned is None:
        return False
    if _reparse_link(owned) or _path_excluded(owned, ws.mirror):
        return True
    return _path_excluded(path, owned)


def _scope(base: str, kind: str) -> list[str]:
    """Relative paths (forward slashes) of mirrored files under a .SemanticModel / .Report folder."""
    out = []
    if not os.path.isdir(base):
        return out
    for cur, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not _path_excluded(os.path.join(cur, d), base)]
        relcur = os.path.relpath(cur, base).replace("\\", "/")
        for f in files:
            full = os.path.join(cur, f)
            if _path_excluded(full, base):
                continue
            rel = f if relcur == "." else f"{relcur}/{f}"
            low = f.lower()
            if low.endswith(".tmdl") or low.endswith(JSON_EXT) or low == ".platform":
                if kind == "report" and rel.lower().startswith("staticresources/") and not low.endswith(".json"):
                    continue
                out.append(rel)
    return sorted(out)


def project_files(project: Project, ws: Workspace | None = None) -> list[tuple[str, str]]:
    """[(mirror-relative path, absolute real path)] for one project.

    When `ws` is supplied, owning source roots must sit under `ws.root` and must not
    themselves be aliases below that workspace. Junctions at or above the workspace remain allowed.
    """
    out = []
    for base, kind in ((project.model_dir, "model"), (project.report_dir, "report")):
        if ws is not None:
            if _reparse_link(base) or _path_excluded(base, ws.root):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        top = os.path.basename(base)
        for rel in _scope(base, kind):
            full = os.path.join(base, *rel.split("/"))
            if ws is not None and _path_excluded(full, ws.root):
                continue
            out.append((f"{top}/{rel}", full))
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


def _checked_raw(path: str, root: str) -> bytes | None:
    """Bytes of a present regular file. Missing is None. An alias or hard link is REFUSED, path only."""
    if not os.path.lexists(path):
        return None
    rel = os.path.relpath(path, root).replace("\\", "/")
    try:
        preflight_regular(path, root=root)
        return read_checked(path, root=root)
    except PbzError as err:
        if err.code == "UNSAFE_PATH":
            raise PbzError("REFUSED", rel) from None
        raise


def _split_bom(raw: bytes) -> tuple[str, bytes]:
    bom = raw[:3] if raw.startswith(b"\xef\xbb\xbf") else b""
    return raw[len(bom):].decode("utf-8"), bom


def _post_mirror_hash(ws, rel: str) -> str | None:
    """Checked hash of a present mirror file. Missing stays None so the caller reports CONFLICT."""
    path = os.path.join(ws.mirror, *rel.split("/"))
    raw = _checked_raw(path, ws.mirror)
    if raw is None:
        return None
    return hashlib.sha256(raw).hexdigest()


def mirror_changes(ws: Workspace, project: Project, man: dict, *, checked: bool = False) -> dict:
    if checked:
        anchor_mirror(ws)
    files = man.get("files", {})
    changed, added, deleted = [], [], []
    prefixes = (project.name + ".SemanticModel/", project.name + ".Report/")
    for rel, info in files.items():
        p = os.path.join(ws.mirror, *rel.split("/"))
        if _path_outside_project(p, rel, ws, project):
            changed.append(rel)
            continue
        if checked:
            # Missing stays deleted. Do not fall through to bare file_sha (TOCTOU / hardlink).
            if not os.path.lexists(p):
                h = None
            else:
                raw = _checked_raw(p, ws.mirror)
                h = hashlib.sha256(raw).hexdigest() if raw is not None else None
        else:
            h = file_sha(p)
        if h is None:
            deleted.append(rel)
        elif h != info.get("mirror"):
            changed.append(rel)
    for cur, dirs, fs in os.walk(ws.mirror):
        dirs[:] = [d for d in dirs if not _path_excluded(os.path.join(cur, d), ws.mirror)]
        for f in fs:
            full = os.path.join(cur, f)
            if not _mirrored_name(f):
                continue
            rel = os.path.relpath(full, ws.mirror).replace("\\", "/")
            if not rel.startswith(prefixes):
                continue
            if _path_outside_project(full, rel, ws, project):
                continue
            if rel not in files and (f.lower().endswith(".tmdl") or f.lower().endswith(JSON_EXT)):
                added.append(rel)
    return {"changed": sorted(changed), "added": sorted(added), "deleted": sorted(deleted)}


def excluded_mirror_additions(ws: Workspace, project: Project) -> list[str]:
    """Mirrored-looking files sitting in a skipped directory or an alias of one.

    A blocked directory is reported as its mirror-relative path with a trailing slash.
    Children of that directory are not listed and are not walked: an alias must not
    leak names from outside the workspace. Direct children of the current directory
    that are themselves excluded are reported. Nothing inside a blocked alias is.
    """
    found = []
    prefixes = (project.name + ".SemanticModel/", project.name + ".Report/")
    for cur, dirs, files in os.walk(ws.mirror, followlinks=False):
        blocked = []
        for d in dirs:
            full = os.path.join(cur, d)
            rel = os.path.relpath(full, ws.mirror).replace("\\", "/")
            if not rel.endswith("/"):
                dir_rel = rel + "/"
            else:
                dir_rel = rel
            # Project-aware: a junction into another project under the same mirror is blocked.
            if _path_excluded(full, ws.mirror) or (
                    dir_rel.startswith(prefixes) and _path_outside_project(full, dir_rel.rstrip("/") or rel, ws, project)):
                blocked.append(d)
        dirs[:] = [d for d in dirs if d not in blocked]
        for d in blocked:
            rel = os.path.relpath(os.path.join(cur, d), ws.mirror).replace("\\", "/")
            if not rel.endswith("/"):
                rel += "/"
            if rel.startswith(prefixes):
                found.append(rel)
        for name in files:
            full = os.path.join(cur, name)
            if not _mirrored_name(name):
                continue
            rel = os.path.relpath(full, ws.mirror).replace("\\", "/")
            if not rel.startswith(prefixes):
                continue
            if _path_outside_project(full, rel, ws, project):
                found.append(rel)
    return sorted(set(found))


def _read_mine(path: str, root: str) -> tuple[str, bytes]:
    """Read one rebase input from the sanitized mirror."""
    return read_text_checked(path, root=root)


def _read_prepared(path: str, rel: str):
    """Read one pull input. A decode or IO failure names the mirror-relative path and nothing else."""
    try:
        return read_text(path)
    except (OSError, UnicodeError, ValueError):
        raise PbzError("REFUSED", rel) from None


def pull(ws: Workspace, project: Project, force: bool = False, rebase: bool = False) -> dict:
    # The mirror root and the guide are checked before recovery scans, mirror walks or pull mutations.
    anchor_mirror(ws)
    preflight_regular(os.path.join(ws.mirror, "MIRROR.md"), root=ws.mirror)
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
    # One capture: the bytes that are hashed are the bytes that are parsed and tokenized.
    # Owning source roots are validated against the workspace before enumeration/read.
    captured = []
    for rel, real in project_files(project, ws):
        if _path_excluded(real, ws.root) or _reparse_link(real):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        text, bom = _read_prepared(real, rel)
        raw = bom + text.encode("utf-8")
        captured.append((rel, text, bom, hashlib.sha256(raw).hexdigest()))
    prefix = project.name + ".SemanticModel/definition/"
    model_texts = {rel[len(prefix):]: text for rel, text, _bom, _real_h in captured
                   if rel.startswith(prefix) and rel.lower().endswith(".tmdl")}
    model = tmdl.load_model(os.path.join(project.model_dir, "definition"), model_texts)
    clf = Classifier(model, ws.private, pol)
    tm = tokens.TokenMap(ws.private)
    new_files, lossy, counts = {}, [], {}
    keep = set(pending["changed"] + pending["added"]) if rebase else set()
    conflicts = []
    bdir = base_dir(ws, project)
    # Phase 1 tokenizes that capture. Nothing is written here, so a later refusal leaves the
    # published mirror and base untouched. A malformed report file refuses the pull; it is not
    # published and it is not reported as PULLED.
    prepared = []
    for rel, text, bom, real_h in captured:
        try:
            mtext = _tokenize(rel, text, model, clf, tm)
        except PbzError as err:
            if err.code == "REFUSED":
                raise PbzError("REFUSED", rel) from None
            raise
        back, unknown = _detokenize(rel, mtext, tm)
        if back != text or unknown:
            lossy.append(rel)
            mtext = None
        old = man.get("files", {}).get(rel, {})
        item = {"rel": rel, "bom": bom, "mtext": mtext, "real_h": real_h, "old": old}
        if rel in keep and old.get("real") != real_h:
            mine = os.path.join(ws.mirror, *rel.split("/"))
            base = os.path.join(bdir, *rel.split("/"))
            # Rebase inputs are checked before any content read. An aliased tracked
            # path must not be opened as merge input.
            if _path_outside_project(mine, rel, ws, project):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            _must_stay(mine, ws.mirror)
            if os.path.lexists(base):
                _must_stay(base, ws.private)
            item["mine_text"], item["mine_bom"] = _read_mine(mine, ws.mirror)
            item["base_text"] = _read_prepared(base, rel)[0] if os.path.exists(base) else None
        prepared.append(item)
    # Preflight every planned mirror/base publish and stale-delete path before the first write,
    # so a later UNSAFE_PATH cannot leave earlier files already changed.
    planned = {item["rel"] for item in prepared}
    for item in prepared:
        rel, mtext = item["rel"], item["mtext"]
        if rel in keep:
            continue
        mpath = os.path.join(ws.mirror, *rel.split("/"))
        bpath = os.path.join(bdir, *rel.split("/"))
        if mtext is None:
            if os.path.exists(mpath):
                _must_stay(mpath, ws.mirror)
            continue
        _must_stay(mpath, ws.mirror)
        _must_stay(bpath, ws.private)
    for rel in man.get("files", {}):
        if rel in planned or rel in keep:
            continue
        stale = os.path.join(ws.mirror, *rel.split("/"))
        if os.path.lexists(stale):
            _must_stay(stale, ws.mirror)
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
                remove_file(mpath, root=ws.mirror)
            new_files[rel] = {"real": real_h, "mirror": None, "lossy": True}
            continue
        write_text(mpath, mtext, bom, root=ws.mirror)
        write_text(os.path.join(bdir, *rel.split("/")), mtext, bom, root=ws.private)
        new_files[rel] = {"real": real_h, "mirror": file_sha(mpath)}
        for t in tokens.TOKEN_RE.findall(mtext):
            lab = t[1:-1].rsplit("#", 1)[0]
            kind = "column" if "[" in lab else lab.split(":")[0]
            counts[kind] = counts.get(kind, 0) + 1
    for rel in man.get("files", {}):
        if rel not in new_files and rel not in keep:
            p = os.path.join(ws.mirror, *rel.split("/"))
            if os.path.exists(p):
                remove_file(p, root=ws.mirror)
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
    if patch:
        anchor_mirror(ws)
    ch = mirror_changes(ws, project, man, checked=patch)
    out = {"verb": "diff", "project": project.name, **ch}
    if patch:
        lines = []
        for rel in ch["changed"] + ch["added"]:
            a_path = os.path.join(base_dir(ws, project), *rel.split("/"))
            b_path = os.path.join(ws.mirror, *rel.split("/"))
            a_raw = _checked_raw(a_path, ws.private) if os.path.lexists(a_path) else b""
            b_raw = _checked_raw(b_path, ws.mirror)
            a = _split_bom(a_raw or b"")[0].splitlines(keepends=True)
            b = _split_bom(b_raw or b"")[0].splitlines(keepends=True)
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


def _must_stay(path: str, root: str | None) -> None:
    """Refuse a link or a junction below `root` before the path is read or deleted."""
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    _contained(path, root)


def _copy_atomic(source, target, *, root: str | None = None):
    _must_stay(target, root)
    with open(source, "rb") as f:
        payload = f.read()
    replace_bytes(target, payload, root=root)


def _begin_transaction(ws, project, stamp, targets, authorized_before: dict | None = None):
    directory = os.path.join(_transaction_dir(ws, project), stamp)
    # Refuse unsafe targets and journal destinations before any snapshot read or journal write.
    _must_stay(directory, ws.private)
    authorized_before = authorized_before or {}
    for scope, rel, _expected in targets:
        target = _transaction_path(ws, project, {"scope": scope, "path": rel})
        trust = ws.root if scope == "workspace" else ws.private
        if os.path.lexists(target) or scope == "workspace":
            _must_stay(target, trust)
    os.makedirs(directory, exist_ok=False)
    entries = []
    _journal_save(directory, {"version": 1, "state": "PREPARING", "entries": []})
    for index, (scope, rel, expected) in enumerate(targets):
        entry = {"scope": scope, "path": rel, "after": expected}
        target = _transaction_path(ws, project, entry)
        trust = ws.root if scope == "workspace" else ws.private
        _must_stay(target, trust)
        # Additions carry expected absence from before validation. A file created later is not
        # adopted as the authorized baseline and is not copied into the backup.
        if scope == "workspace" and rel in authorized_before:
            entry["before"] = authorized_before[rel]
        else:
            entry["before"] = file_sha(target)
        if entry["before"] is not None and file_sha(target) == entry["before"]:
            backup = os.path.join(directory, "before", str(index))
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            with open(target, "rb") as f:
                replace_bytes(backup, f.read(), root=ws.private)
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
        trust = ws.root if entry.get("scope") == "workspace" else ws.private
        _must_stay(target, trust)
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
            if os.path.lexists(target):
                trust = ws.root if entry.get("scope") == "workspace" else ws.private
                remove_file(target, root=trust)
        else:
            with open(os.path.join(directory, "before", entry["backup"]), "rb") as f:
                content = f.read()
            trust = ws.root if entry.get("scope") == "workspace" else ws.private
            replace_bytes(target, content, root=trust)
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
        remove_file(lock, root=os.path.dirname(lock))
    return {"verb": "recover", "result": "RECOVERY_REQUIRED" if failed
            else "RECOVERED" if results or stale_lock else "NOTHING_TO_RECOVER", "transactions": results}


def _push(ws: Workspace, project: Project, reload: bool = False, dry_run: bool = False,
         only: list[str] | None = None, preflight_only: bool = False) -> dict:
    # The push log is checked before any project mutation. A later append failure stays a warning.
    preflight_push_log(ws)
    pending = pending_recovery(ws, project)
    if pending:
        return {"verb": "push", "result": "RECOVERY_REQUIRED", "transactions": pending}
    man = load_manifest(ws, project)
    if not man:
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    anchor_mirror(ws)
    blocked = excluded_mirror_additions(ws, project)
    if blocked:
        return {"verb": "push", "project": project.name, "result": "REFUSED",
                "files": blocked, "hint": "files under excluded directories are not part of the model"}
    ch, held = _select(mirror_changes(ws, project, man, checked=True), only)
    if not any(ch.values()) and not preflight_only:
        return {"verb": "push", "project": project.name, "result": "NOTHING_TO_PUSH",
                **({"held_back": held} if held else {})}
    files = man.get("files", {})
    touched = ch["changed"] + ch["added"] + ch["deleted"]
    unsafe = [rel for rel in touched
              if _path_outside_project(os.path.join(ws.mirror, *rel.split("/")), rel, ws, project)]
    if unsafe:
        return {"verb": "push", "project": project.name, "result": "REFUSED", "files": sorted(unsafe),
                "hint": "files under excluded directories are not part of the model"}
    conflicts = []
    for rel in ch["changed"] + ch["deleted"]:
        real = _real_path(ws, rel)
        _must_stay(real, ws.root)
        if file_sha(real) != files[rel].get("real"):
            conflicts.append(rel)
        if files[rel].get("lossy"):
            conflicts.append(rel + " (not round-trippable, edit in Desktop)")
    for rel in ch["added"]:
        real = _real_path(ws, rel)
        _must_stay(real, ws.root)
        if os.path.exists(real):
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
        raw = _checked_raw(os.path.join(ws.mirror, *rel.split("/")), ws.mirror)
        if raw is None:
            return {"verb": "push", "project": project.name, "result": "CONFLICT",
                    "hint": "project files changed during validation; pull and retry"}
        mtext, bom = _split_bom(raw)
        captured_mirror[rel] = (mtext, bom)
        rtext, unknown = _detokenize(rel, mtext, tm)
        unknown_all += unknown
        new_real[rel] = (rtext, bom)
    if unknown_all:
        return {"verb": "push", "project": project.name, "result": "UNKNOWN_TOKEN",
                "tokens": sorted(set(unknown_all))}
    # Candidate bytes and the preconditions they were read against, before validation.
    # Added paths were authorized as absent. Do not hash a file that appears later.
    pre_real = {rel: file_sha(_real_path(ws, rel)) for rel in ch["changed"] + ch["deleted"]}
    pre_real.update({rel: None for rel in ch["added"]})
    pre_mirror = {rel: hashlib.sha256(bom + mtext.encode("utf-8")).hexdigest()
                  for rel, (mtext, bom) in captured_mirror.items()}
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
                remove_file(p, root=stage)
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
        images.install(imgs_new, os.path.join(stage, project.name + ".Report"), root=ws.private)
        images.delete(imgs_drop, os.path.join(stage, project.name + ".Report"), root=ws.private)
        findings = validate.run_stage(ws, project, stage, tm)
        if any(file_sha(_real_path(ws, rel)) != pre_real[rel] for rel in pre_real) or \
                any(_post_mirror_hash(ws, rel) != pre_mirror[rel] for rel in pre_mirror):
            return {"verb": "push", "project": project.name, "result": "CONFLICT",
                    "hint": "project files changed during validation; pull and retry"}
        errors = [f for f in findings if f["severity"] == "error"]
        summary = []
        for rel in touched:
            old = None
            bpath = os.path.join(base_dir(ws, project), *rel.split("/"))
            if os.path.exists(bpath):
                old = read_text(bpath)[0]
            # Changed/added text came from the checked capture. Deleted entries stay None —
            # never reopen the mirror (a recreated alias must not be read for the summary).
            new = captured_mirror[rel][0] if rel in captured_mirror else None
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
                                         [("private", p, h) for p, h in sorted(private_targets.items())],
                                         pre_real)
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
            _must_stay(real, ws.root)
            if not os.path.isfile(real):
                continue
            dst = os.path.join(backup, *rel.split("/"))
            _must_stay(dst, ws.private)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(real, "rb") as f:
                replace_bytes(dst, f.read(), root=ws.private)
        for rel, (rtext, bom) in new_real.items():
            write_text(_real_path(ws, rel), rtext, bom, root=ws.root)
        for rel in ch["deleted"]:
            remove_file(_real_path(ws, rel), root=ws.root)
            _prune_empty(_real_path(ws, rel), os.path.join(ws.root, rel.split("/")[0]))
        # Commit staged resources by atomic file replacement as well; directory replacement
        # could otherwise leave a partially copied package with an unrecognizable hash.
        for rel in sorted(resource_paths):
            target = _real_path(ws, rel)
            source = os.path.join(stage, *rel.split("/"))
            _must_stay(target, ws.root)
            if os.path.isfile(source):
                _copy_atomic(source, target, root=ws.root)
            elif os.path.lexists(target):
                remove_file(target, root=ws.root)
                _prune_empty(target, project.report_dir)
        installed, img_in, img_out = sorted(pkgs), sorted(imgs_new), sorted(imgs_drop)
        for rel in ch["changed"] + ch["added"]:
            mtext, bom = captured_mirror[rel]
            mirror_hash = hashlib.sha256(bom + mtext.encode("utf-8")).hexdigest()
            files[rel] = {"real": real_targets[rel], "mirror": mirror_hash}
            write_text(os.path.join(base_dir(ws, project), *rel.split("/")), mtext, bom, root=ws.private)
        for rel in ch["deleted"]:
            files.pop(rel, None)
            bp = os.path.join(base_dir(ws, project), *rel.split("/"))
            if os.path.exists(bp):
                remove_file(bp, root=ws.private)
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
    preflight_push_log(ws)
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
        remove_file(lock, root=directory)


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
        commit_bytes(ops, root=ws.root)
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
    commit_bytes([("write", path, new, bom)], root=ws.root)
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
        # The project tree itself must sit inside the trusted mirror; an aliased project root
        # must not become the canonical boundary for history.
        if _path_excluded(base, ws.mirror) or _reparse_link(base):
            continue
        top = os.path.basename(base)
        for cur, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [d for d in dirs if not _path_excluded(os.path.join(cur, d), base)
                        and not _path_excluded(os.path.join(cur, d), ws.mirror)]
            for f in files:
                full = os.path.join(cur, f)
                if _path_excluded(full, base) or _path_excluded(full, ws.mirror):
                    continue
                low = f.lower()
                if low.endswith(".tmdl") or low.endswith(JSON_EXT):
                    rel = f"{top}/" + os.path.relpath(full, base).replace("\\", "/")
                    out[rel] = read_text(full)[0]
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
            remove_file(os.path.join(snaps, old), root=snaps)
        for b in os.listdir(blobs):
            if b[:-4] not in live:
                remove_file(os.path.join(blobs, b), root=blobs)
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


def preflight_push_log(ws: Workspace) -> str:
    """Refuse an aliased or hardlinked push log before push mutates the project. No content is read."""
    path = pbz_state_path(ws, "push-log.jsonl")
    preflight_regular(path, root=ws.root)
    return path


def _log_push(ws: Workspace, project: Project, stamp: str, summary: list[str]) -> None:
    path = pbz_state_path(ws, "push-log.jsonl")
    line = json.dumps({"stamp": stamp, "project": project.name, "objects": summary}) + "\n"
    append_checked(path, line, root=ws.root)


def _write_guide(ws: Workspace, mode: str) -> None:
    anchor_mirror(ws)
    path = os.path.join(ws.mirror, "MIRROR.md")
    preflight_regular(path, root=ws.mirror)
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
    write_text(path, text, root=ws.mirror)
