# pbz parity plan: Measure Killer features, metadata only

Research date 2026-11-04. Inputs: Measure Killer 2.9.4 public documentation and install layout (no
proprietary code inspected), public docs, research passes over related open-source repositories
(read through public APIs; nothing cloned for redistribution).

## Implementation status (2026-11-04)

Phases 0 to 3 are built. Modules: `pbz/deps.py` (DAX / M reference resolver), `pbz/usage.py`
(usage graph and states), `pbz/unused.py` (`unused`, `lineage`, `--remove`, `--restore`),
`pbz/suggest.py`, `pbz/bpa.py` + `pbz/rules/*.json`, `pbz/similar.py`, `pbz/changes.py` (on new
mirror history snapshots taken at every pull and push), `pbz/docs.py`, `pbz/mlineage.py`,
`pbz/engdeps.py` + engine verb `deps` (contract v1.2). Tests: `tests/test_parity.py`,
`tests/test_bpa.py`, `tests/test_tools.py`, new live cases in `tests/test_engine.py`, new steps in the
canary e2e. Deviations from the plan below:

- M lineage is a pure-Python let-step analyser over the sanitized mirror, not a Node worker on
  `@microsoft/powerquery-parser`, so no npm download is needed. Steps it cannot follow are reported
  as `dynamic`.
- Bookmarks nothing points at are `unreferenced`, not `unused` (viewers can open them from the
  Bookmarks pane), and are removed only with `--only bookmark`.
- A table is removable only when nothing outside it, other than its own relationships, refers to it
  or to one of its objects; relationships are kept (locked) only when both of their tables survive.
- Translations in `cultures/` are not pruned: cultures are never mirrored, so objects with a
  translation are skipped as manual removals (pbz reads the real culture files for names only). TOM
  on push also refuses a removal that would leave a dangling translation (verified).

- BPA catalog: 53 model rules (42 on by default, 6 opinion, 5 data-derived and never run) and 21
  report rules (20 on, 1 opinion). Every rule citing `BPARules.json` uses the upstream rule id
  (checked against the file on 2026-11-04); other rules cite semantic-link-labs or are pbz's own.

Verified on an internal development mirror (counts and object names only, no values retained in this document): `unused`, `lineage`, `suggest`, `bpa`,
`similar`, `docs`, `m-lineage` and `unused --remove --dry-run` ran without errors and changed no
file; the graph had no unresolved references or unknown fields; an independent text cross-check
found zero objects called unused that a qualified DAX reference or an entity-matched report field
still uses. A heuristic-mode canary scan (no engine index) found two tokenizer gaps, now fixed: RLS
filters had no home table for bare `[Column]` references, and string cells of embedded tables under
headers the model does not know were kept visible (they are now tokenized when they look specific).

## 0. Ground rules

- Every feature runs on metadata: the sanitized `pbz-workspace` mirror for anything the agent sees,
  the staged real files inside `pbz` only when a rule must look at literal text. Output is object
  names, ids, states, rule ids, object counts and fixed buckets. Never values.
- Measure Killer features that need data are excluded or reduced to policy-gated buckets: VertiPaq
  memory sizes, views and load times, Excel activity, DAX query execution.
- Tenant / service features (workspace inventory, scanner API, capacity metrics) are out of scope
  until an account with workspace rights exists; they would be metadata APIs behind the engine.
- Unused means "unused in the scanned reports". Every unused result carries `scope.unseen_consumers`.
  An incomplete analysis is `unknown`, never "unused", and blocks automatic removal.

## 1. Parity matrix

