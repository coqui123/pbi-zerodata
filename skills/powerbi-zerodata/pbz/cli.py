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
from pbz.common import PbzError, emit, engine_call, file_sha, find_workspace  # noqa: E402


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
        # real files saved behind the mirror's back (Desktop save, another tool): pull --rebase before pushing
        behind = sorted(rel for rel, info in man.get("files", {}).items()
                        if file_sha(os.path.join(ws.root, *rel.split("/"))) != info.get("real"))
        out["real_changed_since_pull"] = behind[:50]
        out["sync"] = ("CONFLICT_RISK" if behind and any(out["pending"].values()) else
                       "BEHIND" if behind else "PENDING_PUSH" if any(out["pending"].values()) else "IN_SYNC")
    out["desktop"] = desktop.status(project)
    if out["desktop"].get("open") and out["desktop"].get("engine") == "READY":
        res = engine_call("health", {"private_dir": ws.private, "port": desktop.require_port(project)}, timeout=300)
        if res.get("ok"):
            real = mirror.real_model(project)
            files = {"tables": len([t for t in real.tables.values() if not t.private]),
                     "measures": len(real.all_measures())}
            loaded = {k: (res.get("objects") or {}).get(k) for k in files}
            out["desktop"]["loaded_matches_files"] = loaded == files
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
    p.add_argument("--only", nargs="+", metavar="PATH", help="push only these mirror files (globs or folder prefixes)")
    p = sub.add_parser("revert", help="discard mirror edits: files back to the last pull / push, or one object")
    p.add_argument("paths", nargs="*", help="mirror files (globs, folder prefixes); default all")
    p.add_argument("--object", help="one TMDL object: measure 'Table'[Name] (also column, hierarchy, partition, calculationItem)")
    p = sub.add_parser("rename")
    p.add_argument("kind", choices=["measure", "column", "table"])
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
    p = sub.add_parser("unused", help="usage state of every object (used / used_external / locked / used_by_unused / unused / unknown)")
    p.add_argument("--report", action="append", default=[], help="extra *.Report folder (thin report) to scan")
    p.add_argument("--all", action="store_true", help="also list used objects and auto date/time tables")
    p.add_argument("--kinds", nargs="*", help="limit to object kinds (measure column table ...)")
    p.add_argument("--impact", action="store_true", help="add cardinality / row buckets from `pbz index`")
    p.add_argument("--remove", action="store_true", help="delete unused objects from the mirror")
    p.add_argument("--cascade", action="store_true", help="with --remove: repeat until nothing new is unused")
    p.add_argument("--only", nargs="*", help="with --remove: only these kinds (bookmark must be named here)")
    p.add_argument("--m-step", default="remove", choices=["remove", "keep", "none"],
                   help="with --remove: how removed import columns leave the Power Query query")
    p.add_argument("--dry-run", action="store_true", help="with --remove: list what would go, change nothing")
    p.add_argument("--restore", metavar="STAMP", help="undo a removal (restore point printed by --remove)")
    p.add_argument("--verify-engine", action="store_true", help="cross-check dependencies with the open Desktop engine")
    p = sub.add_parser("lineage", help="dependency tree of one object")
    p.add_argument("target", help="'Table'[Column], [Measure], Table or a bare name")
    p.add_argument("--up", action="store_true", help="what it depends on (default: what depends on it)")
    p.add_argument("--depth", type=int, default=6)
    p.add_argument("--report", action="append", default=[])
    p = sub.add_parser("suggest", help="cleanup and optimisation suggestions")
    p.add_argument("--report", action="append", default=[])
    p = sub.add_parser("bpa", help="best practice analysis with scores and grades")
    p.add_argument("scope", nargs="?", default="all", choices=["model", "report", "all"])
    p.add_argument("--rules", help="JSON overlay: rule ids to disable / severities / extra settings")
    p.add_argument("--opinion", action="store_true", help="include opinion rules (naming and format conventions)")
    p.add_argument("--labels", default="letters", choices=["letters", "measure-killer"])
    p.add_argument("--report", action="append", default=[])
    p.add_argument("--fix", action="append", default=[], metavar="RULE",
                   help="apply a rule's property-only fix to the mirror (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="with --fix: list what would change")
    p = sub.add_parser("similar", help="duplicate and near-duplicate measures")
    p.add_argument("--threshold", type=float, default=0.80)
    p.add_argument("--max", type=int, default=100)
    p = sub.add_parser("changes", help="semantic diff of mirror snapshots (what changed, by object)")
    p.add_argument("--from", dest="from_", default="last", help="last (latest snapshot), previous, or a stamp")
    p.add_argument("--to", default="mirror", help="mirror (current files), last, or a stamp")
    p.add_argument("--list", action="store_true", help="list snapshots")
    p = sub.add_parser("docs", help="documentation of the model and reports from the mirror")
    p.add_argument("--format", default="md", choices=["md", "html", "json"])
    p.add_argument("--dax", action="store_true", help="include DAX and M expressions")
    p = sub.add_parser("m-lineage", help="Power Query step and column lineage")
    p.add_argument("table", nargs="?")
    p = sub.add_parser("new", help="scaffold a measure table, date table, calculation group or field parameter")
    p.add_argument("kind", choices=["measure-table", "date-table", "calc-group", "field-parameter"])
    p.add_argument("name", help="new table name")
    p.add_argument("--start", type=int, help="date-table: first year")
    p.add_argument("--end", type=int, help="date-table: last year")
    p.add_argument("--relate", action="append", help="date-table: fact date column to relate, 'T'[C] (repeatable)")
    p.add_argument("--date", help="calc-group: the date column, 'Date'[Date]")
    p.add_argument("--items", nargs="*", help="calc-group items: Current MTD QTD YTD PY \"PY YTD\" YoY \"YoY %%\"")
    p.add_argument("--precedence", type=int)
    p.add_argument("--fields", nargs="*", help="field-parameter: [Measure] or 'Table'[Column] list")
    p.add_argument("--labels", nargs="*", help="field-parameter: display labels, one per field")
    p = sub.add_parser("add", help="create a report page or a visual from a built-in template")
    p.add_argument("kind", choices=["page", "visual"])
    p.add_argument("name", help="page: display name; visual: visual type (card, tableEx, pivotTable, slicer, "
                                "clusteredColumnChart, lineChart, textbox, ...)")
    p.add_argument("--page", help="visual: target page (display name or id)")
    p.add_argument("--field", action="append", default=[], help="visual: [Measure], 'T'[Column] or ROLE='T'[Column]")
    p.add_argument("--pos", help="visual: x,y,width,height")
    p.add_argument("--title", help="visual: title text")
    p.add_argument("--text", help="textbox: text")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--hidden", action="store_true", help="page: hidden in view mode")
    p.add_argument("--after", help="page: insert after this page")
    p = sub.add_parser("style", help="visual formatting (titles, labels, colors, fonts, axes, legends, ...) checked "
                                     "against Microsoft's formatting catalog")
    p.add_argument("page", nargs="?", help="page display name or id")
    p.add_argument("visual", nargs="?", help="visual id or its `pbz layout` label")
    p.add_argument("--set", dest="sets", action="append", default=[],
                   help="[container.]object.property=value (repeatable); colors #RRGGBB, theme:<n>[:<shade %%>], "
                        "measure:[Measure]")
    p.add_argument("--unset", dest="unsets", action="append", default=[], help="[container.]object.property (repeatable)")
    p.add_argument("--type", dest="vtype", help="every visual of this type (on the page, or in the whole report)")
    p.add_argument("--all", action="store_true", help="every visual on the page")
    p.add_argument("--series", help="format one series of the visual: [Measure] or 'T'[Column] it shows")
    p.add_argument("--state", help="instance state of the object (hover, selected, press, ...)")
    p.add_argument("--show", action="store_true", help="print the current formatting")
    p.add_argument("--list", action="store_true", help="formatting objects of --type (with --object: its properties)")
    p.add_argument("--object", help="with --list: one formatting object")
    p = sub.add_parser("visual", help="visual catalog: built-in types and custom visuals (list, roles, import, register)")
    p.add_argument("action", choices=["list", "roles", "import", "register"])
    p.add_argument("target", nargs="?", help="roles: visual type; import: a .pbiviz file; register: the visual's guid")
    p.add_argument("--as", dest="as_", help="import: contract (roles only, default), public or private; "
                                            "register: public or organizational")
    p.add_argument("--role", action="append", default=[], help="register: NAME:grouping|measure|either (repeatable)")
    p.add_argument("--replace", action="store_true", help="import --as private: replace an existing package")
    p = sub.add_parser("consumers", help="external consumers that keep objects used: thin reports, Excel, "
                                         "paginated reports, composite models, query exports")
    p.add_argument("action", choices=["list", "add", "scan", "show"])
    p.add_argument("target", nargs="?", help="add: file or folder; show: consumer id")
    p.add_argument("--kind", default="auto", help="add: auto | pbir | pbix | xlsx | rdl | model | queries | scan")
    p.add_argument("--label", help="add: display label (required with --object)")
    p.add_argument("--object", action="append", default=[], help="add: declare a used object by hand (repeatable)")
    p.add_argument("--model-name", action="append", default=[],
                   help="add: the model's name in the service, if it differs from the project name")
    p = sub.add_parser("relationship", help="add, remove or change a relationship (metadata-guarded)")
    p.add_argument("action", choices=["add", "remove", "set"])
    p.add_argument("from_col", help="many side, 'Table'[Column]")
    p.add_argument("to_col", help="one side, 'Table'[Column]")
    p.add_argument("--inactive", action="store_true")
    p.add_argument("--active", action="store_true", help="set: make it active")
    p.add_argument("--both", action="store_true", help="filter in both directions")
    p.add_argument("--single", action="store_true", help="set: filter in one direction")
    p.add_argument("--many-to-many", action="store_true")
    p.add_argument("--force", action="store_true", help="add: allow mismatched data types")
    p = sub.add_parser("move", help="move a measure to another table, fixing references")
    p.add_argument("kind", choices=["measure"])
    p.add_argument("name")
    p.add_argument("--to", required=True, help="target table")
    p = sub.add_parser("culture", help="add, remove, list cultures; list objects missing a caption")
    p.add_argument("action", choices=["add", "remove", "list", "missing"])
    p.add_argument("code", nargs="?", help="culture code, e.g. de-DE")
    p = sub.add_parser("translate", help="set a translated caption, description or display folder")
    p.add_argument("code", help="culture code")
    p.add_argument("target", help="Table, 'Table'[Column], [Measure] or 'Table'[Hierarchy]")
    p.add_argument("--caption")
    p.add_argument("--description")
    p.add_argument("--folder")
    p = sub.add_parser("perspective", help="add, remove, include or exclude objects in a perspective")
    p.add_argument("action", choices=["add", "remove", "include", "exclude"])
    p.add_argument("name")
    p.add_argument("targets", nargs="*", help="Table, 'T'[Column], [Measure] or T.Hierarchy")
    p = sub.add_parser("role", help="security roles: add, remove, filter, unfilter, member-add, member-remove")
    p.add_argument("action", choices=["add", "remove", "filter", "unfilter", "member-add", "member-remove"])
    p.add_argument("name", help="role name")
    p.add_argument("args", nargs="*", help="filter: <table> \"<DAX>\"; unfilter: <table>; member-*: <member or token>")
    p.add_argument("--permission", default="read")
    p = sub.add_parser("refresh-policy", help="incremental refresh: set, remove or show a table's policy")
    p.add_argument("action", choices=["set", "remove", "show"])
    p.add_argument("table", nargs="?")
    p.add_argument("--date", help="set: dateTime column to filter on")
    p.add_argument("--store", type=int, help="set: archive window length")
    p.add_argument("--store-unit", default="year", choices=["day", "month", "quarter", "year"])
    p.add_argument("--refresh", type=int, help="set: incremental window length")
    p.add_argument("--refresh-unit", default="day", choices=["day", "month", "quarter", "year"])
    p.add_argument("--detect", help="set: column whose maximum detects changed periods")
    p.add_argument("--complete", action="store_true", help="set: only refresh complete periods")
    p.add_argument("--hybrid", action="store_true", help="set: latest period in DirectQuery (Premium)")
    sub.add_parser("mcp", help="serve pbz verbs as MCP tools over stdio (for agent hosts)")
    p = sub.add_parser("synth", help="synthetic test copy of the model with fake data (query it freely)")
    p.add_argument("action", choices=["build", "status", "open", "close", "query", "remove"])
    p.add_argument("dax", nargs="?", help="query: an EVALUATE statement (synthetic model only)")
    p.add_argument("--rows", type=int, default=200, help="build: rows per fact table")
    p.add_argument("--seed", type=int, default=7, help="build: random seed")
    p.add_argument("--wait", type=int, default=480)
    p = sub.add_parser("format", help="format DAX (measures, calculated columns, calculation items), token-safe")
    p.add_argument("target", nargs="?", help="one measure or column name (default: all)")
    p.add_argument("--apply", action="store_true", help="write the formatting (default: check only)")
    p = sub.add_parser("repair", help="fix structural PBIR breakage found by validate")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--fields", action="store_true", help="also drop projections on fields the model no longer has")
    p.add_argument("--lineage", action="store_true", help="also give objects with copied lineageTags fresh ones")
    p = sub.add_parser("layout", help="text canvas map of report pages plus layout lint")
    p.add_argument("--page", help="page display name or id (draws its map)")
    p.add_argument("--no-map", action="store_true")
    p = sub.add_parser("admin")
    p.add_argument("action", choices=["budget-reset", "allow", "category", "synthetic", "log", "consumer-remove"])
    p.add_argument("value", nargs="?")
    a = ap.parse_args(argv)
    if a.cmd == "mcp":
        from pbz import mcp
        return mcp.serve(a.workspace, a.project)
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
            out = mirror.push(ws, project, reload=a.reload, dry_run=a.dry_run, only=a.only)
        elif a.cmd == "revert":
            out = mirror.revert(ws, project, a.paths or None, a.object)
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
        elif a.cmd == "unused":
            from pbz import unused
            if a.restore:
                out = unused.restore(ws, project, a.restore)
            elif a.remove:
                out = unused.remove(ws, project, a.report, m_step=a.m_step, cascade=a.cascade, only=a.only,
                                    dry_run=a.dry_run)
            elif a.verify_engine:
                from pbz import engdeps
                out = engdeps.verify(ws, project, a.report)
            else:
                out = unused.report(ws, project, a.report, include_all=a.all, impact=a.impact, kinds=a.kinds)
        elif a.cmd == "lineage":
            from pbz import unused
            out = unused.lineage(ws, project, a.target, "up" if a.up else "down", a.depth, a.report)
        elif a.cmd == "suggest":
            from pbz import suggest
            out = suggest.run(ws, project, a.report)
        elif a.cmd == "bpa" and a.fix:
            from pbz import fix
            out = fix.run(ws, project, a.fix, dry_run=a.dry_run)
        elif a.cmd == "bpa":
            from pbz import bpa
            out = bpa.run(ws, project, a.scope, rules_path=a.rules, opinion=a.opinion, labels=a.labels,
                          extra_reports=a.report)
        elif a.cmd == "similar":
            from pbz import similar
            out = similar.run(ws, project, a.threshold, a.max)
        elif a.cmd == "changes":
            from pbz import changes
            out = changes.list_snapshots(ws, project) if a.list else changes.run(ws, project, a.from_, a.to)
        elif a.cmd == "docs":
            from pbz import docs
            out = docs.run(ws, project, a.format, a.dax)
        elif a.cmd == "m-lineage":
            from pbz import mlineage
            out = mlineage.run(ws, project, a.table)
        elif a.cmd == "new":
            from pbz import scaffold
            out = scaffold.run(ws, project, a)
        elif a.cmd == "add":
            from pbz import author
            if a.kind == "page":
                out = author.add_page(ws, project, a.name, a.width, a.height, a.hidden, a.after)
            else:
                if not a.page:
                    raise PbzError("BAD_REQUEST", "--page is required for a visual")
                out = author.add_visual(ws, project, a.name, a.page, a.field, a.pos, a.title, a.text)
        elif a.cmd == "style":
            from pbz import style
            if a.list:
                if not a.vtype:
                    raise PbzError("BAD_REQUEST", "--list needs --type <visual type>")
                out = style.list_(ws, project, a.vtype, a.object)
            else:
                out = style.run(ws, project, a.page, a.visual, a.sets, a.unsets, a.vtype, a.all, a.state, a.series,
                                a.show)
        elif a.cmd == "visual":
            from pbz import visuals
            if a.action != "list" and not a.target:
                raise PbzError("BAD_REQUEST", f"visual {a.action} needs a target")
            if a.action == "list":
                out = visuals.list_visuals(ws, project)
            elif a.action == "roles":
                out = visuals.roles(ws, project, a.target)
            elif a.action == "import":
                out = visuals.import_pbiviz(ws, project, a.target, a.as_ or "contract", a.replace)
            else:
                out = visuals.register(ws, project, a.target, a.as_ or "public", a.role)
        elif a.cmd == "consumers":
            from pbz import consumers
            if a.action == "list":
                out = consumers.list_(ws, project)
            elif a.action == "scan":
                out = consumers.scan(ws, project)
            elif a.action == "show":
                if not a.target:
                    raise PbzError("BAD_REQUEST", "consumers show <id>")
                out = consumers.show(ws, project, a.target)
            else:
                out = consumers.add(ws, project, a.target, a.kind, a.label, a.object, a.model_name)
        elif a.cmd == "relationship":
            from pbz import relate
            if a.action == "add":
                out = relate.add(ws, project, a.from_col, a.to_col, inactive=a.inactive, both=a.both,
                                 many_to_many=a.many_to_many, force=a.force)
            elif a.action == "remove":
                out = relate.remove(ws, project, a.from_col, a.to_col)
            else:
                out = relate.set_(ws, project, a.from_col, a.to_col,
                                  active=True if a.active else (False if a.inactive else None),
                                  both=True if a.both else (False if a.single else None))
        elif a.cmd == "move":
            from pbz import move
            out = move.move_measure(ws, project, a.name, a.to)
        elif a.cmd in ("culture", "translate", "perspective", "role"):
            from pbz import governance as gv
            if a.cmd == "culture":
                if a.action != "list" and not a.code:
                    raise PbzError("BAD_REQUEST", "culture code required")
                out = {"add": gv.culture_add, "remove": gv.culture_remove, "missing": gv.culture_missing}[a.action](
                    ws, project, a.code) if a.action != "list" else gv.culture_list(ws, project, a.code)
            elif a.cmd == "translate":
                out = gv.translate(ws, project, a.code, a.target, a.caption, a.description, a.folder)
            elif a.cmd == "perspective":
                if a.action == "add":
                    out = gv.perspective_add(ws, project, a.name)
                elif a.action == "remove":
                    out = gv.perspective_remove(ws, project, a.name)
                else:
                    out = gv.perspective_edit(ws, project, a.name, a.targets, a.action == "include")
            else:
                if a.action == "add":
                    out = gv.role_add(ws, project, a.name, a.permission)
                elif a.action == "remove":
                    out = gv.role_remove(ws, project, a.name)
                elif a.action == "filter":
                    if len(a.args) != 2:
                        raise PbzError("BAD_REQUEST", "role filter <name> <table> \"<DAX>\"")
                    out = gv.role_filter(ws, project, a.name, a.args[0], a.args[1])
                elif a.action == "unfilter":
                    if len(a.args) != 1:
                        raise PbzError("BAD_REQUEST", "role unfilter <name> <table>")
                    out = gv.role_filter(ws, project, a.name, a.args[0], None)
                else:
                    if len(a.args) != 1:
                        raise PbzError("BAD_REQUEST", "role member-add|member-remove <name> <member>")
                    out = gv.role_member(ws, project, a.name, "add" if a.action == "member-add" else "remove", a.args[0])
        elif a.cmd == "refresh-policy":
            from pbz import refreshpolicy as rp
            if a.action == "show":
                out = rp.show(ws, project, a.table)
            elif not a.table:
                raise PbzError("BAD_REQUEST", "table required")
            elif a.action == "remove":
                out = rp.remove_policy(ws, project, a.table)
            else:
                if not (a.date and a.store and a.refresh):
                    raise PbzError("BAD_REQUEST", "set needs --date, --store and --refresh")
                out = rp.set_policy(ws, project, a.table, a.date, a.store, a.store_unit, a.refresh, a.refresh_unit,
                                    a.detect, a.complete, a.hybrid)
        elif a.cmd == "synth":
            from pbz import synth
            out = synth.run(ws, project, a)
        elif a.cmd == "format":
            from pbz import daxfmt
            out = daxfmt.run(ws, project, apply=a.apply, target=a.target)
        elif a.cmd == "repair":
            from pbz import repair
            out = repair.run(ws, project, dry_run=a.dry_run, fields=a.fields, lineage=a.lineage)
        elif a.cmd == "layout":
            from pbz import layout
            out = layout.run(ws, project, a.page, show_map=not a.no_map)
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
    if a.action == "consumer-remove":
        if not a.value:
            raise PbzError("BAD_REQUEST", "consumer id required")
        from pbz import consumers
        return consumers.remove(ws, a.value)
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
