# `pbz` CLI catalog

Every top-level verb registered in `skills/powerbi-zerodata/pbz/cli.py`. Global flags: `--project`,
`--workspace`. Output is JSON (plus human text where noted) with metadata or closed-vocabulary
verdicts only.

Deep docs are linked per group. For flags not listed here, run `pbz <verb> -h`.

## Workspace and mirror

| Verb | Purpose | Nested / key flags | Docs |
|---|---|---|---|
| `capabilities` | Closed matrix of what pbz can certify | — | [workspace.md](../skills/powerbi-zerodata/reference/workspace.md) |
| `status` | Workspace, mirror sync, Desktop, engine, guard | — | workspace |
| `doctor` | Read-only OS-isolation checklist | — | [leak-vectors.md](../skills/powerbi-zerodata/reference/leak-vectors.md) |
| `init` | Policy + scan + first pull | — | workspace |
| `scan` | Find data-bearing / sensitive files | `--dry-run` | workspace |
| `pull` | Refresh sanitized mirror | `--force`, `--rebase` | [tokens.md](../skills/powerbi-zerodata/reference/tokens.md) |
| `diff` | Pending mirror vs real | `--patch` | tokens |
| `push` | Validate and write real files | `--reload`, `--dry-run`, `--only` | tokens |
| `revert` | Discard mirror edits | paths, `--object` | workspace |
| `validate` | Mirror metadata lint / PBIR | — | workspace |
| `preflight` | Full staged validation, no apply | `--only` | [improvements.md](../skills/powerbi-zerodata/reference/improvements.md) |
| `recover` | Interrupted push recovery | `--transaction` | improvements |

## Orient

| Verb | Purpose | Notes | Docs |
|---|---|---|---|
| `describe` | Data dictionary (model / table / measure / column) | Metadata only | workspace |
| `values` | Column shape from the mirror | Not a live grid | workspace |
| `rename` | Rename measure/column/table + fix refs | `kind` required | [authoring.md](../skills/powerbi-zerodata/reference/authoring.md) |
| `move` | Move measure to another table | `--to` | authoring |

## Desktop and engine verification

| Verb | Purpose | Nested / key flags | Docs |
|---|---|---|---|
| `desktop` | Drive Desktop without screenshots | `status` \| `open` \| `close` \| `reload` | [checks.md](../skills/powerbi-zerodata/reference/checks.md) |
| `index` | Column classification index in Desktop | — | checks |
| `refresh` | Refresh open model | `--tables` | checks |
| `health` | Objects in error (classes only) | — | checks |
| `check` | Data-blind assertions | `--validate-only`, `--diagnostic` | checks |
| `check-schema` | JSON schema for check specs | — | checks |
| `probe` | Table shape: booleans / coarse buckets | `--columns` | checks |
| `snapshot` | Save/compare query shapes | `save` \| `compare` | checks |
| `smoke` | Per-visual approximate query | `--page` | checks |

## Verification workflows

| Verb | Purpose | Nested | Docs |
|---|---|---|---|
| `preview` | Watermarked synthetic layout SVG | `--page`, `--spec` | [preview.md](../skills/powerbi-zerodata/reference/preview.md) |
| `interactions` | Navigation / slicer contracts (contract 2 tokens; native is `UNSUPPORTED`) | `capabilities` \| `check`; `--mode static\|native` | improvements |
| `freshness` | Refresh provenance contracts | `capture` \| `compare` | improvements |
| `requirements` | Revision-bound requirement checks | `import` \| `status` \| `verify` \| `accept` | improvements |
| `consent` | Human file approval receipts | `request` \| `status` \| `approve` \| `read` | improvements |
| `review` | Review packet + evidence | `--accept` (human-only), `--interactions-spec` | [workflow.md](../skills/powerbi-zerodata/reference/workflow.md) |
| `handoff` | Human visual checklist | `--all` | workflow |
| `recipe` | Transactional JSON edit batches | `run` \| `check` | workflow |

## Analysis and hygiene