| # | Measure Killer feature | pbz command | Zero-data treatment | Main references (all MIT unless noted) | Phase / effort |
|---|---|---|---|---|---|
| 1 | Unused measures, columns, tables, calc columns, relationships, unloaded queries; states used / unused / used-by-unused / remove-manually | `pbz unused` | metadata graph; adds `locked` and `unknown` | semantic-link-labs `semantic_model/_find_unused_objects.py` (`_build_usage_context`, `_expand`), `tom/_model.py` (`used_in_*`, `is_field_parameter`); Tabular Editor 2 `TOMWrapper/Utils/FormulaFixup.cs` (`ParseExpression`, `BuildDependencyTree`), `DaxDependencyHelper.cs` (`GetDAXProperties`); pbi-lineage-lenz `pbirParser.js` (`extractFieldReferences`), `fieldParameters.js`, `impactAnalysis.js` (`findOrphans`) | 1 / L |
| 2 | Where-used, dependency tree, impact | `pbz lineage <obj> --up/--down` | same graph | pbi-lineage-lenz / pbip-lineage-explorer `graph/impactAnalysis.js` (`bfsTraverse`, `getUpstream`, `getDownstream`); semantic-link-labs `_model_dependencies.py` (`measure_dependency_tree`) | 1 / S |
| 3 | Cleanup: kill DAX with backup, clean TMDL, 1-click PBIP, M column-removal step, hide unused columns | `pbz unused --remove [--m-step remove/keep/none] [--hide]` | edits the mirror only; `pbz push` validates and backs up; restore file per run | semantic-link-labs `tom/_model.py` (`remove_object`, `_reduce_model`); pbir-utils `bookmark_utils.py`, `visual_utils.py`, `page_utils.py`; Measure Killer docs (append a removal step, never rewrite the source query) | 2 / M |
| 4 | Suggestions (16): unused VARs, auto date/time, self-only field parameters, dead-end relationships, unreferenced hidden pages/visuals, auto-detected relationships, unapplied filters, key-only columns, IsAvailableInMdx | `pbz suggest` | graph + rule findings, prioritized; savings shown as cardinality class from `pbz index` buckets, not bytes | Measure Killer suggestion list (concepts); pbir-utils sanitizers | 1 / M |
| 5 | Model best practices + score + grade | `pbz bpa model` | Python predicates over Model; data-derived rules off (coverage shown) | microsoft/Analysis-Services `BestPracticeRules/BPARules.json` (rule ids, severities); semantic-link-labs `_model_bpa_rules.py`, `_model_bpa.py`; TE2 `BestPracticeAnalyzer/Analyzer.cs` (ignore annotations, duplicate ids) | 1 / M |
| 6 | Report best practices + score + grade | `pbz bpa report` | predicates over PBIR; literal-sensitive rules run on staged files, report names only | NatVanG/fab-inspector `Rules/Base-rules.json`, `Inspector.cs`; pbir-utils `defaults/rules.yaml`, `sanitize.yaml`, `rule_engine.py`; semantic-link-labs `report/_report_bpa_rules.py` | 1 / M |
| 7 | Duplicate DAX, model/report similarity | `pbz similar` | token canon + weighted similarity; literal slots reported as "differs", never values | BPARules `AVOID_DUPLICATE_MEASURES`; berriosb/powerbi-orchestrator-mcp `validation/model_refactor.py` (skeleton idea only); Measure Killer similarity weights (concept) | 2 / M |
| 8 | Broken DAX, broken visuals | `pbz health`, `pbz validate`, `pbz smoke` | already shipped | n/a | done |
| 9 | Change detection (Measure Killer has none beyond saved snapshots) | `pbz changes --from --to` | object-level semantic diff of sanitized snapshots; literal changes counted, not shown | ALM Toolkit `AlmToolkit/BismNormalizer/TabularCompare/TabularMetadata/Comparison.cs`, `TabularObject.cs`, `Relationship.cs`, `Enums.cs`; pbip-lineage-explorer `packages/core/src/diff` (`changeDetector.detectChanges`); ksbring/pbir-rda `tmdl_normalizer.py` (license to confirm) | 2 / M |
| 10 | Documentation export (Excel, JSON, chart) | `pbz docs --format md/html/json` | mirror only, tokens stay tokens, DAX behind a flag | ViciusLio/pbi-semantic-doc `generator.py`; JonathanJihwanKim/pbip-documenter `doc-generator.js`; arsmagistralis/tmdl-lens | 2 / S |
| 11 | Power Query: unloaded / unused queries, column usage in merges, dynamic M parameters | `pbz m-lineage` (+ feeds `unused`) | Node worker over the sanitized M, no fs writes, no network; strings only from column-name slots | microsoft/powerquery-parser `task/taskUtils.ts` (`tryLexParse`), `language/ast/ast.ts`, `identifierUtils.ts`; powerquery-language-services `inspection/scope/scopeInspection.ts` (later, for M rename) | 3 / L |
| 12 | Engine dependency cross-check | engine verb `deps` | `DISCOVER_CALC_DEPENDENCY` object-identity columns only (never `EXPRESSION`, `QUERY`) | VertiPaq-Analyzer `Dax.Model.Extractor/DmvExtractor.cs` (`PopulateReferences`); semantic-link-labs `get_model_calc_dependencies` | 3 / S |
| 13 | Model size / potential savings | `pbz unused --impact` | cardinality and row buckets from `pbz index`; storage DMVs stay forbidden | n/a (Measure Killer uses VertiPaq stats) | 3 / S |
| 14 | `.measurekiller` analysis files | guard default deny | they hold user emails and column sizes | n/a | 0 / S |

