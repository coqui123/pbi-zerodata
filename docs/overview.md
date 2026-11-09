# Overview: zero-data Power BI development

## Problem

Coding agents are useful for DAX, TMDL, and PBIR edits, but a normal workspace lets them read
model folders, caches, workbooks, and Desktop query results. That pulls **company data values**
into the model context.

## Approach

`pbz` keeps a strict split:

| Layer | Role | Agent may see |
|---|---|---|
| Real PBIP (`*.SemanticModel`, `*.Report`) | Source of truth on disk | **No** (guard blocks) |
| `pbz-workspace/` sanitized mirror | Editable metadata with reversible tokens | **Yes** (names + formulas) |
| `.pbz/` private state | Policy, evidence, snapshots, ledgers | Metadata / closed verdicts only |
| `pbz-engine` | Talks to Power BI Desktop locally | Closed vocabulary (OK / EMPTY / ERROR classes), never cells |
| Human terminal | Consent, review accept, admin, memscrub apply | Full responsibility for approvals |

Agents see **every name and every formula**. They must never see rows, totals, distinct counts,
query grids, populated screenshots, or error text that echoes values.

## Typical loop

```
pbz status / init / pull     → mirror exists and is current
edit under pbz-workspace/    → names and DAX/M/PBIR only
pbz validate / preflight     → metadata and staged checks
pbz push                     → write real files after validation
pbz check / synth test / …   → closed verdicts
pbz handoff / review         → human verifies visuals and acceptance
```

## Safety properties (honest limits)

**pbz does:**

- Tokenize sensitive literals in the mirror and restore them on push.
- Block agent tool access to real definitions, data files, and denied paths.
- Fail closed when policy is present but unusable or aliased.
- Refuse aliased/hardlinked mirror paths on disclosure-sensitive verbs.
- Keep Desktop verification on a disclosure budget with closed outcomes.

**pbz does not claim:**

- Full OS isolation (use `pbz doctor` and [leak-vectors.md](../skills/powerbi-zerodata/reference/leak-vectors.md)).
- Crash-proof atomicity across killed processes (writers are best-effort with journals/recovery).
- That a human who runs unrestricted shells outside the guard is still zero-data.

## Repository layout

```
pbi-zerodata/
  README.md, LICENSE, SECURITY.md, CONTRIBUTING.md, CHANGELOG.md
  docs/                          # human onboarding + CLI catalog
  skills/powerbi-zerodata/
    SKILL.md                     # agent contract
    bin/pbz.cmd                  # CLI entry
    pbz/                         # Python CLI + guard
    engine/                      # C# declassification broker
    reference/                   # deep topic docs
    install/                     # hooks and junctions
    tests/                       # offline + fixture suites
    tools/                       # optional Microsoft npm CLIs
```

## Next

- [getting-started.md](getting-started.md)
- [cli-catalog.md](cli-catalog.md)
- [Agent hard rules](../skills/powerbi-zerodata/SKILL.md)
