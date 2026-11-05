---
name: powerbi-zerodata
description: Data-blind workflow for Power BI PBIP projects (PBIR report JSON + TMDL semantic model). Use for ANY work in a folder holding a .pbip, *.SemanticModel or *.Report - editing measures, columns, relationships, visuals, pages, slicers, filters, themes, bookmarks, renames, debugging wrong numbers, validating, or checking a model - in Claude Code, Codex or any agent. Company data (rows, totals, counts, names, query results, cached data, screenshots) must never enter the agent's context; every read and edit goes through the sanitized pbz-workspace mirror and the `pbz` CLI. Trigger even when the user just says "fix this measure", "move that visual", "why is this card wrong" in a Power BI workspace.
compatibility: Windows; Claude Code and Codex (same SKILL.md). Python 3.11+, .NET 10 for the engine, Power BI Desktop for engine checks.
---

# Power BI zero-data workflow

You work on Power BI projects **without ever seeing company data**. You see every name and every
formula. You never see values: no rows, totals, counts, distinct values, query results, cached
data, screenshots of populated visuals, or error text that echoes values.

CLI: `${CLAUDE_SKILL_DIR}/bin/pbz.cmd` (Codex and others: `%USERPROFILE%\.agents\skills\powerbi-zerodata\bin\pbz.cmd`).
Below it is written as `pbz`. Run it from the workspace folder (the one holding the `.pbip`).
Every command prints JSON with metadata or closed-vocabulary verdicts only.

## Hard rules

1. Edit only files under `pbz-workspace\`. Never read or edit the real `*.SemanticModel\` /
   `*.Report\` folders, `*.bak-*` copies, `.pbi\` caches, data files (xlsx, csv, zip, pbix, ...),
   images, documents, or anything `.pbz\policy.json` denies. The guard hook blocks these; do not
   look for ways around it (copies, other shells, scripts, encodings).
2. Never query a model or service yourself: no DAX query view, no ADOMD/XMLA/TOM scripts, no
   executeQueries, no OData pulls, no Power BI MCP servers, no `powerbi-desktop screenshot`. Use
   `pbz` verbs; they return verdicts, never values.
3. Never take screenshots or read UI/browser pages of Power BI Desktop or the service. The user
   verifies visuals; you give them a checklist (`pbz handoff`).
4. Never write data values (amounts, percentages, counts, people or project names) into memory,
   notes, docs, commit messages or chat summaries. Describe findings qualitatively and by object
   name ("card c0 used a dead field", not its number).
5. Values the user types in chat are theirs to share; use them for this task only and do not
   persist them.
6. Do not delegate Power BI work to agents or services outside this guard (external proxies,
   copilots, web models). Subagents in this session are fine.

## Start of every session

```
pbz status          # workspace, mirror state, Desktop, engine, guard
pbz pull            # refresh the mirror from the real files (refuses if you have unpushed edits)
```
First time in a workspace: `pbz init` (policy + scan + first pull). If `status` shows
`classification: heuristic`, ask the user to open the project in Desktop, then run
`pbz desktop open`, `pbz index`, `pbz pull --force` for exact classification.

## The loop

1. **Orient** (metadata only): read files in `pbz-workspace\`; `pbz describe <Table>`,
   `pbz describe "[Measure]"` (DAX, dependencies, visuals using it),
   `pbz describe "'Table'[Column]"` (type, class, buckets, category labels), `pbz values "'T'[C]"`.
2. **Edit** the mirror with your normal file tools. TMDL is tab-indented; PBIR is JSON. For
   renames use `pbz rename measure|column <old> <new> [--table T]` (fixes every reference).
3. **Review**: `pbz diff --patch`, `pbz validate` (lint + field references on the mirror).
4. **Apply**: close Desktop first for model changes (`pbz desktop close`), then `pbz push`.
   Report-only changes with Desktop open: `pbz push --reload`. Push stages the real files,
   validates them (TOM, PBIR schema, references, lint, tokens), backs up, then writes.
5. **Verify without data**: `pbz desktop open --wait 300` (after model changes, `pbz refresh`
   recalculates calculated tables/columns; source-data refresh is the user's Refresh button), `pbz health` (objects in error),
   `pbz check spec.json` (assertions), `pbz smoke --page <name>` (each visual OK/EMPTY/ERROR),
   `pbz snapshot save|compare` around refactors.
6. **Hand off**: `pbz handoff` writes `pbz-workspace\HANDOFF.md`. Give it to the user; they
   check the real visuals and tell you what looks wrong in words. `pbz review` writes
   `pbz-workspace\REVIEW.md`: changes by release class, impact, and which checks ran on which
   revision (stale evidence is marked STALE); the user accepts with `pbz review --accept`.

## Tokens in the mirror

Sensitive literals appear as tokens; everything else is the real text.
- `«Table[Column]#n»` one specific value of that column. Keep, move or copy it freely.
- `«lit#n»` sensitive text not tied to a column; `«pii#n»` email/user; `«num#n»` a figure in
  free text; `«payload:...»` embedded data. Do not edit inside payloads.
