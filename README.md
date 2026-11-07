# pbi-zerodata

Skill + CLI bundle that lets coding agents (Claude Code, Codex) develop Power BI PBIP projects
without company data entering the agent's context.

- `skills/powerbi-zerodata/SKILL.md`: the workflow agents follow.
- `skills/powerbi-zerodata/pbz/`: the `pbz` CLI (Python, stdlib + jsonschema) and `guard.py`, the
  PreToolUse / SessionStart hook shared by Claude Code and Codex.
- `skills/powerbi-zerodata/engine/`: `pbz-engine`, the C# declassification broker (TOM + ADOMD.NET)
  that runs checks against Power BI Desktop's local engine and prints verdicts only.
- `skills/powerbi-zerodata/tools/`: Microsoft's `powerbi-report-author` and `powerbi-desktop`
  CLIs (npm, local install; `screenshot` / `preview` are blocked by the guard).
- Model hygiene verbs with Measure Killer parity, all metadata only: `unused` (with `--remove` /
  `--restore`), `lineage`, `suggest`, `bpa` (scores, grades, `--fix`), `similar`, `changes`, `docs`,
  `m-lineage`. Plan and status: `docs/parity-plan.md`; usage: `reference/analysis.md`,
  `reference/bpa.md`; attributions: `THIRD_PARTY_NOTICES.md`.
- Refactoring and authoring verbs (mirror edits, validated on push): `rename table`, `move measure`,
  `new` (measure table, date table, calculation group, field parameter), `relationship`,
  `add page|visual`, `layout`, `format`, `repair`. Usage: `reference/authoring.md`.