Not adopted: daxnoob.blog and powerbi-model-audit (no license), data-goblin agentic plugins (GPL +
extra terms), pbi-tools (AGPL), TabularEditor/BestPracticeRules text (no license: cite ids only).

## 2. Phase 0: foundation (prerequisite for everything above)

Fixed during this research (already in the working tree, not yet committed):
- Mirror leak: TMDL `kpi` expressions, calc-group selection expressions and any unknown
  `name = value` property were not treated as expression regions, so their literals were not
  tokenized. Now every such line is lexed and tokenized (fail closed). Test added.
- Mirror tokens `«...»` were lexed as `«` + identifier + `[...]`, producing fake references in
  `describe`, `lint` and possibly `rename`. Both lexers now emit an opaque `pbztok` token.

Remaining foundation work (new module `pbz/deps.py`, shared by describe, lint, rename, unused):

| Gap (verified against our code) | Fix | Reference |
|---|---|---|
| Table/column pairing requires adjacency, so `'T' [C]` yields nothing | pair by significant-token index; quoted table names always qualify; bare identifiers only if not a keyword and not followed by `(` | TE2 `ParseExpression` |
| No function / keyword class (`DESC`, `ROWS`, `REL` read as tables) | keyword + function list from TE2 `DAXLexer.g4` and SQLBI `Dax.Tokenizer` | TE2 lexer, sql-bi/Vpax-Obfuscator `src/Dax.Tokenizer` (MIT) |
| No VAR / UDF-parameter scope (`VAR t = ... t[Hours]`) | scope stack; extension columns from ADDCOLUMNS / SELECTCOLUMNS / SUMMARIZECOLUMNS | own |
| Unqualified `[x]` resolution order | home column, home measure, any measure (calculated column / RLS: home column first; iterator over T: T columns first) | TE2 `ParseExpression` |
| Unterminated `'` or `[` swallows the rest | stop at newline | TE2 lexer |
| `dt"..."` date literals | new literal kind | TE2 lexer |
| `load_model` keeps one DAX region per object; format strings, detail rows, KPIs ignored for deps | keep all regions per object, tagged by property | TE2 `GetDAXProperties` |
| `pbir.field_of` misses `PropertyVariationSource` (auto date hierarchy), `GroupRef`, `TransformTableRef`, `Native*` DAX strings; drops hierarchy name | extend per semanticQuery 1.4.0 schema | microsoft/json-schemas `semanticQuery/1.4.0` |
| `field_usage` reads only page.json and visual.json | path-aware `walk_fields_with_path` over every PBIR file: report.json, bookmarks, mobile.json, reportExtensions.json; `via` from the JSON key path | pbi-lineage-lenz `extractFieldReferences` / `VIA` |
| TMDL nodes have no end line | add `Node.end_line` for block removal | own |
| TMDL defaults omitted (cardinality many/one, isAvailableInMdx true) | apply defaults when reading props | BPA pitfalls |
| Mirror history: `mirror-base` is overwritten each pull | `mirror-history/<project>/<stamp>/`, sanitized text, sha-deduplicated, keep last 10 | own |

Usage graph (`pbz/usage.py`):
- `Key(kind, table, name)` case-insensitive; kinds table, column, measure, hierarchy, calc_item,
  relationship, role, m_query, function, report_measure, bookmark, custom_visual, page, visual.
- `Edge(src, dst, via, where)` directed consumer to dependency, with evidence (file + span or JSON
  path) and confidence; `locks{key: [reasons]}`; `usages{key: [Usage(report, page, visual, via)]}`.
- Layers: model expressions (all DAX properties), structural (relationships, sort-by, hierarchy
  levels, variations, alternateOf, calendars), report usage (every PBIR file + report-level measures),
  Power Query (phase 3).

## 3. Feature specs

### 3.1 `pbz unused` (phase 1)
- Roots: report usages (all `*.Report` folders bound to the model by `definition.pbir byPath`, plus
  `--report <dir>` for thin reports bound byConnection) and locks.
