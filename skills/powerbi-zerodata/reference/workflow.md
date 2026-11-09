# Review, evidence and recipes

## Review packet: `pbz review`

`pbz review` answers what a reviewer needs before accepting a change: what changed, what it touches,
how it ships, which checks ran on which revision, and what is still unverified. It writes
`pbz-workspace/REVIEW.md` and prints the same as JSON.

- **Changes by release class** (pending mirror edits since the last pull or push, from the semantic
  diff):

  | Class | Means | Next |
  |---|---|---|
  | `SECURITY_REVIEW` | roles, RLS filters, OLS, members | a human reviews; test with View as role |
  | `REFRESH_REQUIRED` | partitions, M, sources, refresh policy, new or removed import tables and columns | the user refreshes in Desktop, then `pbz health` |
  | `RECALC_REQUIRED` | calculated columns and tables, relationships, calculation groups | `pbz desktop open`, `pbz refresh`, `pbz health` |
  | `LOGIC` | measure and function DAX | `pbz check`, `pbz snapshot compare`, `pbz synth test run` |
  | `METADATA_ONLY` | descriptions, folders, formats, visibility, renames | validate and push |
  | `REPORT_ONLY` | PBIR pages, visuals, filters, bookmarks | `pbz push --reload`, `pbz smoke`, the user's visual review |

- **Impact**: dependents and visuals of each changed model object.
- **Suggested verification**: compiles checks for changed measures, saved snapshots, synthetic
  tests that mention the changed measures.
- **Evidence**: every verb that proves something appends a line to `.pbz/evidence.jsonl` with its
  stage, verdict counts and ids (never values) and the revision it ran on (a fingerprint of the
  mirror and of the real files as pbz last wrote them):

  | Stage | Recorded by |
  |---|---|
  | `PARSED` | `pbz validate` |
  | `BOUND` | `pbz push` (TOM, Microsoft's PBIR validator, built-in checks) |
  | `LOADED` | `pbz desktop open` / `reload` |
  | `EXECUTED` | `pbz health`, `check`, `snapshot compare`, `smoke` |
  | `STATIC_CONTRACT` | `pbz interactions check` in static mode. It cannot satisfy a runtime requirement or `HUMAN_ACCEPTED` |
  | `SYNTHETIC` | `pbz synth test run` |
  | `HANDOFF` | `pbz handoff` |
  | `HUMAN_ACCEPTED` | `pbz review --accept`, typed by the reviewer in a terminal (agents get `HUMAN_ONLY`) |

  Each stage is `CURRENT` (ran on this revision), `STALE` (an older revision, or real-data evidence
  while edits are still pending) or `NOT_RUN`. Stale evidence is never reported as a pass.
  `pbz review --interactions-spec <file>` binds a static interaction proof to that spec's digest.
  A later mirror revision or a different digest invalidates it.
- **Coverage**: whether external consumers are registered and whether the human accepted the
  revision. Changed pages are drawn as layout maps (boxes and labels, no data).

## Recipes: `pbz recipe run <file.json> [--param name=value]`

A recipe is a reviewable batch of mirror edits applied as one transaction: data, not code.

```json
{"recipe": "kpi-conventions", "version": 1,
 "params": {"table": "Sales"},
 "steps": [
   {"verb": "set", "args": ["--table", "{table}", "--kind", "measure", "--match", "%$", "formatString=0.0%"],
    "expect": {"result": "CHANGED"}},
   {"verb": "hierarchy", "args": ["add", "Date", "Calendar", "--level", "Year", "--level", "Month"],
    "on_error": {"NAME_TAKEN": "skip"}},
   {"verb": "style", "args": ["--type", "card", "--set", "labels.fontSize=20"]}
 ],
 "limits": {"max_files": 40}}
```

- Steps call pbz verbs that edit only the mirror (set, rename, move, new, relationship, add, style,
  filter, interaction, link, bookmark, copy, hierarchy, format-expr, function, culture, translate,
  perspective, role, refresh-policy, format, repair, bpa, a11y, mobile) plus read-only ones (validate,
  diff, layout, get, unused, lineage, describe). Push, pull, Desktop, engine checks, consumers, admin
  and arbitrary code are not available, and steps cannot point at another workspace.
- `{name}` placeholders take `params` from the recipe or `--param name=value`.
- A failed step (unless its error code is listed under `on_error` as `skip`), an unmet `expect`, more
  touched files than `limits.max_files`, or a validation error after the last step restores every
  mirror file to its state before the recipe (`ROLLED_BACK`). Otherwise `APPLIED`; then `pbz review`
  and `pbz push` as usual.
- `pbz recipe check <file>` validates a recipe without running it.

## Guards

- `pbz push` refuses model changes while Desktop holds Power Query edits that were never applied
  (`PENDING_PQ_CHANGES`: applying them later would overwrite the pushed expressions). The user applies
  or discards them in Desktop, saves, then `pbz pull --rebase`.
- Features that need a newer compatibility level refuse with `COMPAT_LEVEL` and name the level:
  dynamic format strings 1601, DAX user-defined functions 1702, object-level security 1400.
  `--upgrade-compat` raises it (one way: older Desktop builds cannot open the model).
