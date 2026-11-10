# pbi-zerodata

**Develop Power BI PBIP projects with coding agents — without company data values entering the
agent context.**

Agents see every **name** and every **formula**. They must never see rows, totals, distinct counts,
query results, cached data, or screenshots of populated visuals. The `pbz` CLI and guard keep work
inside a sanitized mirror (`pbz-workspace/`) and return closed-vocabulary verdicts from Desktop.

| | |
|---|---|
| **License** | [MIT](LICENSE) |
| **Docs** | [Documentation index](docs/INDEX.md) |
| **Security** | [SECURITY.md](SECURITY.md) |
| **Contributing** | [CONTRIBUTING.md](CONTRIBUTING.md) |
| **Changelog** | [CHANGELOG.md](CHANGELOG.md) |

## Why it exists

Normal agent setups can read `*.SemanticModel`, `*.Report`, workbooks, and Desktop outputs. That
leaks business data into prompts. `pbz` separates:

1. **Real PBIP files** — blocked from agent tools by the PreToolUse guard.
2. **Sanitized mirror** — reversible tokens; safe to edit and search.
3. **`pbz-engine`** — local Desktop broker that prints verdicts, never cell values.
4. **Human gates** — consent, review acceptance, admin, and memory scrub apply.

Honest limits (not OS isolation, not crash-proof atomicity) are documented in
[docs/overview.md](docs/overview.md) and
[skills/powerbi-zerodata/reference/leak-vectors.md](skills/powerbi-zerodata/reference/leak-vectors.md).

## Requirements

- Windows
- Python 3.11+
- .NET 10 SDK (to build `pbz-engine`)
- Power BI Desktop (for live engine checks)
- Node.js (optional; Microsoft PBIR / Desktop npm CLIs)
- Claude Code or Codex (or any host that can run the skill + hooks)

## Install

```powershell
cd skills/powerbi-zerodata/tools
npm install

cd ../engine
dotnet publish -c Release -r win-x64 --self-contained false -o bin

cd ..
python install/install.py --what-if
python install/install.py
```

`install.py` creates directory junctions from the Claude, Codex / ChatGPT, and shared agent
skill folders to `skills/powerbi-zerodata`. A later `git pull` updates those skills and the
guard script the hooks run. Trust the hooks once (Codex: `/hooks`). Full steps:
[docs/install.md](docs/install.md). Uninstall: `python install/install.py --uninstall`.

CLI entry: `skills/powerbi-zerodata/bin/pbz.cmd` (the same file the skill junctions expose as
`pbz`).

## 60-second tour

From the folder that holds your `.pbip`:

```powershell
pbz init
pbz status
pbz describe
pbz unused
# edit only under pbz-workspace\
pbz validate
pbz preflight
pbz push
pbz handoff
```

Full walkthrough: [docs/getting-started.md](docs/getting-started.md).  
Every verb: [docs/cli-catalog.md](docs/cli-catalog.md).

## What you get

| Area | Highlights | Docs |
|---|---|---|
| Zero-data workflow | Mirror, guard, policy fail-closed, disclosure contract | [overview](docs/overview.md), [SKILL.md](skills/powerbi-zerodata/SKILL.md) |
| Analysis | `unused`, `lineage`, `find`, `suggest`, `bpa`, `similar`, `changes`, `watch` | [analysis](skills/powerbi-zerodata/reference/analysis.md), [bpa](skills/powerbi-zerodata/reference/bpa.md) |
| Authoring | rename/replace/rewrite, calendars, KPIs, scripts, scaffolds, pages/visuals | [authoring](skills/powerbi-zerodata/reference/authoring.md), [visuals](skills/powerbi-zerodata/reference/visuals.md) |
| Governance | cultures and translation files, roles/OLS, perspectives, refresh policy | [governance](skills/powerbi-zerodata/reference/governance.md) |
| Verification | `check` including RLS and perf bands, snapshots, smoke, synth, Performance Analyzer import | [checks](skills/powerbi-zerodata/reference/checks.md), [external parity](skills/powerbi-zerodata/reference/external-parity.md) |
| Review | evidence stages, `review`, recipes | [workflow](skills/powerbi-zerodata/reference/workflow.md) |
| Audit | efficiency + consolidation findings, waivers, memory bands | [audit](skills/powerbi-zerodata/reference/audit.md) |
| Agents | same verbs as MCP tools | [mcp](skills/powerbi-zerodata/reference/mcp.md) |

Attributions for upstream rules and ideas:
[skills/powerbi-zerodata/THIRD_PARTY_NOTICES.md](skills/powerbi-zerodata/THIRD_PARTY_NOTICES.md).

## Repository layout

```
docs/                         Human onboarding + CLI catalog
skills/powerbi-zerodata/
  SKILL.md                    Agent hard rules
  pbz/                        Python CLI + guard hook
  engine/                     C# declassification broker (TOM + ADOMD.NET)
  reference/                  Deep topic documentation
  install/                    Junctions, hooks, MCP registration
  tests/                      Offline suites (CI) + canary fixture
  tools/                      Optional Microsoft npm CLIs
```

## Tests

Offline suites run on every push via [.github/workflows/tests.yml](.github/workflows/tests.yml).
From `skills/powerbi-zerodata`:

```powershell
python tests/test_guard.py
python tests/test_ci_registration.py
# Suite names are the list in .github/workflows/tests.yml.
# Then: python tests/test_engine.py --offline
```

Desktop / e2e suites stay local and must use synthetic or fixture data only.

## Design history

Earlier research and parity notes (optional reading):

- [docs/parity-plan.md](docs/parity-plan.md)
- [docs/reference-projects.md](docs/reference-projects.md)
