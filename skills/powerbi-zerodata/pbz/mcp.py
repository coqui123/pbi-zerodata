"""`pbz mcp`: the pbz verbs as typed MCP tools over stdio (newline-delimited JSON-RPC 2.0, stdlib only).

Each tool call runs the regular CLI (`python -I pbz/cli.py --workspace <ws> <verb> ...`) in a child
process and returns its JSON output as one text block, so tools obey exactly the same data-blind
rules as the CLI. stdout carries protocol messages only; stderr carries startup / shutdown lines and
never echoes tool arguments or outputs. `pbz admin` and `memscrub --apply` are not exposed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py")
SERVER = {"name": "pbz", "title": "pbz (data-blind Power BI PBIP tools)", "version": "1.0"}
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("pbz edits Power BI PBIP projects without exposing data: work on the sanitized mirror in "
                "pbz-workspace, review with diff / validate, apply with push. Every tool returns names, ids, "
                "verdicts and fixed buckets only, never data values. Never try to read data files or query "
                "an engine another way.")

S, B, I, N, O = {"type": "string"}, {"type": "boolean"}, {"type": "integer"}, {"type": "number"}, {"type": "object"}
L = {"type": "array", "items": {"type": "string"}}


def E(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


def D(schema: dict, text: str) -> dict:
    return dict(schema, description=text)


@dataclass
class Tool:
    name: str
    description: str
    verb: list
    pos: list = field(default_factory=list)       # [(arg, schema, required)] in CLI order
    opts: dict = field(default_factory=dict)      # arg -> (cli flag, schema, mode: flag|value|multi|append|json)
    read_only: bool = True
    destructive: bool = False
    timeout: int = 900
    need: list = field(default_factory=list)      # option args that are required

    def schema(self) -> dict:
        props = {"workspace": D(S, "workspace folder (holds the .pbip); default: the server's workspace"),
                 "project": D(S, "project name when the workspace holds several .pbip files")}
        req = []
        for arg, sch, required in self.pos:
            props[arg] = sch
            if required:
                req.append(arg)
        for arg, (_flag, sch, _mode) in self.opts.items():
            props[arg] = sch
        req += self.need
        out = {"type": "object", "properties": props, "additionalProperties": False}
        if req:
            out["required"] = req
        return out

    def listing(self) -> dict:
        return {"name": self.name, "title": self.name.replace("_", " "), "description": self.description,
                "inputSchema": self.schema(),
                "annotations": {"readOnlyHint": self.read_only, "destructiveHint": self.destructive,
                                "openWorldHint": False}}


def _f(flag: str) -> tuple:
    return (flag, B, "flag")


REPORTS = ("--report", D(L, "extra *.Report folders (thin reports) to scan"), "append")
TOOLS = [
    Tool("preflight", "Full private staged validation without applying project changes.", ["preflight"],
         opts={"only": ("--only", L, "multi")}),
    Tool("recover", "Recover a journaled interrupted push without overwriting later user edits.", ["recover"],
         opts={"transaction": ("--transaction", S, "value")}, read_only=False),
    Tool("preview", "Render watermarked invented-data SVG previews and layout contracts. Native appearance is not certified.", ["preview"],
         opts={"page": ("--page", S, "value"), "spec": ("--spec", O, "json")}, read_only=False),
    Tool("check_schema", "Discover the typed assertion specification schema.", ["check-schema"]),
    Tool("interactions", "Check interaction metadata or report unavailable native capabilities explicitly.", ["interactions"],
         pos=[("action", E("capabilities", "check"), True), ("spec", S, False)],
         opts={"mode": ("--mode", E("static", "native"), "value")}),
    Tool("freshness_capture", "Capture private partition refresh provenance; source publication remains unknown.", ["freshness", "capture"],
         opts={"max_age_hours": ("--max-age-hours", I, "value")}, read_only=False),
    Tool("freshness_compare", "Check matching counting contracts and publication/refresh cutoffs without reading business totals.", ["freshness", "compare"],
         pos=[("spec", S, True)]),
    Tool("requirements_import", "Register prepared task JSON with Excel cell locators. Descriptions remain protected.", ["requirements", "import"],
         pos=[("spec", S, True)], read_only=False),
    Tool("requirements_status", "Revision-bound task coverage and outstanding human acceptance.", ["requirements", "status"]),
    Tool("requirements_verify", "Run the task's registered acceptance checks under disclosure limits.", ["requirements", "verify"],
         pos=[("id", S, True)], read_only=False),
    Tool("consent_request", "Request exact-file approval. User must verify no sensitive content before human approval.", ["consent", "request"],
         pos=[("path", S, True)], opts={"purpose": ("--purpose", E("requirements", "reference", "brand-asset", "other-source"), "value")}, read_only=False),
    Tool("consent_status", "Check a content-bound file approval receipt without reading file contents.", ["consent", "status"],
         pos=[("id", S, True)]),
    Tool("consent_read", "Read only an unchanged file with a valid human no-sensitive-data attestation. Protected Power BI files stay denied.", ["consent", "read"],
         pos=[("id", S, True)]),
    Tool("status", "Workspace, mirror and sync state, Desktop, engine and guard.", ["status"]),
    Tool("doctor", "Read-only OS-isolation checklist (five steps). Does not change ACLs, accounts, "
                   "firewall rules or brokers. Heuristic mode is not OS-isolated.", ["doctor"]),
    Tool("capabilities", "Closed capability matrix (supported, static_only, approximate, unavailable, unknown, "
                         "checklist_only, available). Native visual interactions stay unavailable.", ["capabilities"]),
    Tool("init", "First use in a workspace: policy, scan of data-bearing files, first pull.", ["init"], read_only=False),
    Tool("scan", "Inventory data-bearing files and leaky scripts; extends the policy deny list.", ["scan"],
         opts={"dry_run": _f("--dry-run")}, read_only=False),
    Tool("pull", "Refresh the sanitized mirror from the real files.", ["pull"],
         opts={"force": ("--force", D(B, "discard unpushed mirror edits"), "flag"),
               "rebase": ("--rebase", D(B, "merge unpushed mirror edits onto the new real files"), "flag")},
         read_only=False),
    Tool("diff", "Pending mirror edits (changed / added / deleted files).", ["diff"],
         opts={"patch": ("--patch", D(B, "include a unified diff of the sanitized text"), "flag")}),
    Tool("push", "Validate the mirror edits on a staged copy (TOM, PBIR, references, lint, tokens), back up, then "
                 "write the real files.", ["push"],
         opts={"reload": ("--reload", D(B, "report-only change: reload the open Desktop"), "flag"),
               "dry_run": ("--dry-run", D(B, "validate only, write nothing"), "flag"),
               "only": ("--only", D(L, "push only these mirror files (globs or folder prefixes); the rest stays pending"),
                        "multi")},
         read_only=False, timeout=1800),
    Tool("revert", "Discard mirror edits: files back to the last pull / push, or one TMDL object.", ["revert"],
         pos=[("paths", D(L, "mirror files (globs or folder prefixes); default all"), False)],
         opts={"object": ("--object", D(S, "one object: measure 'Table'[Name] (also column, hierarchy, partition, "
                                           "calculationItem)"), "value")}, read_only=False, destructive=True),
    Tool("style", "Visual formatting (titles, data labels, colors, fonts, axes, legends, borders, ...) checked "
                  "against Microsoft's formatting catalog; one visual, every visual of a type, or every visual on "
                  "a page. Use list (with type, optionally object) to see the formatting objects.", ["style"],
         pos=[("page", D(S, "page display name or id"), False), ("visual", D(S, "visual id or layout label"), False)],
         opts={"set": ("--set", D(L, "[container.]object.property=value; colors #RRGGBB, theme:<n>[:<shade %>], "
                                     "measure:[Measure]"), "append"),
               "unset": ("--unset", D(L, "[container.]object.property"), "append"),
               "type": ("--type", D(S, "every visual of this type"), "value"),
               "all": ("--all", D(B, "every visual on the page"), "flag"),
               "series": ("--series", D(S, "[Measure] or 'T'[Column] shown by the visual"), "value"),
               "state": ("--state", D(S, "instance state: hover, selected, press, ..."), "value"),
               "show": ("--show", D(B, "print the current formatting"), "flag"),
               "list": ("--list", D(B, "formatting objects of type"), "flag"),
               "object": ("--object", D(S, "with list: one object"), "value"),
               "canvas": ("--canvas", D(B, "format the page itself: background.<p> / wallpaper.<p>, p = color, "
                                           "transparency, image (registered item), scaling (fit, fill, normal)"), "flag")},
         read_only=False),
    Tool("image", "Report images: add a file (type checked from its header, staged in the private store, installed "
                  "by push; content never shown), list registered images with where they are used, or remove an "
                  "unused one. Use them with style --canvas (background / wallpaper), add_visual image, or an image "
                  "property of a visual.", ["image"],
         pos=[("action", E("add", "list", "remove"), True),
              ("target", D(S, "add: image file path; remove: item name"), False)],
         opts={"name": ("--name", D(S, "add: item name (default: the file name)"), "value")},
         read_only=False),
    Tool("visual", "Visual catalog: list built-in and custom visuals, show a type's data roles, import a .pbiviz "
                   "(contract = roles only, public = AppSource guid, private = package installed by push), or "
                   "register a custom visual by guid.", ["visual"],
         pos=[("action", E("list", "roles", "import", "register"), True),
              ("target", D(S, "roles: visual type; import: .pbiviz path; register: visual guid"), False)],
         opts={"as": ("--as", E("contract", "public", "private", "organizational"), "value"),
               "roles": ("--role", D(L, "register: NAME:grouping|measure|either"), "append"),
               "replace": ("--replace", B, "flag")},
         read_only=False),
    Tool("consumers", "External consumers that keep objects used (thin reports, Excel, paginated reports, "
                      "composite models, query exports, a scanner result, manual declarations): list, add, "
                      "rescan, show, or discover files connected to the model in a folder. Removing one is "
                      "human only.", ["consumers"],
         pos=[("action", E("list", "add", "scan", "show", "discover"), True),
              ("target", D(S, "add: file or folder path; show: consumer id; discover: folder"), False)],
         opts={"register": ("--register", D(B, "discover: register the files bound to this model"), "flag"),
               "depth": ("--depth", D(I, "discover: folder depth"), "value"),
               "kind": ("--kind", E("auto", "pbir", "pbix", "xlsx", "rdl", "model", "queries", "scan"), "value"),
               "label": ("--label", S, "value"),
               "objects": ("--object", D(L, "manual: [Measure] / 'T'[Column] / Table used elsewhere"), "append"),
               "model_names": ("--model-name", D(L, "the model's name in the service"), "append")},
         read_only=False),
    Tool("culture", "Cultures: add, remove, list, or list the objects missing a caption.", ["culture"],
         pos=[("action", E("add", "remove", "list", "missing"), True), ("code", D(S, "culture code, e.g. de-DE"), False)],
         read_only=False),
    Tool("translate", "Set a translated caption, description or display folder for a table, column, measure or "
                      "hierarchy.", ["translate"],
         pos=[("code", D(S, "culture code"), True), ("target", D(S, "Table, 'T'[Column], [Measure] or 'T'[Hierarchy]"), True)],
         opts={"caption": ("--caption", S, "value"), "description": ("--description", S, "value"),
               "folder": ("--folder", S, "value")}, read_only=False),
    Tool("perspective", "Perspectives: add, remove, include or exclude objects.", ["perspective"],
         pos=[("action", E("add", "remove", "include", "exclude"), True), ("name", S, True),
              ("targets", D(L, "Table, 'T'[Column], [Measure] or T.Hierarchy"), False)], read_only=False),
    Tool("role", "Security roles: add, remove, filter (table + DAX), unfilter, member-add, member-remove, ols "
                 "(object-level security: [Table or 'T'[C], none|read|default]).", ["role"],
         pos=[("action", E("add", "remove", "filter", "unfilter", "member-add", "member-remove", "ols"), True), ("name", S, True),
              ("args", D(L, "filter: [table, DAX]; unfilter: [table]; member-*: [member or its mirror token]; "
                            "ols: [Table or 'T'[C], none|read|default]"), False)],
         opts={"permission": ("--permission", E("read", "readRefresh", "refresh", "administrator", "none"), "value")},
         read_only=False),
    Tool("refresh_policy", "Incremental refresh: set, remove or show a table's policy (parameters, filter step, "
                           "windows, change detection).", ["refresh-policy"],
         pos=[("action", E("set", "remove", "show"), True), ("table", S, False)],
         opts={"date": ("--date", D(S, "dateTime column to filter on"), "value"),
               "store": ("--store", D(I, "archive window length"), "value"),
               "store_unit": ("--store-unit", E("day", "month", "quarter", "year"), "value"),
               "refresh": ("--refresh", D(I, "incremental window length"), "value"),
               "refresh_unit": ("--refresh-unit", E("day", "month", "quarter", "year"), "value"),
               "detect": ("--detect", D(S, "column whose maximum detects changed periods"), "value"),
               "complete": ("--complete", D(B, "only refresh complete periods"), "flag"),
               "hybrid": ("--hybrid", D(B, "latest period in DirectQuery"), "flag")}, read_only=False),
    Tool("validate", "Metadata checks on the mirror: lint, field references, PBIR integrity.", ["validate"]),
    Tool("describe", "Data dictionary: model overview, a table, a [Measure] or a 'Table'[Column] (types, classes, "
                     "buckets, category labels, DAX, dependencies, visuals using it).", ["describe"],
         pos=[("target", D(S, "table name, [Measure] or 'Table'[Column]; omit for the overview"), False)]),
    Tool("values", "Category labels of a column, or for a sensitive column a distinct-count bucket and its tokens.",
         ["values"], pos=[("column", D(S, "'Table'[Column]"), True)]),
    Tool("rename", "Rename a measure, column or table and fix every reference in the mirror.", ["rename"],
         pos=[("kind", E("measure", "column", "table"), True), ("old", S, True), ("new", S, True)],
         opts={"table": ("--table", D(S, "home table when a column name is ambiguous"), "value")}, read_only=False),
    Tool("move_measure", "Move a measure to another table (for example a measure table), fixing references.",
         ["move", "measure"], pos=[("name", D(S, "measure name"), True)],
         opts={"to": ("--to", D(S, "target table"), "value")}, need=["to"], read_only=False),
    Tool("unused", "Usage state of every object (used / used_external / locked / used_by_unused / unused / unknown; "
                   "register external consumers with the consumers tool); with remove, "
                   "delete unused objects from the mirror (restore point kept).", ["unused"],
         opts={"all": ("--all", D(B, "also list used objects and auto date/time tables"), "flag"),
               "kinds": ("--kinds", D(L, "limit to object kinds"), "multi"),
               "report": REPORTS,
               "impact": ("--impact", D(B, "add cardinality / row buckets from index"), "flag"),
               "remove": ("--remove", D(B, "delete unused objects from the mirror"), "flag"),
               "dry_run": ("--dry-run", D(B, "with remove: list what would go"), "flag"),
               "cascade": ("--cascade", D(B, "with remove: repeat until nothing new is unused"), "flag"),
               "only": ("--only", D(L, "with remove: only these kinds"), "multi"),
               "m_step": ("--m-step", E("remove", "keep", "none"), "value"),
               "restore": ("--restore", D(S, "undo a removal: its restore point stamp"), "value"),
               "verify_engine": ("--verify-engine", D(B, "cross-check dependencies with the open Desktop engine"),
                                 "flag")},
         read_only=False, destructive=True),
    Tool("lineage", "Dependency tree of one object: what depends on it (default) or what it depends on.",
         ["lineage"], pos=[("target", D(S, "'Table'[Column], [Measure], Table or a bare name"), True)],
         opts={"up": ("--up", D(B, "what it depends on"), "flag"), "depth": ("--depth", I, "value"), "report": REPORTS}),
    Tool("suggest", "Cleanup and optimisation suggestions.", ["suggest"], opts={"report": REPORTS}),
    Tool("bpa", "Best practice analysis with scores and grades; with fix, apply property-only fixes.", ["bpa"],
         pos=[("scope", E("model", "report", "all"), False)],
         opts={"rules": ("--rules", D(S, "JSON overlay file"), "value"),
               "opinion": ("--opinion", D(B, "include opinion rules"), "flag"),
               "labels": ("--labels", E("letters", "measure-killer"), "value"),
               "report": REPORTS,
               "fix": ("--fix", D(L, "rule ids whose property-only fix to apply"), "append"),
               "dry_run": ("--dry-run", D(B, "with fix: list what would change"), "flag")},
         read_only=False),
    Tool("similar", "Identical, alias, same-shape and near-duplicate measures.", ["similar"],
         opts={"threshold": ("--threshold", N, "value"), "max": ("--max", I, "value")}),
    Tool("audit", "Efficiency and consolidation audit of the model: iterators, calculated columns, storage bands, "
                  "relationships, Power Query overlap, duplicate measures and tables, calculation-group, filtered and "
                  "parameterized families, repeated sub-expressions. Each finding has an id, evidence, fix and "
                  "verify. run (default), waive <id> --reason, or memory (storage bands from the open Desktop "
                  "model; buckets only).", ["audit"],
         pos=[("action", E("run", "waive", "memory"), False), ("id", D(S, "waive: finding id"), False)],
         opts={"category": ("--category", E("efficiency", "consolidation", "design"), "value"),
               "rule": ("--rule", D(L, "only these rule ids"), "append"),
               "min_severity": ("--min-severity", E("high", "medium", "low", "info"), "value"),
               "since": ("--since", D(S, "baseline name: mark new / persistent / resolved"), "value"),
               "save_baseline": ("--save-baseline", D(S, "save fingerprints under this name"), "value"),
               "fail_on": ("--fail-on", E("new", "any", "high"), "value"),
               "coverage": ("--coverage", D(B, "every rule with source, evidence and status"), "flag"),
               "max": ("--max", I, "value"),
               "reason": ("--reason", D(S, "waive: why the finding is accepted"), "value"),
               "expires": ("--expires", D(S, "waive: YYYY-MM-DD"), "value"),
               "remove": ("--remove", D(B, "waive: drop the waiver"), "flag"),
               "no_predicates": ("--no-predicates", D(B, "memory: skip value predicates"), "flag")},
         read_only=False),
    Tool("consolidate", "Consolidation refactors in the mirror: merge a duplicate measure into another (references "
                        "retargeted; hidden alias kept for external consumers), list calculation-group families, "
                        "or turn a family into a calculation group with wrapper measures and a check spec.",
         ["consolidate"],
         pos=[("action", E("merge", "families", "calc-group"), True),
              ("target", D(S, "merge: the duplicate [Measure]"), False)],
         opts={"into": ("--into", D(S, "merge: the measure to keep"), "value"),
               "keep_alias": ("--keep-alias", D(B, "merge: keep the duplicate as a hidden alias"), "flag"),
               "force": ("--force", D(B, "merge: even when DAX or formats differ"), "flag"),
               "family": ("--family", D(S, "calc-group: family id"), "value"),
               "measure": ("--measure", D(L, "calc-group: member measures instead of a family id"), "append"),
               "name": ("--name", D(S, "calc-group: table name"), "value"),
               "column": ("--column", D(S, "calc-group: item column"), "value"),
               "precedence": ("--precedence", I, "value"),
               "dry_run": ("--dry-run", B, "flag")},
         read_only=False),
    Tool("changes", "Semantic diff of mirror snapshots by object (pending edits, or what changed between pulls).",
         ["changes"], opts={"from": ("--from", D(S, "last, previous or a stamp"), "value"),
                            "to": ("--to", D(S, "mirror, last or a stamp"), "value"),
                            "list": ("--list", D(B, "list snapshots"), "flag")}),
    Tool("docs", "Write documentation of the model and reports into pbz-workspace/docs.", ["docs"],
         opts={"format": ("--format", E("md", "html", "json"), "value"),
               "dax": ("--dax", D(B, "include DAX and M as in the mirror"), "flag")}, read_only=False),
    Tool("m_lineage", "Power Query steps and column origins; columns the query drops or the model lacks.",
         ["m-lineage"], pos=[("table", S, False)]),
    Tool("layout", "Text canvas map of a report page plus overlap / off-canvas / z-order lint.", ["layout"],
         opts={"page": ("--page", D(S, "page display name or id (draws its map)"), "value"),
               "no_map": ("--no-map", B, "flag")}),
    Tool("format", "Token-safe DAX formatting of measures, calculated columns and calculation items.", ["format"],
         pos=[("target", D(S, "one measure or column (default: all)"), False)],
         opts={"apply": ("--apply", D(B, "write the formatting (default: check only)"), "flag")}, read_only=False),
    Tool("repair", "Fix structural PBIR breakage (page order, names, groups, interactions, bookmark lists).",
         ["repair"], opts={"dry_run": _f("--dry-run"),
                           "fields": ("--fields", D(B, "also drop projections on missing fields"), "flag"),
                           "lineage": ("--lineage", D(B, "also give copied lineageTags fresh ones"), "flag")},
         read_only=False),
    Tool("new", "Scaffold a measure table, date table, calculation group or field parameter in the mirror.", ["new"],
         pos=[("kind", E("measure-table", "date-table", "calc-group", "field-parameter"), True),
              ("name", D(S, "new table name"), True)],
         opts={"start": ("--start", D(I, "date-table: first year"), "value"),
               "end": ("--end", D(I, "date-table: last year"), "value"),
               "relate": ("--relate", D(L, "date-table: fact date columns to relate, 'T'[C]"), "append"),
               "date": ("--date", D(S, "calc-group: the date column, 'Date'[Date]"), "value"),
               "items": ("--items", D(L, "calc-group items"), "multi"),
               "precedence": ("--precedence", I, "value"),
               "fields": ("--fields", D(L, "field-parameter: [Measure] or 'Table'[Column] list"), "multi"),
               "labels": ("--labels", D(L, "field-parameter: display labels, one per field"), "multi")},
         read_only=False),
    Tool("relationship", "Add, remove or change a relationship with type, duplicate and ambiguous-path guards.",
         ["relationship"], pos=[("action", E("add", "remove", "set"), True),
                                ("from_col", D(S, "many side, 'Table'[Column]"), True),
                                ("to_col", D(S, "one side, 'Table'[Column]"), True)],
         opts={"inactive": _f("--inactive"), "active": _f("--active"), "both": _f("--both"), "single": _f("--single"),
               "many_to_many": _f("--many-to-many"), "force": ("--force", D(B, "add: allow mismatched types"), "flag"),
               "dry_run": _f("--dry-run")},
         read_only=False),
    Tool("add_page", "Create a report page in the mirror: plain, a report-page tooltip, or a drillthrough target "
                     "(with a Back button).", ["add", "page"],
         pos=[("name", D(S, "page display name"), True)],
         opts={"width": ("--width", I, "value"), "height": ("--height", I, "value"),
               "hidden": ("--hidden", B, "flag"), "after": ("--after", D(S, "insert after this page"), "value"),
               "tooltip": ("--tooltip", D(B, "a report-page tooltip (hidden, 320x240)"), "flag"),
               "drillthrough": ("--drillthrough", D(L, "drillthrough columns, 'T'[C]"), "append")},
         read_only=False),
    Tool("link", "Button actions (page navigation, back, bookmark, drillthrough, web link) and report-page tooltips "
                 "on a visual.", ["link"],
         pos=[("page", D(S, "page holding the visual"), True), ("visual", D(S, "visual id or layout label"), True)],
         opts={"to_page": ("--to-page", S, "value"), "back": ("--back", B, "flag"), "bookmark": ("--bookmark", S, "value"),
               "drillthrough": ("--drillthrough", S, "value"), "url": ("--url", S, "value"),
               "tooltip": ("--tooltip", D(S, "tooltip page shown on hover"), "value"), "clear": ("--clear", B, "flag")},
         read_only=False),
    Tool("filter", "Report, page and visual filters: add (basic in / not in, advanced comparisons, Top N, empty), "
                   "remove, list. Values are labels, typed values or mirror tokens.", ["filter"],
         pos=[("action", E("add", "remove", "list"), True),
              ("target", D(S, "add: 'T'[Column] or [Measure]; remove: filter name or field"), False)],
         opts={"report": ("--report", B, "flag"), "page": ("--page", S, "value"), "visual": ("--visual", S, "value"),
               "values_in": ("--in", D(L, "keep these values"), "multi"), "values_out": ("--not-in", D(L, "drop these values"), "multi"),
               "where": ("--where", D(L, "\">= 100\" (two for AND)"), "append"), "top": ("--top", I, "value"),
               "by": ("--by", D(S, "Top N: ranking measure"), "value"), "bottom": ("--bottom", B, "flag"),
               "dry_run": _f("--dry-run")},
         read_only=False),
    Tool("bookmark", "Bookmarks of a page's shown and hidden visuals (never data state): add, remove, list.",
         ["bookmark"],
         pos=[("action", E("add", "remove", "list"), True), ("name", D(S, "display name (remove: name or id)"), False)],
         opts={"page": ("--page", S, "value"), "hide": ("--hide", D(L, "visuals to hide"), "multi"),
               "show": ("--show", D(L, "hidden visuals to show"), "multi"),
               "only": ("--only", D(L, "apply to these visuals only"), "multi")},
         read_only=False),
    Tool("interaction", "How one visual filters or highlights another (or every other) visual on its page.",
         ["interaction"],
         pos=[("page", S, True), ("source", D(S, "visual id or layout label"), True),
              ("target", D(S, "visual id, layout label, or all"), True),
              ("mode", E("filter", "highlight", "none", "default"), True)],
         opts={"dry_run": _f("--dry-run")},
         read_only=False),
    Tool("add_visual", "Create a visual bound to model fields: any built-in type (roles from Microsoft's core "
                       "visual capabilities) or a registered custom visual with a known contract.", ["add", "visual"],
         pos=[("type", D(S, "built-in type (card, cardVisual, kpi, gauge, tableEx, pivotTable, slicer, textbox, "
                            "clusteredColumnChart, lineChart, ...), or a custom visual's guid or imported name"), True)],
         opts={"page": ("--page", D(S, "target page (display name or id)"), "value"),
               "fields": ("--field", D(L, "[Measure], 'T'[Column] or ROLE='T'[Column]"), "append"),
               "pos": ("--pos", D(S, "x,y,width,height"), "value"),
               "title": ("--title", S, "value"), "text": ("--text", D(S, "textbox text"), "value"),
               "image": ("--image", D(S, "image visual: a registered image item"), "value"),
               "fit": ("--fit", E("fit", "fill", "normal"), "value"),
               "alt": ("--alt", D(S, "alt text for screen readers"), "value")},
         need=["page"], read_only=False),
    Tool("health", "Objects in error state in the open Desktop model (classes, never error text).", ["health"],
         timeout=600),
    Tool("check", "Data-blind assertions against the open Desktop model (PASS / FAIL / EMPTY / BUDGET / ERROR).",
         ["check"], pos=[("spec", D(O, "checks spec: {\"checks\": [...]} (see reference/checks.md)"), True)],
         opts={"validate_only": ("--validate-only", B, "flag"),
               "diagnostic": ("--diagnostic", B, "flag")},
         timeout=1800),
    Tool("probe", "Shape facts of a table: booleans and coarse buckets.", ["probe"],
         pos=[("table", S, True)], opts={"columns": ("--columns", L, "multi")}, timeout=900),
    Tool("snapshot", "Save or compare HMAC snapshots of queries (SAME / DIFF / NEW / MISSING / STALE_DATA).",
         ["snapshot"], pos=[("action", E("save", "compare"), True), ("name", S, True)],
         opts={"spec": ("--spec", D(O, "queries spec (compare reuses the saved one when omitted)"), "json")},
         timeout=1800),
    Tool("smoke", "Run each visual's query: OK / EMPTY / UNSUPPORTED / ERROR per visual.", ["smoke"],
         opts={"page": ("--page", S, "value")}, timeout=1800),
    Tool("handoff", "Visual review checklist for the human (names only).", ["handoff"],
         opts={"all": ("--all", D(B, "include every pushed change, not just new ones"), "flag")}, read_only=False),
    Tool("evidence", "The `pbz review` packet: pending changes by release class (security, refresh, recalc, logic, metadata, report), "
                   "impact, revision-bound evidence (CURRENT / STALE / NOT_RUN per stage) and coverage; writes "
                   "pbz-workspace/REVIEW.md. Acceptance is the human's (terminal only).", ["review"], read_only=False),
    Tool("set_props", "Typed model properties on one object or a selection: description, displayFolder, formatString, "
                      "isHidden, isKey, dataCategory, summarizeBy, sortByColumn, isAvailableInMdx.", ["set"],
         pos=[("items", D(L, "an optional object ('T'[C], [M], Table, Table.Hierarchy) and prop=value pairs"), True)],
         opts={"kind": ("--kind", E("table", "column", "measure", "hierarchy"), "value"), "table": ("--table", S, "value"),
               "match": ("--match", D(S, "regular expression on names"), "value"), "folder": ("--folder", S, "value"),
               "unset": ("--unset", D(L, "properties to remove"), "append"), "dry_run": ("--dry-run", B, "flag")},
         read_only=False),
    Tool("get_props", "Show the typed properties of one object or a selection.", ["get"],
         pos=[("items", D(L, "an optional object"), False)],
         opts={"kind": ("--kind", E("table", "column", "measure", "hierarchy"), "value"), "table": ("--table", S, "value"),
               "match": ("--match", S, "value"), "folder": ("--folder", S, "value")}),
    Tool("copy", "Duplicate a report page, or a visual or group, with every id remapped.", ["copy"],
         pos=[("kind", E("page", "visual"), True), ("page", D(S, "source page"), True), ("visual", D(S, "visual id or label"), False)],
         opts={"as": ("--as", D(S, "page: display name of the copy"), "value"), "after": ("--after", S, "value"),
               "independent_slicers": ("--independent-slicers", B, "flag"), "to_page": ("--to-page", S, "value"),
               "pos": ("--pos", D(S, "x,y or x,y,width,height"), "value")},
         read_only=False),
    Tool("a11y", "Accessibility audit (alt text, titles, tab order, font sizes, contrast, phone layout) or a tab-order fix.",
         ["a11y"], opts={"page": ("--page", S, "value"), "fix_tab_order": ("--fix-tab-order", B, "flag"),
                         "min_font": ("--min-font", N, "value")}, read_only=False),
    Tool("mobile", "Phone layout of a visual (mobile.json), or every visual stacked in reading order.", ["mobile"],
         pos=[("page", S, True), ("visual", D(S, "visual id, layout label, or all"), True)],
         opts={"pos": ("--pos", D(S, "x,y,width,height"), "value"), "remove": ("--remove", B, "flag")}, read_only=False),
    Tool("hierarchy", "User hierarchies: add, re-level (levels keep their identity), remove.", ["hierarchy"],
         pos=[("action", E("add", "levels", "remove"), True), ("table", S, True), ("name", S, True)],
         opts={"levels": ("--level", D(L, "a column per level, top first"), "append"),
               "names": ("--names", D(L, "level names"), "multi"), "force": ("--force", B, "flag")}, read_only=False),
    Tool("format_expr", "Dynamic format string of a measure (formatStringDefinition), or remove it.", ["format-expr"],
         pos=[("measure", D(S, "[Measure]"), True), ("dax", D(S, "DAX returning a format string"), False)],
         opts={"remove": ("--remove", B, "flag"), "upgrade_compat": ("--upgrade-compat", D(B, "raise the compatibility level (one way)"), "flag")},
         read_only=False),
    Tool("function", "DAX user-defined functions: add, update, rename (fixes call sites), remove, list.", ["function"],
         pos=[("action", E("add", "update", "rename", "remove", "list"), True), ("name", S, False)],
         opts={"params": ("--params", D(S, "(name : TYPE, ...)"), "value"), "body": ("--body", S, "value"),
               "to": ("--to", D(S, "rename: new name"), "value"), "force": ("--force", B, "flag"),
               "upgrade_compat": ("--upgrade-compat", B, "flag")}, read_only=False),
    Tool("recipe", "Run (or check) a recipe: a JSON batch of mirror-editing verbs applied as one transaction, rolled "
                   "back on any failed step, unmet expectation, limit or validation error.", ["recipe"],
         pos=[("action", E("run", "check"), True), ("spec", D(S, "recipe file path or JSON"), True)],
         opts={"params": ("--param", D(L, "name=value"), "append"), "dry_run": _f("--dry-run")}, read_only=False),
    Tool("desktop", "Power BI Desktop control without screenshots: status, open, close, reload.", ["desktop"],
         pos=[("action", E("status", "open", "close", "reload"), True)],
         opts={"wait": ("--wait", D(I, "seconds to wait for the engine"), "value")}, read_only=False, timeout=1800),
    Tool("index", "Build the column classification index inside the engine (Desktop open).", ["index"],
         read_only=False, timeout=3600),
    Tool("refresh", "Recalculate calculated tables / columns in the open Desktop model (never a data refresh).",
         ["refresh"], opts={"tables": ("--tables", L, "multi")}, read_only=False, timeout=3600),
    Tool("synth", "Synthetic test copy of the model with fake data: build (optionally --edge cases), status, open, close, "
                  "query, check (a pbz check spec with visible fake counterexamples), remove.",
         ["synth"], pos=[("action", E("build", "status", "open", "close", "query", "remove", "check"), True),
                         ("dax", D(S, "query: an EVALUATE statement (synthetic model only)"), False)],
         opts={"edge": ("--edge", D(B, "build: edge-case profile"), "flag"),
               "spec": ("--spec", D(O, "check: {\"checks\": [...]}"), "json"),
               "rows": ("--rows", D(I, "build: rows per fact table"), "value"),
               "seed": ("--seed", D(I, "build: random seed"), "value"),
               "wait": ("--wait", I, "value")}, read_only=False, timeout=1800),
    Tool("synth_test", "Measure regression tests on the synthetic model: save golden results of fake data, run and "
                       "compare after a refactor (SAME / DIFF / ERROR, drift reported), list, remove.", ["synth-test"],
         pos=[("action", E("save", "run", "list", "remove"), True), ("name", S, False)],
         opts={"spec": ("--spec", D(O, "{\"queries\": [{\"id\", \"dax\", \"keys\"?}]}"), "json"),
               "measures": ("--measure", D(L, "[Measure] shortcut"), "append"),
               "by": ("--by", D(L, "'Table'[Column] grouping"), "append"),
               "replace": ("--replace", B, "flag"), "tol": ("--tol", N, "value")},
         read_only=False, timeout=900),
    Tool("memscrub", "Preview which memory notes would change (counts only). Showing scrubbed text and applying are human-only.", ["memscrub"],
         opts={"dir": ("--dir", D(S, "memory folder"), "value"),
               "show": ("--show", D(S, "one file to show scrubbed"), "value")}, need=["dir"]),
]
BY_NAME = {t.name: t for t in TOOLS}


class ToolError(Exception):
    pass


def _type_ok(value, sch: dict) -> bool:
    t = sch.get("type")
    if t == "string":
        ok = isinstance(value, str)
    elif t == "boolean":
        ok = isinstance(value, bool)
    elif t == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif t == "array":
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    elif t == "object":
        ok = isinstance(value, dict)
    else:
        ok = False
    return ok and ("enum" not in sch or value in sch["enum"])


def _no_dash(value: str, what: str) -> str:
    if value.startswith("-"):
        raise ToolError(f"{what} cannot start with '-'")
    return value


def build_argv(tool: Tool, args: dict, workspace: str | None, project: str | None) -> tuple[list, str]:
    """CLI argv (after cli.py) and the working directory for one tool call. Raises ToolError."""
    sch = tool.schema()
    props = sch["properties"]
    for k in args:
        if k not in props:
            raise ToolError(f"unknown argument: {k}")
    for k in sch.get("required", []):
        if k not in args:
            raise ToolError(f"missing argument: {k}")
    for k, v in args.items():
        if not _type_ok(v, props[k]):
            raise ToolError(f"argument {k} has the wrong type or value")
    ws = args.get("workspace") or workspace or os.getcwd()
    if not os.path.isdir(ws):
        raise ToolError("workspace folder not found")
    argv = ["--workspace", os.path.abspath(ws)]
    proj = args.get("project") or project
    if proj:
        argv.append(f"--project={proj}")
    argv += tool.verb
    for arg, sch_, _req in tool.pos:
        if arg not in args:
            continue
        v = args[arg]
        if isinstance(v, list):  # a trailing nargs="*" positional: one argv entry per item
            argv += [_no_dash(str(x), arg) for x in v]
        else:
            argv.append(_no_dash(json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else str(v), arg))
    for arg, (flag, _s, mode) in tool.opts.items():
        if arg not in args:
            continue
        v = args[arg]
        if mode == "flag":
            if v:
                argv.append(flag)
        elif mode == "value":
            argv.append(f"{flag}={v}")
        elif mode == "json":
            argv.append(f"{flag}={json.dumps(v, ensure_ascii=False)}")
        elif mode == "append":
            argv += [f"{flag}={x}" for x in v]
        elif mode == "multi":
            if v:
                argv += [flag] + [_no_dash(x, arg) for x in v]
    return argv, os.path.abspath(ws)


def call_tool(name: str, args: dict, workspace: str | None, project: str | None) -> dict:
    tool = BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool: {name}")
    argv, cwd = build_argv(tool, args or {}, workspace, project)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        p = subprocess.run([sys.executable, "-I", CLI, *argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=tool.timeout)
    except subprocess.TimeoutExpired:
        return _result({"ok": False, "error": "TIMEOUT", "detail": f"{name} took longer than {tool.timeout}s"}, True)
    except OSError:
        return _result({"ok": False, "error": "CLI_UNAVAILABLE"}, True)
    text = p.stdout.decode("utf-8", errors="replace").strip()
    if not text:  # argparse errors and crashes write only to stderr, which can echo inputs: never forward it
        err = "BAD_ARGUMENTS" if p.returncode == 2 else "INTERNAL_ERROR"
        return _result({"ok": False, "error": err, "detail": f"see the {name} tool's input schema"}, True)
    return {"content": [{"type": "text", "text": text}], "isError": p.returncode != 0}


def _result(obj: dict, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}], "isError": is_error}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg, workspace: str | None, project: str | None) -> dict | None:
    """One incoming message -> response (None for notifications and client responses)."""
    if isinstance(msg, list):
        return _error(None, -32600, "batch requests are not supported")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    method = msg.get("method")
    mid = msg.get("id")
    if method is None:  # a response to a server request (this server sends none) or junk
        return None if ("result" in msg or "error" in msg) else _error(mid, -32600, "invalid request")
    if not isinstance(method, str):
        return _error(mid, -32600, "invalid request")
    notification = "id" not in msg
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [t.listing() for t in TOOLS]}
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") if params.get("arguments") is not None else {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return None if notification else _error(mid, -32602, "tools/call needs name and an arguments object")
        try:
            result = call_tool(name, args, workspace, project)
        except ToolError as e:
            return None if notification else _error(mid, -32602, str(e))
    else:
        return None if notification else _error(mid, -32601, f"method not found: {method}")
    return None if notification else {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(workspace: str | None = None, project: str | None = None) -> int:
    out = sys.stdout.buffer
    ws = os.path.abspath(workspace) if workspace else None
    sys.stderr.write("pbz mcp: ready\n")
    sys.stderr.flush()
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            resp = _error(None, -32700, "parse error")
        else:
            try:
                resp = handle(msg, ws, project)
            except Exception:  # never let one bad message kill the server or leak a traceback
                resp = _error(msg.get("id") if isinstance(msg, dict) else None, -32603, "internal error")
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            out.flush()
    sys.stderr.write("pbz mcp: stdin closed\n")
    return 0
