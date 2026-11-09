# pbz parity plan: Measure Killer features, metadata only

> **Design history.** This file records how analysis and authoring features were planned and
> shipped. For current user docs see [INDEX.md](INDEX.md), the [CLI catalog](cli-catalog.md), and
> `skills/powerbi-zerodata/reference/`. No Measure Killer code or assets are vendored; see
> [THIRD_PARTY_NOTICES.md](../skills/powerbi-zerodata/THIRD_PARTY_NOTICES.md).

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

  **Current behavior:** Round 3 mirrors cultures, and rename, move and remove keep translations
  when they are mirrored. Legacy mirrors without cultures still refuse rename and move with "run pbz pull".

- BPA catalog: 53 model rules (42 on by default, 6 opinion, 5 data-derived and never run) and 21
  report rules (20 on, 1 opinion). Every rule citing `BPARules.json` uses the upstream rule id
  (checked against the file on 2026-11-04); other rules cite semantic-link-labs or are pbz's own.

Verified on an internal development mirror (counts and object names only, no values retained in
this document): `unused`, `lineage`, `suggest`, `bpa`, `similar`, `docs`, `m-lineage` and
`unused --remove --dry-run` ran without errors and changed no file; the graph had no unresolved
references or unknown fields; an independent text cross-check found zero objects called unused that
a qualified DAX reference or an entity-matched report field still uses. A heuristic-mode canary scan
(no engine index) found two tokenizer gaps, now fixed: RLS filters had no home table for bare
`[Column]` references, and string cells of embedded tables under headers the model does not know
were kept visible (they are now tokenized when they look specific).

## Round 2 (2026-11-04): gap research and authoring verbs

Three research passes (a Perplexity deep research survey of Tabular Editor 2/3, Measure Killer, Bravo,
DAX Studio, ALM Toolkit, fab-inspector, pbir-utils, semantic-link-labs, Microsoft's modeling MCP and
report CLIs, Fabric Git tooling and community MCP projects; plus independent design reviews)
agreed that the biggest gaps were write-side: safe
refactoring, deterministic report construction, scaffolds, relationship edits, offline cross-file
validation and local DAX formatting. Built:

- `rename table`, `move measure` (reference-aware across TMDL, M and PBIR).
- `new measure-table | date-table | calc-group | field-parameter`, `relationship add|remove|set`.
- `add page | visual` (templates for cards, tables, matrix, slicer, text box and native charts).
- `layout` (text canvas map plus layout lint), `format` (token-preserving DAX formatter).
- Built-in PBIR integrity checks in `validate` and every push, `repair` (structural fixes, optional
  projection and lineageTag repair), `bpa --fix` (property-only fixes).
- `status` sync state; `push --reload` refuses while Desktop has unsaved changes.
- Runtime self-containment: pure-Python three-way merge (no git), PowerShell fallback, optional npm
  CLIs, and a test that no third-party binary is invoked.

Deferred (documented, not built): culture / perspective / role editors and incremental-refresh setup;
`changes`-based selective plan / apply; an MCP transport; a synthetic-model test host. Design risks the
reviews raised, kept as roadmap items: a schema-driven allowlist tokenizer instead of the current
classifier, a disclosure ledger shared across sessions and query shapes for engine checks, and
OS-level isolation in addition to the hook.

## Round 3 (2026-11-04): the deferred items

- Cultures are mirrored (linguistic schema as one payload token); `culture`, `translate`; rename /
  move / remove keep translations in step (removes the earlier "manual: has translations" limit).
- `perspective` and `role` editors; measure and column renames now also rename perspective
  membership (a gap TOM caught in testing), and moving a measure moves it.
- `refresh-policy set|remove|show` for incremental refresh.
- `push --only` (selective, validated together with the real files) and `revert` (files or one object).
- `synth`: synthetic model built from the mirror with fake rows; engine verb `synth-query` (contract
  v1.3) answers only for a model carrying the HMAC marker of its own lineage tags; push refuses the
  marker in a real model.
