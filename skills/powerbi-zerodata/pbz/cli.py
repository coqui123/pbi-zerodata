"""pbz: data-blind Power BI PBIP toolkit. Every command prints metadata or closed-vocabulary verdicts only."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "pbz"

from pbz import checks, describe, desktop, dpapi, handoff, memscrub, mirror, policy, rename, scan, smoke, validate  # noqa: E402
from pbz.common import PbzError, emit, engine_call, find_workspace  # noqa: E402


def _spec(arg: str | None) -> dict | None:
    if not arg:
        return None
    if os.path.exists(arg):
        with open(arg, encoding="utf-8-sig") as f:
            return json.load(f)
    return json.loads(arg)


def _is_human() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("CLAUDECODE") \
        and not os.environ.get("CODEX_SANDBOX") and not os.environ.get("CODEX_MANAGED_BY_NPM")


def cmd_status(ws, project, a):
    man = mirror.load_manifest(ws, project)
    out = {"verb": "status", "workspace": ws.root, "project": project.name,
           "mirror": "present" if man else "missing (run pbz pull)",
           "classification": man.get("classification") if man else None,
           "index": os.path.exists(os.path.join(ws.private, "index.json")),
           "policy": os.path.exists(ws.policy_path),
           "engine": "available" if engine_call("ping", {"private_dir": ws.private}).get("ok") else "unavailable"}
    if man:
        out["pending"] = mirror.mirror_changes(ws, project, man)
    out["desktop"] = desktop.status(project)
    settings = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(settings, encoding="utf-8") as f:
            out["guard_claude"] = "powerbi-zerodata" in f.read()
    except OSError:
        out["guard_claude"] = False
    return out


def cmd_init(ws, project, a):
    os.makedirs(ws.pbz_dir, exist_ok=True)
    if not os.path.exists(ws.policy_path):
        policy.save(ws, policy.load(ws))
    dpapi.load_key(ws.private)
    sc = scan.scan(ws, apply=True)
    hosts = set()
    model = mirror.real_model(project)
    for t in model.tables.values():
        for _n, _k, expr in t.partitions:
            hosts |= set(re.findall(r"(?i)https?://([a-z0-9.-]+)", expr))
    for doc in model.docs.values():
        hosts |= set(re.findall(r"(?i)https?://([a-z0-9.-]+)", doc.text))
    hosts = sorted(h.lower() for h in hosts if not h.lower().endswith(("microsoft.com", "w3.org", "schema.org")))
    added = policy.tighten(ws, sensitive_hosts=hosts)
    res = {"verb": "init", "policy": os.path.relpath(ws.policy_path, ws.root), "scan": sc["added"],
           "sensitive_hosts": added["sensitive_hosts"]}
    if not mirror.load_manifest(ws, project):
        res["pull"] = mirror.pull(ws, project)
    return res


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # tokens and arrows must survive cp1252 consoles
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="pbz", description=__doc__)
    ap.add_argument("--project", help="project name when the workspace holds several .pbip files")
    ap.add_argument("--workspace", help="workspace root (default: discover from the current folder)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("init")
    p = sub.add_parser("scan")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("pull")
    p.add_argument("--force", action="store_true", help="discard unpushed mirror edits")
    p.add_argument("--rebase", action="store_true", help="merge unpushed mirror edits onto the new real files")
    p = sub.add_parser("diff")
    p.add_argument("--patch", action="store_true")
    p = sub.add_parser("push")
    p.add_argument("--reload", action="store_true", help="report-only change: reload the open Desktop")
    p.add_argument("--dry-run", action="store_true", help="validate only")
    p = sub.add_parser("rename")
    p.add_argument("kind", choices=["measure", "column"])
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--table")
    p = sub.add_parser("describe")
    p.add_argument("target", nargs="?", help="table name, [Measure] or 'Table'[Column]; omit for a model overview")
    p = sub.add_parser("values")
    p.add_argument("column", help="'Table'[Column]")
    sub.add_parser("validate")
    p = sub.add_parser("desktop")
    p.add_argument("action", choices=["status", "open", "close", "reload"])
    p.add_argument("--wait", type=int, default=240)
    sub.add_parser("index")
    p = sub.add_parser("refresh")
    p.add_argument("--tables", nargs="*")
    sub.add_parser("health")
    p = sub.add_parser("check")
    p.add_argument("spec", help="path to a checks JSON file, or inline JSON")
    p = sub.add_parser("probe")
    p.add_argument("table")
    p.add_argument("--columns", nargs="*")
    p = sub.add_parser("snapshot")
    p.add_argument("action", choices=["save", "compare"])
    p.add_argument("name")
    p.add_argument("--spec", help="queries JSON file (compare reuses the saved spec when omitted)")
    p = sub.add_parser("smoke")
    p.add_argument("--page")
    p = sub.add_parser("handoff")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("memscrub")
    p.add_argument("--dir", required=True, help="memory folder to scrub")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--show", help="print the scrubbed text of one file")
    p = sub.add_parser("admin")
    p.add_argument("action", choices=["budget-reset", "allow", "category", "synthetic", "log"])
    p.add_argument("value", nargs="?")
    a = ap.parse_args(argv)
    try:
        ws = find_workspace(a.workspace)
        project = ws.project(a.project) if a.cmd not in ("scan", "admin", "memscrub") or ws.projects else None
        if a.cmd == "status":
            out = cmd_status(ws, project, a)
        elif a.cmd == "init":
            out = cmd_init(ws, project, a)
        elif a.cmd == "scan":
            out = scan.scan(ws, apply=not a.dry_run)
        elif a.cmd == "pull":
            out = mirror.pull(ws, project, force=a.force, rebase=a.rebase)
        elif a.cmd == "diff":
            out = mirror.diff(ws, project, patch=a.patch)
        elif a.cmd == "push":
            out = mirror.push(ws, project, reload=a.reload, dry_run=a.dry_run)
        elif a.cmd == "rename":
            out = rename.rename(ws, project, a.kind, a.old, a.new, a.table)
        elif a.cmd == "describe":
            out = describe.describe(ws, project, a.target)
        elif a.cmd == "values":
            out = describe.values(ws, project, a.column)
        elif a.cmd == "validate":
            out = {"verb": "validate", "mode": "mirror (metadata only; push runs the full staged validation)",
                   "findings": validate.run_mirror(ws, project)}
        elif a.cmd == "desktop":
            fn = {"status": desktop.status, "open": lambda p: desktop.open_(p, a.wait), "close": desktop.close,
                  "reload": lambda p: {"verb": "desktop", "reload": desktop.reload(p)}}[a.action]
            out = fn(project)
        elif a.cmd == "index":
            out = checks.index(ws, project)
        elif a.cmd == "refresh":
            out = checks.refresh(ws, project, a.tables)
        elif a.cmd == "health":
            out = checks.health(ws, project)
        elif a.cmd == "check":
            out = checks.run_checks(ws, project, _spec(a.spec) or {})
        elif a.cmd == "probe":
            out = checks.probe(ws, project, a.table, a.columns)
        elif a.cmd == "snapshot":
            spec = _spec(a.spec) or (checks.saved_spec(ws, a.name) if a.action == "compare" else None)
            if not spec:
                raise PbzError("BAD_REQUEST", "--spec required")
            out = checks.snapshot(ws, project, a.action, a.name, spec)
        elif a.cmd == "smoke":
            out = smoke.run(ws, project, a.page)
        elif a.cmd == "handoff":
            out = handoff.build(ws, project, a.all)
        elif a.cmd == "memscrub":
            out = memscrub.run(ws, project, a.dir, apply=a.apply, show=a.show)
        elif a.cmd == "admin":
            out = admin(ws, a)
        else:
            raise PbzError("BAD_REQUEST", a.cmd)
    except PbzError as e:
        emit({"ok": False, "error": e.code, "detail": e.detail})
        return 2
    emit(out)
    return 0


def admin(ws, a) -> dict:
    if not _is_human():
        raise PbzError("HUMAN_ONLY", "run `pbz admin` yourself in a terminal")
    if a.action == "budget-reset":
        p = os.path.join(ws.private, "state.json")
        if os.path.exists(p):
            os.remove(p)
        return {"verb": "admin", "budget": "reset"}
    pol = policy.load(ws)
    if a.action == "allow" and a.value:
        pol["allow"] = sorted(set(pol.get("allow", [])) | {a.value})
    elif a.action == "category" and a.value:
        pol["category_columns"] = sorted(set(pol.get("category_columns", [])) | {a.value})
    elif a.action == "synthetic" and a.value in ("on", "off"):
        pol["synthetic"] = a.value == "on"
    elif a.action == "log":
        n = int(a.value or 20)
        path = os.path.join(ws.private, "logs", "engine.log")
        lines = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f.readlines()[-n:]:
                    lid, _, blob = line.rstrip("\n").partition("\t")
                    import base64
                    lines.append(f"{lid}\t{dpapi.unprotect(base64.b64decode(blob)).decode('utf-8', 'replace')}")
        except OSError:
            pass
        print("\n".join(lines) or "(empty)")
        return {"verb": "admin", "log": len(lines)}
    else:
        raise PbzError("BAD_REQUEST", "missing value")
    policy.save(ws, pol)
    return {"verb": "admin", "policy": "updated", "action": a.action}


if __name__ == "__main__":
    sys.exit(main())