- Locks (with reasons): relationship columns, sort-by targets, hierarchy levels, RLS / OLS
  references, used field-parameter tables (all `NAMEOF` targets), calc groups with selection
  expressions or any used item, marked date table key, `alternateOf`, refresh-policy parameters,
  calendar column groups, Direct Lake `expressionSource`.
- Closure: breadth-first over dependencies with a visited set (cycles safe). States: `used`
  (reached from a report root), `locked` (reached only from locks), `used_by_unused` (not reached,
  referenced by a dead object; `blocked_by` listed), `unused`, `unknown` (parse error, unresolved
  reference, unsupported schema, dynamic string-built reference).
- Special cases: `SELECTEDMEASURE` credits the whole calc group; `ISSELECTEDMEASURE([m])` is an
  edge; an inactive relationship no `USERELATIONSHIP` / `CROSSFILTER` names is unused; hidden is
  not unused; unused calculated-table columns are "remove manually".
- Output (names and object counts only):
  ```json
  {"verb": "unused", "scope": {"reports": ["Sales", "Thin A"], "unseen_consumers": ["service reports", "Excel", "paginated", "composite models"]},
   "summary": {"measure": {"used": 0, "unused": 0, "used_by_unused": 0, "locked": 0, "unknown": 0}},
   "objects": [{"kind": "measure", "table": "Sales", "name": "Margin %", "state": "unused", "depends_on": ["[Margin]"]},
               {"kind": "column", "table": "Date", "name": "MonthNo", "state": "locked", "locks": ["sortBy:Date[Month]"]}],
   "report_objects": [{"kind": "bookmark", "report": "Sales", "name": "b1", "state": "unused"}],
   "warnings": [{"code": "AMBIGUOUS_REF", "where": "tables/Sales.tmdl:42"}]}
  ```

### 3.2 `pbz lineage` (phase 1)
- `--down` (impact / where-used): reverse edges plus report usages, grouped report > page > visual.
- `--up`: dependencies to columns, `sourceColumn`, partition, shared M queries (phase 3 adds M steps).
- Tree nodes `{key, via, children, cycle?}`; a graph rendered as a tree keeps shared parents.

### 3.3 `pbz unused --remove` (phase 2)
- Edits the mirror only, writes `pbz-workspace/.pbz-trash/<stamp>.json` with the removed TMDL blocks,
  then the usual `pbz diff` and `pbz push` (stage, validate, back up, write).
- TMDL: delete node span including `///` descriptions, prune perspective entries, delete the table
  file and its `ref table` line; push prunes translations in `cultures/` on the real side from the
  list of removed keys (the agent never sees cultures).
- PBIR: report-level measures, bookmark files plus `bookmarks.json` entries, unused
  `publicCustomVisuals`, hidden pages and visuals never shown (pbir-utils logic, with sync-slicer
  protection added).
- M (import tables): append `#"pbz Removed Columns" = Table.RemoveColumns(<last>, {...}, MissingField.Ignore)`
  and repoint `in` (keep mode: `Table.SelectColumns`). Skip with a reason when another query
  references the query, the table has an incremental refresh policy (then also `sourceExpression`),
  the body is not `let ... in`, or the table is Direct Lake / calculated.
- Validation adds an ALM-style blocking check: refuse to delete an object something still references.

### 3.4 `pbz bpa model` / `pbz bpa report` (phase 1)
- Rule catalog `pbz/rules/model.json`, `pbz/rules/report.json`; logic in `pbz/bpa.py` predicates:
  ```json
  {"id": "HIDE_FOREIGN_KEYS", "category": "Formatting", "scope": "column", "severity": 2,
   "evidence": "metadata", "applies": "visible_column", "check": "many_side_key",
   "message": "{table}[{name}] is a visible many-side key", "fix": {"set": {"isHidden": true}},
   "source": "microsoft/Analysis-Services HIDE_FOREIGN_KEYS (MIT)"}
  ```
