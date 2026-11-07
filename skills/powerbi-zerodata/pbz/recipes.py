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
import shutil

from .common import PbzError

ALLOWED = {"set", "rename", "move", "new", "relationship", "add", "style", "filter", "interaction", "link",
           "bookmark", "copy", "hierarchy", "format-expr", "function", "culture", "translate", "perspective", "role",
           "refresh-policy", "format", "repair", "bpa", "a11y", "mobile", "validate", "diff", "layout", "get",
           "unused", "lineage", "describe", "image"}


def _snapshot(ws, project) -> dict[str, bytes]:
    snap = {}
    for base in (project.mirror_model, project.mirror_report):
        for cur, _dirs, files in os.walk(base):
            for f in files:
                p = os.path.join(cur, f)
                with open(p, "rb") as h:
                    snap[os.path.relpath(p, ws.mirror)] = h.read()
    return snap


def _restore(ws, project, snap: dict[str, bytes]) -> int:
    now = _snapshot(ws, project)
    n = 0
    for rel, data in snap.items():
        if now.get(rel) != data:
            p = os.path.join(ws.mirror, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as h:
                h.write(data)
            n += 1
    for rel in set(now) - set(snap):
        os.remove(os.path.join(ws.mirror, rel))
        n += 1
    for base in (project.mirror_model, project.mirror_report):  # drop folders a step created
        for cur, dirs, files in sorted(os.walk(base), key=lambda x: -len(x[0])):
            if cur != base and not os.listdir(cur):
                shutil.rmtree(cur, ignore_errors=True)
    return n


def _subst(arg: str, params: dict) -> str:
    def rep(m):
        k = m.group(1)
        if k not in params:
            raise PbzError("BAD_REQUEST", f"recipe parameter {{{k}}} has no value (pass --param {k}=...)")
        return str(params[k])
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", rep, arg)


def load(spec: str) -> dict:
    text = spec
    if not spec.lstrip().startswith("{"):
        if not os.path.isfile(spec):
            raise PbzError("NOT_FOUND", "recipe file")
        with open(spec, encoding="utf-8-sig") as f:
            text = f.read()
    try:
        r = json.loads(text)
    except ValueError:
        raise PbzError("BAD_REQUEST", "a recipe is JSON")
    if not isinstance(r, dict) or not isinstance(r.get("steps"), list) or not r["steps"]:
        raise PbzError("BAD_REQUEST", "a recipe needs a non-empty steps list")
    for i, s in enumerate(r["steps"], 1):
        if not isinstance(s, dict) or s.get("verb") not in ALLOWED:
            raise PbzError("BAD_REQUEST", f"step {i}: verb must be one of the mirror-editing verbs ({', '.join(sorted(ALLOWED))})")
        if not isinstance(s.get("args", []), list) or not all(isinstance(a, (str, int, float)) for a in s.get("args", [])):
            raise PbzError("BAD_REQUEST", f"step {i}: args is a list of strings")
        if any(str(a).startswith(("--workspace", "--project")) for a in s.get("args", [])):
            raise PbzError("BAD_REQUEST", f"step {i}: a recipe works on the current workspace and project only")
    return r


def _call(ws, project, verb: str, args: list[str]) -> tuple[int, dict]:
    from . import cli
    argv = ["--workspace", ws.root, "--project", project.name, verb, *args]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    text = buf.getvalue().strip()
    try:
        out = json.loads(text) if text else {}
    except ValueError:
        out = {"raw": text[-300:]}
    return rc, out if isinstance(out, dict) else {"value": out}


def run(ws, project, spec: str, params: dict | None = None, dry_run: bool = False) -> dict:
    from . import validate
    r = load(spec)
    params = {**(r.get("params") or {}), **(params or {})}
    limits = r.get("limits") or {}
    snap = _snapshot(ws, project)
    steps, failed = [], None
    for i, s in enumerate(r["steps"], 1):
        args = [_subst(str(a), params) for a in s.get("args", [])]
        rc, out = _call(ws, project, s["verb"], args)
        rec = {"step": i, "verb": s["verb"], "rc": rc}
        for k in ("result", "action", "changed", "error", "detail"):
            if k in out:
                rec[k] = out[k] if k != "changed" or not isinstance(out[k], list) else len(out[k])
        if rc != 0:
            policy = (s.get("on_error") or {}).get(out.get("error", ""), "fail")
            if policy == "skip":
                rec["skipped"] = True
                steps.append(rec)
                continue
            failed = f"step {i} ({s['verb']}) failed: {out.get('error')}"
            steps.append(rec)
            break
        for k, want in (s.get("expect") or {}).items():
            if out.get(k) != want:
                failed = f"step {i} ({s['verb']}) expected {k}={want}, got {out.get(k)}"
                break
        steps.append(rec)
        if failed:
            break
    changed = sorted(rel.replace("\\", "/") for rel, data in _snapshot(ws, project).items() if snap.get(rel) != data)
    removed = sorted(rel.replace("\\", "/") for rel in set(snap) - set(_snapshot(ws, project)))
    if not failed and limits.get("max_files") is not None and len(changed) + len(removed) > int(limits["max_files"]):
        failed = f"the recipe touched {len(changed) + len(removed)} files, more than its limit of {limits['max_files']}"
    findings = [] if failed else [f for f in validate.run_mirror(ws, project) if f.get("severity") == "error"]
    if findings and not failed:
        failed = f"validation failed after the recipe: {len(findings)} error(s)"
    out = {"verb": "recipe", "recipe": r.get("recipe", "unnamed"), "version": r.get("version"), "steps": steps,
           "files_changed": changed[:200], "files_removed": removed[:50]}
    if failed or dry_run:
        restored = _restore(ws, project, snap)
        out.update(result="ROLLED_BACK" if failed else "DRY_RUN", restored_files=restored)
        if failed:
            out["reason"] = failed
            if findings:
                out["findings"] = findings[:20]
        return out
    out.update(result="APPLIED", next="pbz review, pbz diff, then pbz push")
    return out