- Category labels (`"Active"`, `'Project'`, `'Current Year'`, `"PTO"`) stay readable.
- New plain literals you type are written as-is. Invented tokens make push fail (`UNKNOWN_TOKEN`).
Details: [reference/tokens.md](reference/tokens.md).

## Debugging wrong numbers blind

You never ask "what is the value". You ask whether two formulations agree, at which grain they
diverge, and whether the data has a shape problem. See [reference/checks.md](reference/checks.md).
1. `pbz snapshot compare <name>` after a refactor: which queries/columns changed.
2. `pbz check` with `equal` at finer grains (Year -> Quarter -> Month -> category columns);
   mismatch comes back as `NONE/ONE/FEW/MANY` plus group keys (date and category columns) or tokens.
3. Identity checks: `additive`, `filter_independent`, `no_orphans`, `unique`, `no_blank_keys`.
4. `pbz probe <Table>` for blanks, duplicate keys, orphans, negatives, future dates (booleans +
   coarse buckets).
5. Fix, re-check, `snapshot compare` = SAME where nothing should change, then `pbz handoff`.
Constants in checks are limited (3 distinct per target, remembered for 30 days, including literals
you pushed into measures the target reaches) and checks, probes, snapshot queries and smoke runs have
daily budgets; `BUDGET` means stop probing and ask the user. There is no `between`. To test logic
freely, use `pbz synth` instead of the real model.

## Verdicts and what to do

| Output | Meaning | Next step |
|---|---|---|
| `CONFLICT` (push) | real files changed since pull (Desktop saved) | `pbz pull --rebase`, fix markers, push |
| `DESKTOP_OPEN` | Desktop holds the project | `pbz desktop close` (if `WAITING_ON_DIALOG`, ask the user) |
| `INVALID` + findings | staged validation failed, nothing written | fix the mirror, push again |
| `UNKNOWN_TOKEN` | a token in the mirror is not in the map | restore the original token or use a plain literal |
| `ENGINE_UNAVAILABLE` | Desktop not open / engine not built | `pbz desktop open`; tell the user if it persists |
| `ERROR:<class>` | engine error class (SYNTAX, UNKNOWN_OBJECT, ...) | fix the expression; raw text is human-only |
| `BUDGET` | probe limit reached | summarize what you know, ask the user |
| `DESKTOP_DIRTY` (push --reload) | Desktop has unsaved changes a reload would discard | ask the user to save or close Desktop |
| `AMBIGUOUS_PATH` / `TYPE_MISMATCH` / `DUPLICATE` (relationship) | the relationship would break or duplicate the model | add it `--inactive`, fix the columns, or `set` the existing one |
| `NO_FREE_SPACE` (add visual placement) | the page is full | pass `--pos x,y,w,h` or make room (`pbz layout`) |
| `INCOMPLETE` (unused --remove) | a report file or a registered consumer could not be read or fully covered, so nothing is provably unused | `pbz consumers list`: fix the file or register the missing definitions, rerun |
| `NOT_REGISTERED` / `NO_CONTRACT` (add visual) | the custom visual is not registered in report.json, or its data roles are unknown | `pbz visual import <file.pbiviz> --as public\|private`, or `pbz visual register <guid> --role ...` |
| `PENDING_PQ_CHANGES` (push) | Desktop holds Power Query edits that were never applied | ask the user to apply or discard them in Desktop and save, then `pbz pull --rebase` |
| `COMPAT_LEVEL` | the feature needs a newer model compatibility level (formats 1601, functions 1702) | ask the user before `--upgrade-compat` (one way) |
| `IN_USE` (hierarchy / function remove) | visuals use the hierarchy, or DAX calls the function | rewrite the users first, or `--force` |
| `ROLLED_BACK` (recipe) | a step failed, an expectation or limit was not met, or validation failed | read `reason`; the mirror is as before the recipe |
| `CONFLICT` (unused --restore) | the mirror changed after the removal | restore later points first, or pull |
| `FORMAT_STRING_CONFLICT` | a measure has both a static and a dynamic format string; Desktop shows "Issues were found" and does not open the model (TOM accepts it) | `pbz repair` drops the static one; `format-expr` already replaces it |
| guard deny `[pbz guard] ...` | you tried a blocked path or command | follow the hint; never retry variants |

## Refactor and author (edits the mirror; then `pbz diff`, `pbz validate`, `pbz push`)

Prefer these over hand edits: they keep every cross-file reference consistent. Details:
[reference/authoring.md](reference/authoring.md).