- `mcp`: stdio MCP server exposing the verbs as typed tools; `install.py --mcp` registers it.
- Safety: `"literals": "strict"` policy; disclosure ledger (30-day constants per target including
  literals pushed into reached measures; daily budgets for checks, probes, snapshot queries, smoke);
  heuristic name detection no longer shielded by a capitalized common word next to a name.
- OS-level isolation is documented as a hardening tier for the machine owner
  (`reference/leak-vectors.md`); pbz does not change security settings itself.

## Round 4 (2026-11-04): external consumers and custom visuals

Design from multi-model design reviews plus
Microsoft's own authoring CLI rules.
- `consumers`: registered external consumers feed the usage graph. Kinds: thin report folders and
  Fabric Git exports (PBIR and PBIR-Legacy), PBIX / PBIT (definition or legacy layout, never
  DataModel), Excel (OLAP pivots by placed fields and hierarchy usage, slicers, timelines, CUBE
  formulas with member keys cut, DAX query tables; never pivot records, shared items or values), RDL /
  RSD, downstream composite models, query-text exports, an admin scanner result (coverage gaps), manual
  declarations. Only names of this model's objects are cached or shown; unresolved names are counted.
- New state `used_external`; evidence only keeps, missing coverage (missing, unreadable, opaque
  consumers, scanned consumers without definitions) makes the analysis incomplete and blocks
  `--remove`. Registry protected by the guard: agents add, only the user removes. `rename` warns
  (`USED_EXTERNALLY`) when a registered consumer binds to the old name.
- `add visual` now covers every built-in type in Microsoft's core visual capabilities (data roles,
  kinds and mapping conditions compacted into `rules/visuals.json`, MIT) and custom visuals: `visual
  import` (.pbiviz as an untrusted zip: contract, AppSource registration, or a private package that
  `push` installs into `CustomVisuals/`), `visual register` (AppSource / organizational `_OrgStore` by
  guid, optional manual roles), observed roles from existing instances. New checks: unregistered
  visual types, missing private packages, `_OrgStore` naming, kind conflicts, unknown roles.
- Still out of reach without service rights: calling the service APIs (scanner, getDefinition, Log
  Analytics) from pbz. The user exports and registers the files instead (`reference/consumers.md`).

## Round 5 (2026-11-04): visual formatting

- `style`: set and unset formatting on one visual, every visual of a type (page or report) or every
  visual on a page; a series (metadata selector) or an instance state; `--show`, `--list`. Values are
  typed by Microsoft's formatting catalog (`rules/visual-objects.json`: objects and properties of every
  built-in visual and of the container, value types, enumeration members, states): flags, numbers,
  integers, display units, enumerations, text, alignment, colors (hex, theme shades, field values),
  dates. Complex values (images, rules, filters) and data-point selectors are refused.
- New checks: `PBIR_OBJECT_UNKNOWN`, `PBIR_PROPERTY_UNKNOWN`, `PBIR_PROPERTY_VALUE` (warnings). On a
  large Desktop-authored report every object, property and literal value matched the catalog.

## Round 6 (2026-11-05): the rest of the authoring list

- `consumers discover <folder> [--register]`: finds Excel, PBIX, RDL, thin report and composite model
  consumers of this model from connection parts only.
- `filter add|remove|list`: report, page and visual filters (Categorical In / Not In, Advanced with
  AND, Top N with an ordered subquery, measure filters at visual level, field-only); values are labels,
  typed values or mirror tokens. `interaction`: DataFilter, HighlightFilter, NoFilter, or default.
- `add page --tooltip` and `--drillthrough 'T'[C]` (page binding, bound parameters and filters, a Back
  button), `link` for button actions (page, back, bookmark, drillthrough, web link) and report-page
  tooltips; validate flags dangling link targets. `bookmark add|remove|list`: show/hide bookmarks that
  never capture data state (`suppressData`). Every shape was checked against Microsoft's report
  validator with its JSON schemas on a Desktop-made report before use.
