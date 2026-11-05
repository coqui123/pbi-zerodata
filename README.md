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
python skills/powerbi-zerodata/tests/test_engine.py    # engine verbs + canary scan (needs Desktop)
python skills/powerbi-zerodata/tests/test_e2e.py       # every verb on a fixture copy in Desktop, canary scan
```

This repo is marked synthetic (`.pbz/policy.json`), so the guard treats its fixtures as fake data.
Develop the skill from a session started here, not from a real Power BI workspace.