- About 26 model rules (auto date/time, unmarked date table, floats, summarize-by, visible FKs,
  relationship key types, bidirectional / M:M ratio, never-activated inactive relationships, isolated
  tables, snowflake, unused hidden objects, calculated columns / tables, DIVIDE, IFERROR /
  EVALUATEANDLOG / INTERSECT, reference qualification, duplicate and alias measures, FILTER over a
  table, SUM on text, RLS cost patterns, IsAvailableInMdx, month sorting, format strings,
  descriptions, name hygiene and broken expressions) and 13 report rules (pages, bookmarks, visuals
  per page, fields per visual, TopN / advanced / measure filters, show items with no data, visible
  tooltip / drillthrough pages, tall pages, unused custom visuals, hard-coded colors, implicit
  measures, invalid references and report-level measures, dead bookmarks / hidden visuals / hidden
  pages, pie / donut, default page names, alt text off by default). Data-derived rules (split
  date/time, partition large tables, high-cardinality bidirectional, long text, referential
  integrity) are off; when enabled they get booleans from the engine only.
- Honor Tabular Editor's `BestPracticeAnalyzer_IgnoreRules` annotation; accept BPARules ids in an
  overlay file; opinion rules (integer / percentage / date format conventions) default off.
- Pitfalls to encode in tests: word-boundary matching ("Date" vs "Updates", "city" vs "Capacity"),
  TMDL defaults, auto-date tables / field parameters / calc groups / measure tables misfiring
  isolation and calc-table rules, text measures and format-string rules.

### 3.5 Scores and grades (phase 1)
- Finding = (rule, object), deduplicated; suppressed findings listed separately.
- Model parity score, as in Measure Killer's bundled docs:
  `score = sum(severity x violations) x log10(tables + columns + measures + relationships)`,
  grades at 75 / 300 / 600 / 900 / 1200.
- Report parity score: `severity x (1 + excess)` for threshold rules, grades at 50 / 100 / 150 / 250 / 300.
- Also `per100` (score per 100 objects) and `rate` (0-100, size independent) for comparing models.
- Grade labels configurable: neutral defaults `A` to `F`; a `measure-killer` preset reproduces its six
  labels ("Power BI Pro" down to "Power BI Criminal") for side-by-side comparison.
- Output carries `ruleset` hash and `coverage` (rules evaluated / applicable / skipped as data-derived);
  scores are only comparable with the same ruleset.

### 3.6 `pbz similar` (phase 2)
- `canon()` on the fixed lexer: drop trivia, uppercase functions, casefold names, resolve `[c]` to
  `T[c]`, numbers as floats, tokens as opaque literals, VARs renamed `$v1...`, strip outer
  parentheses / `VAR x = E RETURN x`; a top-level filterless `CALCULATE` only stripped for candidate
  discovery (it performs context transition), never for "identical".
- Groups: `identical` (sha of canon), `alias` (body is one `[Other]`), `same_shape` (literals and
  columns as slots; calculation-group candidate).
- Fuzzy pairs blocked by token 3-grams (ignore grams in > 20% of measures, need 2 shared):
  `expr = 0.5 SequenceMatcher + 0.25 Jaccard(3-grams) + 0.25 Jaccard(refs)`,
  `score = 0.70 expr + 0.12 name + 0.06 format + 0.06 folder + 0.06 table`;
  near-duplicate at score >= 0.90 and expr >= 0.92, similar at >= 0.80 / 0.80; skip canons under
  5 tokens and pairs where one depends on the other (unless expr >= 0.95). Standard library only.
- Later: Measure Killer-style object similarity across models / reports (weighted attributes:
  normalized Levenshtein, set Jaccard, boolean penalties).

### 3.7 `pbz changes` (phase 2)
- `--from base|pull:<stamp> --to mirror|pull:<stamp>`; reads sanitized snapshots only.
- TMDL index keyed by lineageTag, else `kind:parent/name`; relationships by endpoints; partitions by
  table + ordinal; ignore lineageTag, `PBI_*` annotations, `changedProperty`.
- PBIR index: report, page order, pages, visuals (type, fields per role, filters, sort, position,
  format hash, visibility, interactions), filters by name, bookmarks by file.
- Statuses added / removed / renamed / moved / modified (ALM Toolkit adds missing-in-source /
  target semantics); expression change class format_only / comments_only / literal_only / logic;
  refs added / removed from deps; impact from the graph. Literal changes are counted, never shown.
- Replaces `mirror._object_summary` in the push log; feeds `pbz handoff`.