- `synth test save|run|list|remove`: golden results on the synthetic copy, SAME / DIFF per query with
  drift reported separately (built by a parallel agent; offline and live tests pass).
- CI: GitHub Actions runs the offline suites and the engine's offline tests on Windows for every push.

## Round 7 (2026-11-05): gap list

An external design review ranked the most valuable missing features
for this setting against Tabular Editor, DAX Studio, ALM Toolkit, Bravo, pbi-tools, PBI and Fab
Inspector, pbir-utils, semantic-link-labs, Fabric CLI and Microsoft's modeling and report tools.
Built from that list (three pieces by parallel agents):
- Revision-bound review packet and evidence (`review`, `.pbz/evidence.jsonl`): release classes,
  impact, stages PARSED / BOUND / LOADED / EXECUTED / SYNTHETIC / HANDOFF / HUMAN_ACCEPTED, STALE
  evidence, human-only acceptance.
- Presentation properties: `set` / `get` (bulk), `hierarchy`, `format-expr` (dynamic format strings).
- Declarative recipes with transactional rollback (`recipe run|check`).
- `copy page|visual` with id remapping; `a11y` audit and tab-order fix; `mobile` phone layouts.
- DAX user-defined functions (`function`), object-level security (`role ols`); compatibility levels
  checked (1601 for dynamic formats, 1702 for functions, confirmed against TOM) with an explicit
  `--upgrade-compat`.
- Push refuses model changes while Desktop holds unapplied Power Query edits.
- Synthetic edge-case fixtures and checks with visible fake counterexamples (`synth build --edge`,
  `synth check`).
Deferred: object-level semantic merge, verdict-only performance gates, a role-aware RLS test harness
(needs engine work and capability tests per Desktop build), Power Query authoring with refresh
contracts. Not building (leak or duplicate): real-data DAX debugging, raw timings or storage
statistics, screenshot redaction, arbitrary script plugins, an unrestricted modeling MCP passthrough,
service deployment, another PBIX extractor.

## Round 8 (2026-11-05): audit, efficiency, consolidation

A second design review compared VertiPaq Analyzer / VPAX, Bravo, DAX Studio, Tabular Editor and
community rule sets, semantic-link-labs, Measure Killer, DAX Optimizer and Microsoft's guidance, and
ranked audit features by value over effort here. Built:
- `audit`: findings with stable fingerprints, category, severity, confidence, evidence, fix, verify and
  blockers. Efficiency: per-row measure calls and nested iterators (through called measures), SUMMARIZE
  extension columns, calculated columns that are only aggregated (measure candidates) or row-local
  (Power Query candidates), unused high-cardinality columns, bidirectional high-cardinality keys, Import
  dimensions on DirectQuery facts, Table.Buffer before filters, storage hot spots. Consolidation:
  duplicate measures, calculation-group, filtered and parameterized families, repeated sub-expressions
  (with a context check: helper measure vs VAR / function), duplicate tables, several date tables,
  RELATED / plain copy columns, repeated Power Query steps. `--coverage` names the evidence each rule
  needs and whether it ran.
- `audit memory` (engine verb `memory`): VertiPaq storage attribution inside the broker, released as
  fixed size and share bands plus three fixed predicates (time part, fractions, long text).
- Baselines, waivers with expiry and a CI gate (`--since`, `--fail-on new|any|high`, exit code 1).
- `consolidate merge` (duplicate measures, alias kept for external consumers) and `consolidate
  calc-group` (wrapper measures keep report bindings; writes a check spec for synth and real checks).
- Fix: a dynamic format string replaces the static one; `FORMAT_STRING_CONFLICT` lint (Desktop refuses
  a measure with both although TOM accepts it) and `repair` fix. Desktop opened levels 1601 and 1702.