| Verb | Purpose | Nested / key flags | Docs |
|---|---|---|---|
| `unused` | Usage states; optional remove/restore | `--remove`, `--restore`, `--cascade`, `--verify-engine` | [analysis.md](../skills/powerbi-zerodata/reference/analysis.md) |
| `lineage` | Dependency tree | `--up`, `--depth` | analysis |
| `suggest` | Cleanup suggestions | `--report` | analysis |
| `bpa` | Best-practice analysis | scope `model\|report\|all`; `--fix`, `--opinion` | [bpa.md](../skills/powerbi-zerodata/reference/bpa.md) |
| `similar` | Near-duplicate measures | `--threshold`, `--max` | analysis |
| `changes` | Semantic diff of mirror snapshots | `--from`, `--to`, `--list` | analysis |
| `docs` | Model/report documentation export | `--format md\|html\|json` | analysis |
| `m-lineage` | Power Query step/column lineage | optional table | analysis |
| `audit` | Efficiency / consolidation findings | `run` \| `waive` \| `memory` | [audit.md](../skills/powerbi-zerodata/reference/audit.md) |
| `consolidate` | Apply consolidations | `merge` \| `families` \| `calc-group` | audit |
| `consumers` | External consumer registry | `list` \| `add` \| `scan` \| `show` \| `discover` | [consumers.md](../skills/powerbi-zerodata/reference/consumers.md) |

## Authoring and report edit

| Verb | Purpose | Nested | Docs |
|---|---|---|---|
| `new` | Scaffolds | `measure-table` \| `date-table` \| `calc-group` \| `field-parameter` | authoring |
| `add` | Create page or visual | `page` \| `visual` | authoring, [visuals.md](../skills/powerbi-zerodata/reference/visuals.md) |
| `copy` | Duplicate page or visual | `page` \| `visual` | authoring |
| `link` | Button actions / tooltips | page + visual + action flags | authoring |
| `filter` | Report/page/visual filters | `add` \| `remove` \| `list` | authoring |
| `bookmark` | Show/hide bookmarks | `add` \| `remove` \| `list` | authoring |
| `interaction` | Visual cross-filter mode | `page source target filter\|highlight\|none\|default`, or `page --show` | authoring |
| `style` | Visual formatting | `--set`, `--unset`, `--canvas`, `--list` | [style.md](../skills/powerbi-zerodata/reference/style.md) |
| `image` | Stage report images | `add` \| `list` \| `remove` | authoring |
| `visual` | Built-in + custom catalog | `list` \| `roles` \| `import` \| `register` | visuals |
| `layout` | Text canvas map + lint | `--page`, `--no-map` | authoring |
| `a11y` | Accessibility audit / tab order | `--fix-tab-order` | authoring |
| `mobile` | Phone layout positions | `--pos`, `--remove` | authoring |
| `format` | Token-safe DAX formatter | `--apply` | authoring |
| `repair` | Structural PBIR fixes | `--dry-run`, `--fields`, `--lineage` | authoring |
| `relationship` | Model relationships | `add` \| `remove` \| `set` | authoring |
| `set` / `get` | Typed model properties | `--kind`, `--table`, `--match`, `--folder` | authoring |
| `hierarchy` | User hierarchies | `add` \| `levels` \| `remove` | authoring |
| `format-expr` | Dynamic format string | `--remove` | authoring |
| `function` | DAX UDFs | `add` \| `update` \| `rename` \| `remove` \| `list` | authoring |

## Model governance

| Verb | Purpose | Nested | Docs |
|---|---|---|---|
| `culture` | Cultures | `add` \| `remove` \| `list` \| `missing` | [governance.md](../skills/powerbi-zerodata/reference/governance.md) |
| `translate` | Captions / descriptions / folders | — | governance |
| `perspective` | Perspectives | `add` \| `remove` \| `include` \| `exclude` | governance |
| `role` | RLS roles + OLS | `add` \| `remove` \| `filter` \| `unfilter` \| `member-add` \| `member-remove` \| `ols` | governance |
| `refresh-policy` | Incremental refresh | `set` \| `remove` \| `show` | governance |

## Synthetic model, MCP, admin

| Verb | Purpose | Nested | Docs |
|---|---|---|---|
| `synth` | Fake-data model agents may query | `build` \| `status` \| `open` \| `close` \| `query` \| `remove` \| `check` | [synth.md](../skills/powerbi-zerodata/reference/synth.md) |
| `synth-test` | Golden / SAME·DIFF tests (alias `pbz synth test`) | `save` \| `run` \| `list` \| `remove` | synth |
| `mcp` | Stdio MCP server of pbz tools | — | [mcp.md](../skills/powerbi-zerodata/reference/mcp.md) |
| `memscrub` | Scrub agent memory folders | `--dir`, `--apply` (human), `--show` (human) | workspace |
| `admin` | Human-only policy / budget / log | `budget-reset` \| `allow` \| `category` \| `synthetic` \| `log` \| `consumer-remove` | workspace |

## Exit codes (summary)

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Failed assertion / rolled-back operation |
| 2 | Invalid request |
| 3 | Blocked / inconclusive |
| 4 | Unavailable capability |
| 5 | Partial (e.g. push ok, reload failed) |

See [improvements.md](../skills/powerbi-zerodata/reference/improvements.md) for the outcome envelope and honesty notes.
