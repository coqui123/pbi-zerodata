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
   check the real visuals and tell you what looks wrong in words.

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
Constants in checks are limited (3 distinct per target per day) and there is a daily budget;
`BUDGET` means stop probing and ask the user. There is no `between`.

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
| guard deny `[pbz guard] ...` | you tried a blocked path or command | follow the hint; never retry variants |

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
  columns, engine raw log). Never run it yourself; the guard blocks it.
- Changing this skill: open a session in your clone of the `pbi-zerodata` repo (a
  synthetic tree; the guard treats its fixtures as fake data). From a real workspace session the
  skill, hooks and agent rule files are read-only.