### 3.8 `pbz docs` (phase 2)
- Mirror only, writes `pbz-workspace/docs/`; HTML is one escaped file with `<details>`, no JS.
- Sections: overview, tables (columns with type / hidden / calculated / sortBy / format / used-by
  count, hierarchies), measures (folder, format, description, depends_on, used_by; DAX with a flag),
  calc groups / UDFs / field parameters, relationships (optional Mermaid erDiagram), Power Query
  (connector names and steps, no literal arguments), security (roles and filtered tables, members
  omitted), report (pages, visuals with fields per role, filters as field + type, bookmark targets),
  unused objects, BPA findings, change log.

### 3.9 `pbz m-lineage` (phase 3)
- `pbz/mworker/lineage.mjs` on `@microsoft/powerquery-parser@4.0.1`, run with
  `node --permission --allow-fs-read=<mworker>` (no writes, no network); JSON on stdin / stdout.
- Python masks bare tokens with same-length runs before sending; worker parses, walks let steps with
  a scope stack, applies a per-function transfer table (SelectColumns, RemoveColumns, RenameColumns,
  AddColumn, Expand*, Group, NestedJoin, Split / Combine, read-only transforms), marks dynamic steps,
  and maps the output columns to TMDL `sourceColumn`.
- Output emits strings only from column-name argument slots; UTF-16 offsets converted in Python.

### 3.10 Engine `deps` (phase 3)
- Contract change: allow the exact DMV `DISCOVER_CALC_DEPENDENCY`, select only object type / table /
  object columns, scrub names against TMSCHEMA names, reduce data sources to their type, keep
  `INFO.*` blocked for agent-written DAX. Used as `pbz unused --verify-engine` (diff of edges), not as
  the primary source (it reflects the loaded model, not mirror edits, and has no report usage).

## 4. Phases

| Phase | Scope | Effort | Exit criteria |
|---|---|---|---|
| 0 | deps.py, usage.py, parser and PBIR walker fixes, mirror history, guard deny `*.measurekiller` | M | all existing tests green; new dependency tests (scope, qualification, tokens, KPI, field parameters, auto date) |
| 1 | `unused`, `lineage`, `bpa model/report`, scores, `suggest` | L | fixture expectations met; real-model dry run shows counts by state and grade only |
| 2 | `unused --remove`, `similar`, `changes`, `docs` | M-L | remove/push/restore round trip on the fixture; changes detects every planted edit |
| 3 | `m-lineage`, M column pruning, engine `deps`, savings buckets | M | worker sandboxed; pruning verified in Desktop on the fixture by the user |

## 5. Verification

- Fixture extension (synthetic, in `tests/fixture`): an unused measure chain, a measure used only by
  an unused one, columns locked by sort-by / relationship / RLS / hierarchy, a field parameter, a calc
  group with SELECTEDMEASURE, an inactive relationship with and without USERELATIONSHIP, a KPI, a
  dynamic format string, report filters at three levels, a bookmark-only field, a mobile layout, a
  report-level measure, a hidden tooltip page, a second thin report, an auto-date column used through
  its hierarchy, and an M query that is loaded vs unloaded.
- Golden JSON outputs per verb; the canary scan from `test_e2e.py` extended to every new verb.
- Rule tests per BPA rule (positive and negative), including the substring pitfalls.
- Real models: dry runs only, reporting counts by state, findings by rule id and grade; the user
  reviews removal plans before any `--remove` + `push`.

## 6. Approvals needed later

- npm `@microsoft/powerquery-parser@4.0.1` (MIT, Node 22+, two small MIT dependencies) for phase 3.
- Optional `@microsoft/powerquery-language-services@1.3.0` (MIT) only for M rename.
- Engine contract change for `DISCOVER_CALC_DEPENDENCY` (phase 3).
- Nothing from PyPI: similarity, docs and BPA use the standard library.

## 7. Licensing and attribution

Port from MIT sources with a `THIRD_PARTY_NOTICES.md` (copyright line + MIT text per project) and a
`source` field on every ported rule: microsoft/Analysis-Services (BPA rules, ALM Toolkit),
microsoft/semantic-link-labs, TabularEditor/TabularEditor (algorithms), NatVanG/fab-inspector,
akhilannan/pbir-utils, JonathanJihwanKim/pbi-lineage-lenz / pbip-lineage-explorer /
pbip-documenter, ViciusLio/pbi-semantic-doc, sql-bi/Vpax-Obfuscator and VertiPaq-Analyzer,
microsoft/powerquery-parser. Measure Killer: concepts and published formulas only; its grade labels
are its branding (and "Power BI Pro" is a Microsoft SKU), so labels are configurable.
