# Documentation index

Start here if you are new to **pbi-zerodata** (`pbz`): a skill + CLI that lets coding agents develop
Power BI PBIP projects without company data values entering the agent context.

## Start here

| Doc | Audience | Contents |
|---|---|---|
| [../README.md](../README.md) | Everyone | What it is, requirements, install, quick tour |
| [overview.md](overview.md) | Everyone | Zero-data model, mirror, guard, engine, policy |
| [getting-started.md](getting-started.md) | Practitioners | First session: init → edit → validate → push → handoff |
| [install.md](install.md) | Practitioners | Junctions so Claude, Codex, ChatGPT, and Cursor follow `git pull` |
| [cli-catalog.md](cli-catalog.md) | Practitioners | Every `pbz` verb and nested action |

## Legal and project process

| Doc | Contents |
|---|---|
| [../LICENSE](../LICENSE) | MIT |
| [../SECURITY.md](../SECURITY.md) | Vulnerability reporting and boundary expectations |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Dev setup, tests, PR rules |
| [../CHANGELOG.md](../CHANGELOG.md) | User-facing history |

## Agent contract and deep topics

These live with the skill so agents and humans share one source of truth:

| Path | Contents |
|---|---|
| [../skills/powerbi-zerodata/SKILL.md](../skills/powerbi-zerodata/SKILL.md) | Hard rules agents must follow |
| [../skills/powerbi-zerodata/reference/workspace.md](../skills/powerbi-zerodata/reference/workspace.md) | Status, init, scan, describe, admin, handoff |
| [../skills/powerbi-zerodata/reference/tokens.md](../skills/powerbi-zerodata/reference/tokens.md) | Mirror token grammar and classification |
| [../skills/powerbi-zerodata/reference/checks.md](../skills/powerbi-zerodata/reference/checks.md) | Data-blind Desktop checks, snapshots, smoke |
| [../skills/powerbi-zerodata/reference/analysis.md](../skills/powerbi-zerodata/reference/analysis.md) | Unused, lineage, suggest, similar, changes, docs, m-lineage |
| [../skills/powerbi-zerodata/reference/bpa.md](../skills/powerbi-zerodata/reference/bpa.md) | Best-practice analysis |
| [../skills/powerbi-zerodata/reference/audit.md](../skills/powerbi-zerodata/reference/audit.md) | Efficiency / consolidation audit |
| [../skills/powerbi-zerodata/reference/authoring.md](../skills/powerbi-zerodata/reference/authoring.md) | Refactors, pages, visuals, filters, bookmarks |
| [../skills/powerbi-zerodata/reference/style.md](../skills/powerbi-zerodata/reference/style.md) | Visual formatting |
| [../skills/powerbi-zerodata/reference/visuals.md](../skills/powerbi-zerodata/reference/visuals.md) | Built-in and custom visuals |
| [../skills/powerbi-zerodata/reference/governance.md](../skills/powerbi-zerodata/reference/governance.md) | Cultures, roles, perspectives, refresh policy |
| [../skills/powerbi-zerodata/reference/consumers.md](../skills/powerbi-zerodata/reference/consumers.md) | External consumer registry |
| [../skills/powerbi-zerodata/reference/synth.md](../skills/powerbi-zerodata/reference/synth.md) | Synthetic model and regression tests |
| [../skills/powerbi-zerodata/reference/workflow.md](../skills/powerbi-zerodata/reference/workflow.md) | Review packets, evidence, recipes |
| [../skills/powerbi-zerodata/reference/improvements.md](../skills/powerbi-zerodata/reference/improvements.md) | Consent, preview, freshness, requirements, recover, honesty notes |
| [../skills/powerbi-zerodata/reference/preview.md](../skills/powerbi-zerodata/reference/preview.md) | Synthetic layout preview |
| [../skills/powerbi-zerodata/reference/mcp.md](../skills/powerbi-zerodata/reference/mcp.md) | MCP stdio server |
| [../skills/powerbi-zerodata/reference/disclosure-contract.md](../skills/powerbi-zerodata/reference/disclosure-contract.md) | What may leave the broker |
| [../skills/powerbi-zerodata/reference/leak-vectors.md](../skills/powerbi-zerodata/reference/leak-vectors.md) | Leak surface and OS isolation checklist |
| [../skills/powerbi-zerodata/reference/gotchas.md](../skills/powerbi-zerodata/reference/gotchas.md) | TMDL / PBIR / Desktop pitfalls |
| [../skills/powerbi-zerodata/engine/README.md](../skills/powerbi-zerodata/engine/README.md) | `pbz-engine` build and run |
| [../skills/powerbi-zerodata/THIRD_PARTY_NOTICES.md](../skills/powerbi-zerodata/THIRD_PARTY_NOTICES.md) | Attributions |

## Design history (optional)

| Doc | Contents |
|---|---|
| [parity-plan.md](parity-plan.md) | Feature parity design history (Measure Killer–style analysis, metadata only) |
| [reference-projects.md](reference-projects.md) | Related open-source projects studied during design |