- `add visual` covers every built-in visual type (roles from Microsoft's core visual capabilities) and
  custom visuals: `visual import|register|roles|list` for AppSource, organizational and private
  .pbiviz visuals (`reference/visuals.md`).
- `style`: visual formatting (titles, data labels, colors, fonts, axes, legends, borders, one series,
  button states) for one visual or every visual of a type, checked against Microsoft's formatting
  catalog (`reference/style.md`).
- `filter` (basic, not in, advanced, Top N, measure filters at report, page and visual level),
  `interaction`, tooltip and drillthrough pages (`add page --tooltip | --drillthrough`), show/hide
  `bookmark`s and button actions (`link`): `reference/authoring.md`.
- `synth test`: golden results on the synthetic model and SAME / DIFF after a refactor
  (`reference/synth.md`); `consumers discover` finds files connected to the model.
- Model properties (`set` / `get`, bulk by table, kind, name pattern or folder), `hierarchy`,
  `format-expr` (dynamic format strings), DAX user-defined `function`s, object-level security
  (`role ols`); page and visual `copy`, `a11y` audit and tab-order fix, `mobile` phone layouts.
- `review`: review packet by release class with revision-bound evidence (`reference/workflow.md`);
  `recipe run`: JSON batches of mirror edits applied as one transaction with rollback.
- `audit`: efficiency and consolidation findings (iterators, calculated columns, storage bands, relationships,
  Power Query overlap, duplicate measures and tables, calculation-group, filtered and parameterized
  families, repeated sub-expressions), each with evidence, fix and verification; baselines, waivers and a
  CI gate. `audit memory` adds VertiPaq storage bands from Desktop (bands only); `consolidate merge` and
  `consolidate calc-group` apply the consolidations with a check spec (`reference/audit.md`).
- Themes, page backgrounds and images: custom theme JSON is edited in the mirror and checked on push;
  `style --canvas` sets canvas and wallpaper color, transparency and image; `image add|list|remove`
  stages image files without showing them and push installs them; `add visual image`. Images embedded
  in themes, text or SVG measures become `«payload:image#n»` tokens (`reference/authoring.md`).
- CI: `.github/workflows/tests.yml` runs the offline suites on every push (Desktop suites stay local).
- `consumers`: register thin reports, PBIX, Excel workbooks, paginated reports, composite models, query
  exports or a scanner result so `unused` keeps what they use (`used_external`) and refuses removals
  while coverage is incomplete (`reference/consumers.md`).
- Governance and refresh editors: `culture`, `translate`, `perspective`, `role`, `refresh-policy`;
  `push --only` and `revert`. Usage: `reference/governance.md`.
- `synth`: a synthetic copy of the model with fake data that agents may query freely
  (`reference/synth.md`); `mcp`: the same verbs as typed MCP tools (`reference/mcp.md`, register with
  `install.py --mcp`).
- Safety: optional `"literals": "strict"` policy (hide every literal by default) and a 30-day
  disclosure ledger for engine checks; OS-level hardening steps in `reference/leak-vectors.md`.
- Self-contained at runtime: no third-party binaries (no Measure Killer, Tabular Editor, DAX Studio or
  Bravo); built-in three-way merge and PBIR integrity checks; Microsoft's npm CLIs are optional.

## Install

```
cd skills/powerbi-zerodata/tools && npm install          # Microsoft PBIR + Desktop bridge CLIs
cd ../engine && dotnet publish -c Release -r win-x64 --self-contained false -o bin
python ../install/install.py --what-if                    # preview
python ../install/install.py                              # junctions, hooks, rule blocks
```

Then trust the hooks once in Codex (`/hooks`). Uninstall: `python install/install.py --uninstall`.

## Tests

```
python skills/powerbi-zerodata/tests/test_guard.py     # hook decision table (Claude + Codex shapes)
python skills/powerbi-zerodata/tests/test_mirror.py    # tokenizer, round trip, push, rename, conflicts
python skills/powerbi-zerodata/tests/test_parity.py    # resolver, usage states, unused/remove/restore, m-lineage
python skills/powerbi-zerodata/tests/test_bpa.py       # best-practice rules, scores, grades
python skills/powerbi-zerodata/tests/test_tools.py     # similar, changes, docs
python skills/powerbi-zerodata/tests/test_refactor.py  # rename table, move measure, layout, bpa --fix
python skills/powerbi-zerodata/tests/test_authoring.py # scaffolds, relationships, pages/visuals, repair
python skills/powerbi-zerodata/tests/test_selfcontained.py # built-in merge and PBIR checks, no external binaries
python skills/powerbi-zerodata/tests/test_governance.py # cultures, perspectives, roles, refresh policy, ledger, strict
python skills/powerbi-zerodata/tests/test_mcp.py        # MCP server over stdio
python skills/powerbi-zerodata/tests/test_synth.py      # synthetic model (add --live to open it in Desktop)
python skills/powerbi-zerodata/tests/test_consumers.py  # external consumers: every kind, coverage gaps, canaries
python skills/powerbi-zerodata/tests/test_visuals.py    # built-in catalog, custom visuals, private package push
python skills/powerbi-zerodata/tests/test_style.py      # visual formatting: values, selectors, bulk, checks
python skills/powerbi-zerodata/tests/test_reportedit.py # filters, interactions, tooltip / drillthrough pages, links
python skills/powerbi-zerodata/tests/test_synthtest.py  # synthetic regression tests (add --live to run in Desktop)
python skills/powerbi-zerodata/tests/test_props.py      # typed model properties, bulk selection
python skills/powerbi-zerodata/tests/test_modeling.py   # hierarchies, dynamic formats, functions, OLS (TOM)
python skills/powerbi-zerodata/tests/test_workflow.py   # review packet, evidence, recipes, Power Query guard
python skills/powerbi-zerodata/tests/test_components.py # page and visual copies
python skills/powerbi-zerodata/tests/test_a11y.py       # accessibility audit, tab order, phone layout
python skills/powerbi-zerodata/tests/test_synthedge.py  # edge-case synthetic data and checks (add --live for Desktop)
python skills/powerbi-zerodata/tests/test_audit.py      # audit rules, evidence, baselines, waivers, gate
python skills/powerbi-zerodata/tests/test_consolidate.py # merge duplicates, calculation-group families (add --live for Desktop)
python skills/powerbi-zerodata/tests/test_memory.py     # storage bands (add --live for Desktop)
python skills/powerbi-zerodata/tests/test_images.py     # images, page canvas, embedded image tokens, push install
python skills/powerbi-zerodata/tests/test_engine.py    # engine verbs + canary scan (needs Desktop)
python skills/powerbi-zerodata/tests/test_e2e.py       # every verb on a fixture copy in Desktop, canary scan
```

This repo is marked synthetic (`.pbz/policy.json`), so the guard treats its fixtures as fake data.
Develop the skill from a session started here, not from a real Power BI workspace.