| Verb | Does |
|---|---|
| `pbz rename measure\|column\|table <old> <new>` | renames and fixes DAX, M, relationships, RLS, perspectives, PBIR entities and queryRefs |
| `pbz move measure <name> --to <table>` | moves a measure (for example into a measure table), fixing visuals and qualified refs |
| `pbz new measure-table\|date-table\|calc-group\|field-parameter <name> ...` | Desktop-shaped scaffolds with fresh lineageTags (date table: `--start/--end` years, `--relate 'T'[C]`) |
| `pbz relationship add\|remove\|set <'T'[C]> <'U'[K]> [--inactive] [--both]` | relationship edits with type, duplicate and ambiguous-path guards |
| `pbz add page <name> [--tooltip \| --drillthrough 'T'[C]]` / `pbz add visual <type> --page <p> --field ...` | pages (plain, report-page tooltip, drillthrough target with a Back button) and visuals of any built-in type (roles, kinds and field limits from Microsoft's core visual capabilities) or a registered custom visual |
| `pbz link <page> <visual> --to-page P \| --back \| --bookmark B \| --drillthrough P \| --url U \| --tooltip P \| --clear` | button actions and report-page tooltips |
| `pbz filter add 'T'[C] --report\|--page P [--visual V] --in ... \| --not-in ... \| --where ">= 100" \| --top N --by [M]`, `remove`, `list` | report, page and visual filters (values: labels, typed values or mirror tokens) |
| `pbz interaction <page> <source> <target\|all> filter\|highlight\|none\|default` | how one visual affects others on its page |
| `pbz bookmark add <name> --page P [--hide v ...] [--only v ...]`, `remove`, `list` | show/hide bookmarks (never data state) for buttons |
| `pbz copy page <p> --as <name>` / `pbz copy visual <p> <v> [--to-page P]` | duplicate pages, visuals and groups with every id remapped |
| `pbz a11y [--page P] [--fix-tab-order]`, `pbz mobile <p> <v\|all> [--pos x,y,w,h]` | accessibility audit (alt text, titles, tab order, fonts, contrast) and phone layout |
| `pbz set <obj> prop=value ...` / `pbz get <obj>` (or `--table/--kind/--match/--folder`) | typed model properties: description, displayFolder, formatString, isHidden, isKey, dataCategory, summarizeBy, sortByColumn, isAvailableInMdx |
| `pbz hierarchy add\|levels\|remove`, `pbz format-expr [M] "<DAX>"`, `pbz function add\|update\|rename\|remove\|list` | hierarchies, dynamic format strings (replace the static formatString: Desktop refuses a measure with both), DAX user-defined functions |
| `pbz consolidate merge "[Copy]" --into "[Keep]"`, `pbz consolidate families`, `pbz consolidate calc-group --family <id>` | merge duplicate measures; turn measure families into a calculation group with wrapper measures and a check spec ([reference/audit.md](reference/audit.md)) |
| `pbz recipe run <file.json> [--param k=v]` | a JSON batch of mirror edits applied as one transaction, rolled back on any failure ([reference/workflow.md](reference/workflow.md)) |
| `pbz visual list\|roles <type>\|import <file.pbiviz> [--as contract\|public\|private]\|register <guid> --as public\|organizational [--role NAME:kind]` | custom visuals: contracts (data roles), registration in report.json, private packages installed by push ([reference/visuals.md](reference/visuals.md)) |
| `pbz style <page> <visual>\|--type T\|--all --set object.property=value [--series [M]] [--state hover]`, `--unset`, `--show`, `--list --type T` | visual formatting (titles, data labels, colors, fonts, axes, legends, borders) checked against Microsoft's formatting catalog ([reference/style.md](reference/style.md)) |
| `pbz layout [--page <p>]` | text canvas map of a page plus overlap / off-canvas / z-order lint: how you "see" a page |
| `pbz format [<measure>] [--apply]` | local, token-safe DAX formatting (check by default) |
| `pbz repair [--fields] [--lineage]` | fixes page order, names, groups, interactions, bookmark lists, stale targets, copied lineageTags |
| `pbz bpa --fix <RULE> [--dry-run]` | property-only BPA fixes (hide foreign keys, summarizeBy, isAvailableInMdx, mark date table) |

`pbz status` shows `sync` (IN_SYNC, PENDING_PUSH, BEHIND, CONFLICT_RISK) and, with Desktop open,
whether the loaded model matches the files. `push --reload` refuses while Desktop has unsaved changes.

More editors ([reference/governance.md](reference/governance.md)):
- `pbz culture add|remove|list|missing`, `pbz translate <code> <object> --caption ...`: translations
  (culture files are mirrored; the Q&A linguistic schema stays one opaque token).
- `pbz perspective add|remove|include|exclude`, `pbz role add|remove|filter|unfilter|member-add|member-remove|ols`.
- `pbz refresh-policy set|remove|show`: incremental refresh (parameters, filter step, windows).
- `pbz push --only <paths>` pushes part of the pending edits; `pbz revert [paths] [--object ...]`
  discards mirror edits.

## Debugging DAX on fake data: `pbz synth`

`pbz synth build` writes a synthetic copy of the model (from the mirror, fake rows, outside the
workspace, marked synthetic); `pbz synth open`, then `pbz synth query "EVALUATE ..."` returns real
query results of that fake data, so you can see what a measure does. It refuses any model that is not
its own synthetic copy. `pbz synth test save <name> --measure "[M]" --by "'T'[C]"` stores golden
results of that fake data; after a refactor, rebuild and `pbz synth test run <name>` reports SAME / DIFF
per query. `pbz synth build --edge` plants blanks, orphans, zero and negative values and boundary dates;
`pbz synth check '<check spec>'` runs a `pbz check` spec on that copy with visible fake counterexamples,
so a spec can be debugged before it runs on real data for verdicts. Details:
[reference/synth.md](reference/synth.md). Agent hosts that prefer typed tools can run `pbz mcp` (see
[reference/mcp.md](reference/mcp.md)).

## Model hygiene and analysis (metadata only)

These read the mirror and print names, states, counts, scores and rule ids. Details:
[reference/analysis.md](reference/analysis.md), [reference/bpa.md](reference/bpa.md).

| Verb | Use it for |
|---|---|
| `pbz unused [--all] [--report <dir>]` | state of every object: used / used_external / locked / used_by_unused / unused / unknown |
| `pbz consumers add <path> \| --object <ref> --label <l>`, `discover <folder> [--register]`, `list`, `scan`, `show <id>` | find and register external consumers (thin reports, PBIX, Excel, RDL, composite models, query exports, a scanner result) so `unused` keeps what they use ([reference/consumers.md](reference/consumers.md)) |
| `pbz lineage <obj> [--up]` | where-used tree (default) or what it depends on |
| `pbz unused --remove [--dry-run] [--cascade] [--only kinds] [--m-step remove\|keep\|none]` | delete unused objects from the mirror, with a restore point |
| `pbz unused --restore <stamp>` | undo a removal (before or after push) |
| `pbz suggest` | cleanup suggestions (auto date/time, unused variables, hidden pages, key columns...) |
| `pbz bpa [model\|report] [--labels measure-killer]` | best-practice findings, score and grade |
| `pbz similar` | identical, alias, same-shape and near-duplicate measures |
| `pbz audit [--category efficiency\|consolidation] [--since <baseline>] [--fail-on new]`, `pbz audit memory`, `pbz audit waive <id> --reason ...` | efficiency and consolidation findings with evidence, fix and verify; storage bands from Desktop; baselines and waivers ([reference/audit.md](reference/audit.md)) |
| `pbz changes [--from previous --to last]` | semantic diff of mirror snapshots (pending edits, or what changed between pulls) |
| `pbz docs [--format md\|html\|json] [--dax]` | documentation into `pbz-workspace\docs\` |
| `pbz m-lineage [table]` | Power Query steps, column origins, columns the query drops or the model lacks |
| `pbz unused --verify-engine` | cross-check the dependency parser against the open Desktop engine |

"Unused" means unused in the scanned reports, the model and the registered consumers. Ask the user
which service reports, Excel files, paginated reports and composite models use the model and register
them (`pbz consumers add <path>`; the user can export them, see reference/consumers.md). What a
consumer uses becomes `used_external` and is never removed; an unreadable or opaque consumer makes the
analysis incomplete. Consumers nobody registered stay invisible, so ask the user before a removal and
push it like any other edit (`pbz diff`, `pbz push`). Only the user removes a consumer
(`pbz admin consumer-remove <id>`).

## Memory hygiene

Before saving a memory note about this work, keep only technical knowledge: object names, file
paths, patterns, gotchas, decisions. To clean old notes: `pbz memscrub --dir <memory folder>`
(preview), `--show <file>` (scrubbed text), `--apply` after the user agrees.

## Reference

- [reference/checks.md](reference/checks.md): check spec format, kinds, outputs, snapshot specs.
- [reference/tokens.md](reference/tokens.md): token grammar and push rules.
- [reference/gotchas.md](reference/gotchas.md): TMDL / PBIR / DAX / Desktop gotchas.
- [reference/leak-vectors.md](reference/leak-vectors.md): what is blocked and why.
- `pbz admin ...` is for the user in their own terminal (budget reset, allow paths, mark category
  columns, engine raw log, consumer removal). Never run it yourself; the guard blocks it.
- Changing this skill: open a session in your clone of the `pbi-zerodata` repo (a
  synthetic tree; the guard treats its fixtures as fake data). From a real workspace session the
  skill, hooks and agent rule files are read-only.