Deferred: performance workloads from Performance Analyzer exports (needs trusted replay and latency
bands), Power Query refresh-cost evidence, Direct Lake guardrails (needs a service adapter), automated
moves of calculated columns to Power Query. Not building: a VPAX export, a black-box cost score
presented as speed, fix-all for BPA, similarity-based table merges.

## Round 9 (2026-11-05): themes, page backgrounds and images

Question: could agents change themes and images? Themes already worked (theme JSON is mirrored and
Microsoft's validator checks it on push) but SKILL.md had no rules; images could not be placed at all
(image files are not mirrored and the guard blocks them); an image embedded in a theme reached the
mirror unhidden. Built, from Microsoft's core visual capabilities, the report-authoring validator and
Microsoft's report-authoring guidance:
- `«payload:image#n»`: `data:` URIs in report JSON and themes, and base64 images inside longer strings
  (text boxes, Vega specs, SVG measures), are tokenized on pull and restored byte for byte on push.
- `image add|list|remove`: header-checked files (PNG, JPEG, GIF, BMP, SVG without scripts or DTDs),
  staged in the private store, registered in report.json, installed (and dropped ones deleted, backed
  up) by push; checks for flat, unregistered, missing and unsafe image references.
- `style --canvas`: canvas and wallpaper color, transparency, image and scaling; image-typed and
  imageUrl-typed properties of visuals take a registered image (or a public URL); `add visual image`.
- Fixes: push removes the folder a deleted visual leaves; tokens escaped by a JSON writer
  (`«...»`) are still read as tokens and flagged if left over.
- SKILL.md rules for themes, page colors and images.
Not building: image previews or descriptions for the agent (images can show data), theme generation
from a brand image, plot-area images (Microsoft's validator rejects the nested form there).

## Round 10 (2026-11-06): hardening

- `commit_bytes` reports `FAILED` (exit 1) after a complete rollback of the files that transaction
  touched. `RECOVERY_REQUIRED` stays blocked when that restore itself fails. This is still a
  caught-OSError rollback, not crash-proof multi-process atomicity.
- `unused --remove` and `--restore` plan each cascade round, then commit it once. A later round that
  cannot be analyzed, or whose write rolls back, is `PARTIAL`: earlier rounds stay applied and the
  restore point returns pre-command bytes. It does not say nothing was removed.
- Culture, perspective and role add/remove, and the multi-file culture and perspective writers,
  commit their files together. A sanitized file name that another object already owns is refused.
- `smoke` parses report, page and visual PBIR before spending the daily budget. An unreadable file
  is `INVALID` and an unknown page is `NOT_FOUND`, both before any spend. A present but malformed
  filter is `PARTIAL`. An impossible calendar or clock value is not `FULL`.
- Filter and relationship edits refuse a non-positive Top N, ranking modifiers without Top N,
  non-finite numbers, impossible datetimes, and opposing `--active`/`--inactive` or
  `--both`/`--single` before any write. Refusals do not echo the rejected value.

Not built this round: staging a whole cascade on a disposable mirror copy, moving every writer onto
`commit_bytes`, a CI glob, native interactions, Power Query authoring, semantic merge, an RLS harness.

## Round 11 (2026-11-07): more atomic writers

- Property sets, `repair` (including a present malformed page, visual, bookmark or `pages.json`,
  which is refused on apply and on dry-run), bookmark add/remove, tab order, phone layout,
  visual and canvas style, `add page` / `add visual` (with a planned custom-visual registration
  and a planned drillthrough Back button) and `copy page` / `copy visual` each validate first,
  then call `commit_bytes` once. A clean rollback is `FAILED`. Empty directories may remain;
  no orphan files and no `.pbz-tmp` remain.
- Mutation discovery for tab order, phone layout and style parses candidate documents before a
  helper can skip or rename a bad one. Style strictness is opt-in, so bookmarks, filters and
  button links keep the previous discovery. A refusal names the mirror-relative path only.
- New offline suites: `repair_props_atomic`, `bookmarks_atomic`, `a11y_atomic`, `style_atomic`,
  `author_atomic`, `components_atomic`.

Not built this round: relationship, filter and interaction `--dry-run`, native interactions,
Power Query authoring, semantic merge, an RLS harness, VPAX, screenshots, empty-directory cleanup.

## Round 12 (2026-11-07): project-wide hardening

- A present consumer registry that cannot be read or parsed is a closed `REFUSED` before any
  evaluation or write. A missing registry is still "none". A bad entry blocks `unused --remove`
  as incomplete coverage. `consumers discover` points at `--register`.
- `image add` and `image remove` parse the report strictly, then commit the registration edit,
  staged bytes and orphan pending deletes together (`write_bytes` on `commit_bytes`). A private
  restore that fails is `RECOVERY_REQUIRED` with a fixed message: no absolute path, pending-store
  location, image bytes or exception text.
- Filter, relationship and interaction edits plan first, expose `--dry-run` (and the MCP twin),
  refuse a malformed sibling document, and apply with one `commit_bytes`. Relationship results
  name `files`.
- A date table and every `--relate` are validated on an in-memory model that already contains the
  new table and each previously planned relationship, then written as one commit (one relationships
  document). A missing `model.tmdl` is refused instead of an unreferenced table.
- Dynamic format strings and function add/rename commit the measure or call sites together with
  `database.tmdl` when the compatibility level changes. An unknown reference is refused before that
  write. `files` uses the real `database.tmdl` path.
- `recipe run --dry-run` substitutes and parses every step before any step runs, refuses `image`
  and `unused --remove` / `--verify-engine` (including a substituted flag) with zero engine calls,
  and hides parser text. A failed step or a later validation error restores through `commit_bytes`;
  a restore that fails is `RECOVERY_REQUIRED`. Evidence is the final recipe outcome only.
- `refresh-policy set` and `format --apply` use one `commit_bytes` as well.

Not built this round: native interactions, Power Query authoring, semantic merge, an RLS harness,
VPAX, screenshots, Desktop UI automation, Performance Analyzer replay, Direct Lake, a mirror
pull/push rewrite, empty-directory cleanup.

## Round 13 (2026-11-07): remaining transactions and honesty

- `write_text` and `write_bytes` remove their `.pbz-tmp` if the write or replace fails. `commit_bytes`
  is unchanged. Empty directories may remain.
- `consumers add`, `remove` and `discover --register` plan the evaluation in memory, then write the
  registry and the cache (or delete a cache) in one private `commit_bytes`. A cache write on a read
  path stays best-effort. A signature that cannot be stated is `missing`. A present bad registry is
  still `REFUSED`.
- `synth build` writes the candidate in a sibling `<id>.pbz-stage`, validates that folder, and
  publishes with one private commit only when validation succeeds. A rejected validator does not
  replace the previous destination. An unavailable validator does not publish either: the previous
  good tree stays, and a first build leaves no destination. Cleanup deletes only a stage this call
  created, or a destination whose policy, `synth.json` and HMAC marker all verify. Missing or
  malformed markers never authorize a delete. Status, open and query refuse an incomplete root.
- `handoff` refuses a present malformed or unreadable cursor, and a malformed push log, before any
  write. The packet, archive, alias and cursor are one commit. Acceptance uses `commit_bytes`.
  The checklist asks for qualitative feedback only.
- `preview` plans every page, then commits the SVG and JSON once. `--spec` is an inline JSON object
  or a path inside the mirror. An outside path is refused and not read. MCP still passes an object.
- `memscrub --apply` is `HUMAN_ONLY` before any read or write, then one private commit of the notes
  and a collision-safe backup. `doctor` does not mark disposable isolation `configured` from
  `USERNAME=WDAGUtilityAccount` alone; that step stays `unverified`. MCP still omits `--apply`.
- `synth test run` is never `SAME` when either side is truncated, when the saved query list is
  empty, or when the saved envelope is malformed. A malformed envelope makes no query and no engine
  call. An invalid build timestamp or a stale synthetic copy is `NOT_COMPARABLE`. A private
  `commit_bytes` restore that fails is `RECOVERY_REQUIRED` on the CLI and on MCP, with no path,
  bytes or exception text.

Not built this round: native interactions, Power Query authoring, semantic merge, an RLS harness,
VPAX, screenshots, Desktop UI automation, Performance Analyzer replay, Direct Lake, a mirror
pull/push rewrite, empty-directory cleanup, control/memory/docs migrations, disclosure expansion.

## Round 14 (2026-11-07): mirror honesty and remaining writers

- Outcome mapping: `NOT_COMPARABLE` and `LOCAL_STATE_UNAVAILABLE` are blocked (exit 3).
  `ERROR` / `ERROR:CLASS` and `INTERNAL_ERROR` are failed (exit 1). Incomplete smoke is
  inconclusive, not success. `--diagnostic` stays exit 0. `recover` of an unknown id is
  `NOT_FOUND`. `check --validate-only` appends no evidence. Synth evidence is scoped by
  golden name. A repeated synthetic error is never `SAME`.
- `revert` and `bpa --fix` read targets first (an unreadable file is `REFUSED`) and then
  apply one `commit_bytes`. Dry-run uses that same plan and writes nothing.
- Pull prepares every selected file, and for rebase every mirror and base text the merge
  will consume, before any write. A decode failure names the mirror-relative path only.
  Pending recovery or a commit lock blocks ordinary, forced and rebased pulls. A journal
  with an unknown state or a bad entry shape is recovery-required and is not echoed.
- Push captures candidate bytes before validation and returns `CONFLICT` if those files
  change before apply, without taking the new bytes as the baseline. Hashes written into
  the manifest come from the captured candidate. A reload failure after commit stays
  `PUSHED` with `PARTIAL` (exit 5).
- History snapshots use `write_text`. An unreadable snapshot is `UNREADABLE` and blocks
  blob garbage collection. A history failure after a successful pull is a
  `POST_PULL_HISTORY` warning; the pull result stays `PULLED`.
- `synth test save` refuses a stale or invalid build before any query, then writes with
  one `commit_bytes`. Remove deletes through the same helper. An unreadable golden is
  `REFUSED` (missing is `NOT_FOUND`). `run` with `comparable` false returns
  `NOT_COMPARABLE` and does not query.
- Strict memory disclosure strips `size_band` and `share_band` on the live result and when
  a shape-mode cache is loaded under the current strict policy. Predicates stay. An
  unexpected engine error class is a closed `ENGINE_UNAVAILABLE` with no sentinel.
  The docs change log parses each line, keeps the valid records, reports `INCOMPLETE`
  and a skipped count, and never echoes a rejected line.
- New offline suites are registered. `test_engine.py --offline` counts as registered
  offline work. `test_e2e.py` stays Desktop-only.

Not built this round: a full mirror pull/push rewrite, native interactions, Power Query
authoring, semantic merge, an RLS harness, VPAX, screenshots, Desktop UI automation,
Performance Analyzer replay, Direct Lake, empty-directory cleanup, control/docs writer
migrations as product, disclosure expansion.

## 0. Ground rules

- Every feature runs on metadata: the sanitized `pbz-workspace` mirror for anything the agent sees,
  the staged real files inside `pbz` only when a rule must look at literal text. Output is object
  names, ids, states, rule ids, object counts and fixed buckets. Never values.
- Measure Killer features that need data are excluded or reduced to policy-gated buckets: VertiPaq
  memory sizes, views and load times, Excel activity, DAX query execution.
- Tenant / service features (workspace inventory, scanner API, capacity metrics) are out of scope
  until an account with workspace rights exists; they would be metadata APIs behind the engine.
- Unused means "unused in the scanned reports and registered consumers". Every unused result
  carries `scope.unseen_consumers` and `scope.external_coverage`. An incomplete analysis is `unknown`,
  never "unused", and blocks automatic removal.

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

## Round 15 parity

CLI and MCP share the same refusal path: `UNSAFE_PATH`, `REFUSED`, `HUMAN_ONLY`,
`ENGINE_UNAVAILABLE`, `RECOVERY_REQUIRED` and `FAILED` keep their existing exit codes. MCP runs
the CLI, so a non-human `memscrub --show` is `HUMAN_ONLY` with no scrubbed text. Policy writes,
push journals and synthetic publish still prepare before they replace. A failed replace leaves
the previous bytes. This is not a claim that a crash mid-replace is atomic.

## Round 16 parity

`.pbz` state paths are anchored at the workspace root. Waiver, baseline, handoff state,
memory and the push log use the same checked writers and single-link reads. A mirror
junction is refused before pull, docs, handoff, control or guide writes treat that
directory as a trust root. `--spec` and the sibling recipe, synth-test, freshness and
interaction loaders refuse a link, a hard link, `.pbz`, a model or report path, or a
protected file before the first content byte; malformed JSON stays `BAD_REQUEST`.
Consent checks the opened handle before it reads. Requirement proofs store the mirror
revision and the saved real revision together, and a check result is kept only when it
matches the registered ids. A late push-log append after a committed push is still
`POST_COMMIT_HISTORY`. None of this is crash-proof, and it is not OS isolation.

## Round 17 parity

Evidence and synth-test state are anchored at the workspace root. The evidence log is capped
at 1 MiB; nothing rotates it. Over-cap recovery is to archive or remove `.pbz/evidence.jsonl`
by hand, which also discards `HUMAN_ACCEPTED` history with the file. A present policy that
cannot be trusted fails closed (broker `REFUSED` / `UNSAFE_PATH`, guard lockdown) instead of
falling back to shape disclosure. `diff --patch` and `push` refuse an aliased or hardlinked
mirror file; other mirror readers still do not check link counts. Recipe snapshot and restore
use the mirror as the trust root: an aliased directory or hardlinked leaf is refused before any
step, and a link planted while a step runs is `RECOVERY_REQUIRED` without being followed. Freshness class `OTHER` is
`ENGINE_UNAVAILABLE`. Interactions static honesty stays deferred to Round 18. Rollback is still
exception-only. Documented residuals: shell content writes; script-built policy paths;
script-level policy deletion; recursive directory copy/move that implicitly carries `.pbz`;
recursive deletion of a workspace root or ancestor; script-scanning false positives; ancestor
inspection failures that force lockdown; other mirror readers that do not check link counts;
guard-log hardening (deferred). Unusable policy recovery: fix or remove `.pbz/policy.json` by
hand in your own terminal. This is not OS isolation.

## Round 18 parity

`pbz interactions` is static-only. Results use a frozen token set (`MATCH`, `MISMATCH`,
`DEFAULT_UNRESOLVED`, `MISSING_ID`, `RUNTIME_ONLY`, `LEGACY`, `UNKNOWN_TYPE`, `UNSUPPORTED`,
`REFUSED`). Evidence stage is `STATIC_CONTRACT` and cannot stand in for `HUMAN_ACCEPTED` or native
execution. Page and visual JSON under the mirror is read through `open_mirror_file_readonly`.
The guard denies protected content writes, recursive copies that name `.pbz`, and recursive deletes
of the workspace root. Model TMDL reads through `tmdl.load_model` stay outside that reader gate.
Evidence rotation and `admin memory-dir` stay open. This is not OS isolation.

## Round 19 parity

`pbz interaction --show` reports stored cross-filter metadata for a page: one closed mode per
source and target, with page and visual ids only. Setting one pair leaves the other stored pairs
in place. `pbz review --interactions-spec` is how a static interaction digest becomes current.
