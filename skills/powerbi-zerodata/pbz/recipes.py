"""`pbz recipe run <file.json>`: a reviewable batch of mirror edits, applied as one transaction.

A recipe is data, not code: a list of pbz verb calls with typed parameters, per-step expectations
and limits. Only verbs that edit the mirror are allowed (no push, pull, engine, Desktop, consumers,
admin). Every step runs through the normal CLI, then the whole result is validated; any failed step,
unmet expectation or validation error restores the mirror to its state before the recipe.

    {"recipe": "kpi-folders", "version": 1,
     "params": {"table": "Sales"},
     "steps": [
       {"verb": "set", "args": ["--table", "{table}", "--kind", "measure", "displayFolder=KPIs"],
        "expect": {"result": "CHANGED"}},
       {"verb": "hierarchy", "args": ["add", "Date", "Calendar", "--level", "Year", "--level", "Month"],
        "on_error": {"NAME_TAKEN": "skip"}}
     ],
     "limits": {"max_files": 40}}
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat

from .common import PbzError, _reparse_link, anchor_mirror, commit_bytes, preflight_regular, read_checked

ALLOWED = {"set", "rename", "move", "new", "relationship", "add", "style", "filter", "interaction", "link",
           "bookmark", "copy", "hierarchy", "format-expr", "function", "culture", "translate", "perspective", "role",
           "refresh-policy", "format", "repair", "bpa", "a11y", "mobile", "validate", "diff", "layout", "get",
           "unused", "lineage", "describe", "image"}


def _gather(base: str) -> list[str]:
    """Regular files under `base`. A junction or hard link is refused before it is opened or entered."""
    if not os.path.lexists(base):
        return []
    if _reparse_link(base):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    found: list[str] = []
    stack = [base]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as scan:
                entries = list(scan)
        except OSError:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
        for entry in entries:
            path = entry.path
            if _reparse_link(path):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            try:
                info = os.lstat(path)
            except OSError:
                raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                if getattr(info, "st_nlink", 1) != 1:
                    raise PbzError("UNSAFE_PATH", "refusing to write through a link")
                found.append(path)
            else:
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    return found


def _snapshot(ws, project) -> dict[str, bytes]:
    """Anchor the mirror, refuse aliases, then read. Listing and preflight happen before any content read."""
    root = anchor_mirror(ws)
    paths: list[str] = []
    for base in (project.mirror_model, project.mirror_report):
        paths.extend(_gather(base))
    for path in paths:
        preflight_regular(path, root=root)
    snap = {}
    for path in paths:
        snap[os.path.relpath(path, root)] = read_checked(path, root=root)
    return snap


def _restore(ws, project, snap: dict[str, bytes]) -> int:
    try:
        now = _snapshot(ws, project)
    except PbzError:
        raise PbzError("RECOVERY_REQUIRED", "the recipe could not be restored") from None
    ops = []
    for rel, data in snap.items():
        if now.get(rel) != data:
            ops.append(("write_bytes", os.path.join(ws.mirror, rel), data))
    for rel in set(now) - set(snap):
        ops.append(("delete", os.path.join(ws.mirror, rel)))
    if not ops:
        return 0
    try:
        commit_bytes(ops, root=ws.mirror)
    except PbzError:
        raise PbzError("RECOVERY_REQUIRED", "the recipe could not be restored") from None
    return len(ops)


def _subst(arg: str, params: dict) -> str:
    def rep(m):
        k = m.group(1)
        if k not in params:
            raise PbzError("BAD_REQUEST", f"recipe parameter {{{k}}} has no value (pass --param {k}=...)")
        return str(params[k])
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", rep, arg)


def _workspace_option(arg) -> bool:
    """True for a flag argparse can take as --workspace or --project, including short and abbreviated forms."""
    text = str(arg).split("=", 1)[0]
    if text.startswith(("--workspace", "--project")) or text in ("-w", "-p"):
        return True
    if text.startswith("--") and len(text) > 2:
        body = text[2:]
        return bool(body) and ("workspace".startswith(body) or "project".startswith(body))
    return False


def load(spec: str, *, root: str | None = None) -> dict:
    if not isinstance(spec, str):
        raise PbzError("BAD_REQUEST", "a recipe is JSON")
    if spec.lstrip().startswith("{") or os.path.lexists(spec):
        from . import control
        r = control.load_spec(spec, root=root)
    else:
        raise PbzError("NOT_FOUND", "recipe file")
    if not isinstance(r, dict) or not isinstance(r.get("steps"), list) or not r["steps"]:
        raise PbzError("BAD_REQUEST", "a recipe needs a non-empty steps list")
    for i, s in enumerate(r["steps"], 1):
        if not isinstance(s, dict) or s.get("verb") not in ALLOWED:
            raise PbzError("BAD_REQUEST", f"step {i}: verb must be one of the mirror-editing verbs ({', '.join(sorted(ALLOWED))})")
        if not isinstance(s.get("args", []), list) or not all(isinstance(a, (str, int, float)) for a in s.get("args", [])):
            raise PbzError("BAD_REQUEST", f"step {i}: args is a list of strings")
        if any(_workspace_option(a) for a in s.get("args", [])):
            raise PbzError("BAD_REQUEST", f"step {i}: a recipe works on the current workspace and project only")
    return r


def _flag(args: list[str], name: str) -> bool:
    return any(a == name or a.startswith(name + "=") for a in args)


def _forbidden(verb: str, args: list[str]) -> bool:
    if verb == "image":
        return True
    if verb == "unused" and (_flag(args, "--remove") or _flag(args, "--verify-engine")):
        return True
    return False


def _prepare(ws, project, steps: list, params: dict) -> list[tuple[dict, list[str]]]:
    """Substitute and parse every step before any step runs. Parser text stays local."""
    from . import cli
    parser = cli.argument_parser()
    ready = []
    for i, s in enumerate(steps, 1):
        try:
            args = [_subst(str(a), params) for a in s.get("args", [])]
        except PbzError as e:
            raise PbzError(e.code, f"step {i}") from None
        if any(_workspace_option(a) for a in args):
            raise PbzError("BAD_REQUEST", f"step {i}: a recipe works on the current workspace and project only")
        if _forbidden(s["verb"], args):
            raise PbzError("BAD_REQUEST", f"step {i}: that action is not allowed in a recipe")
        argv = ["--workspace", ws.root, "--project", project.name, s["verb"], *args]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(argv)
        except (SystemExit, Exception):
            raise PbzError("BAD_REQUEST", f"step {i}: arguments are not valid") from None
        ready.append((s, args))
    return ready


def _call(ws, project, verb: str, args: list[str]) -> tuple[int, dict]:
    from . import cli
    argv = ["--workspace", ws.root, "--project", project.name, verb, *args]
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except SystemExit:
            return 2, {"error": "BAD_REQUEST"}
    text = buf.getvalue().strip()
    try:
        out = json.loads(text) if text else {}
    except ValueError:
        out = {"error": "BAD_REQUEST"}
    if not isinstance(out, dict):
        out = {"error": "BAD_REQUEST"}
    out.pop("raw", None)
    return rc, out


def run(ws, project, spec: str, params: dict | None = None, dry_run: bool = False) -> dict:
    from . import cli, validate
    r = load(spec, root=ws.root)
    params = {**(r.get("params") or {}), **(params or {})}
    limits = r.get("limits") or {}
    resolved = _prepare(ws, project, r["steps"], params)
    snap = _snapshot(ws, project)
    steps, failed, findings = [], None, []
    changed, removed = [], []
    prev = cli.suppress_evidence
    cli.suppress_evidence = True
    try:
        # Leftover empty folders from an earlier rollback are not this recipe's fault.
        baseline = {(f.get("code"), f.get("where")) for f in validate.run_mirror(ws, project)
                    if f.get("severity") == "error"}
        for i, (s, args) in enumerate(resolved, 1):
            try:
                rc, out = _call(ws, project, s["verb"], args)
            except Exception:
                failed = f"step {i}"
                steps.append({"step": i, "verb": s["verb"], "error": "BAD_REQUEST"})
                break
            rec = {"step": i, "verb": s["verb"], "rc": rc}
            for k in ("result", "action", "error"):
                if k in out and not isinstance(out[k], (list, dict)):
                    rec[k] = out[k]
            if "changed" in out:
                rec["changed"] = len(out["changed"]) if isinstance(out["changed"], list) else out["changed"]
            if rc != 0:
                policy = (s.get("on_error") or {}).get(out.get("error", ""), "fail")
                if policy == "skip":
                    rec["skipped"] = True
                    steps.append(rec)
                    continue
                failed = f"step {i} ({s['verb']}) failed: {out.get('error') or 'BAD_REQUEST'}"
                steps.append(rec)
                break
            for k, want in (s.get("expect") or {}).items():
                if out.get(k) != want:
                    failed = f"step {i} ({s['verb']}) expected {k}={want}"
                    break
            steps.append(rec)
            if failed:
                break
        if not failed:
            try:
                now = _snapshot(ws, project)
            except PbzError:
                # A link planted while a step ran cannot be rolled back by walking it.
                raise PbzError("RECOVERY_REQUIRED", "the recipe could not be restored") from None
            changed = sorted(rel.replace("\\", "/") for rel, data in now.items() if snap.get(rel) != data)
            removed = sorted(rel.replace("\\", "/") for rel in set(snap) - set(now))
            if limits.get("max_files") is not None and len(changed) + len(removed) > int(limits["max_files"]):
                failed = f"the recipe touched {len(changed) + len(removed)} files, more than its limit of {limits['max_files']}"
            else:
                findings = [f for f in validate.run_mirror(ws, project) if f.get("severity") == "error"
                            and (f.get("code"), f.get("where")) not in baseline]
                if findings:
                    failed = f"validation failed after the recipe: {len(findings)} error(s)"
    except PbzError as e:
        if e.code == "RECOVERY_REQUIRED":
            raise
        failed = failed or "the recipe failed"
    except Exception:
        failed = failed or "the recipe failed"
    finally:
        cli.suppress_evidence = prev
    out = {"verb": "recipe", "recipe": r.get("recipe", "unnamed"), "version": r.get("version"), "steps": steps,
           "files_changed": changed[:200], "files_removed": removed[:50]}
    if failed or dry_run:
        restored = _restore(ws, project, snap)
        out.update(result="ROLLED_BACK" if failed else "DRY_RUN", restored_files=restored,
                   files_changed=[], files_removed=[])
        if failed:
            out["reason"] = failed
            if findings:
                out["findings"] = findings[:20]
        return out
    out.update(result="APPLIED", next="pbz review, pbz diff, then pbz push")
    return out
