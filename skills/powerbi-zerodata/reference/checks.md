# Data-blind checks

`pbz check <spec.json | inline JSON>` runs assertions inside pbz-engine against the model open in
Power BI Desktop. Results are verdicts, never values.

## Spec

```json
{"checks": [
  {"id": "total-nonblank", "kind": "nonblank", "expr": "[Total Hours]"},
  {"id": "buckets-add-up", "kind": "equal", "left": "[Total Hours]",
   "right": "[Project Hours] + [BAU Hours] + [Admin Hours]", "by": ["DimDate[Year]", "DimDate[Month]"], "tol": 1e-9},
  {"id": "pk-unique", "kind": "unique", "column": "'Projects'[ProjectID]"},
  {"id": "fk-orphans", "kind": "no_orphans", "from": "'Timesheet'[Project ID]", "to": "'Projects'[ProjectID]"},
  {"id": "date-respected", "kind": "differs", "left": "[Total Hours]",
   "right": "CALCULATE([Total Hours], REMOVEFILTERS(DimDate))", "by": ["DimDate[Year]"]},
  {"id": "type-independent", "kind": "filter_independent", "expr": "[Total Projects]", "column": "'Timesheet'[Resource Type]"},
  {"id": "new-measure-binds", "kind": "compiles", "table": "Timesheet", "expr": "DIVIDE([A], [B])"},
  {"id": "gap-sign", "kind": "sign", "expr": "[Capacity Gap]"},
  {"id": "one-owner", "kind": "nonblank", "expr": "[Total Hours]",
   "where": ["'DimPortfolioOwner'[Owner] = \"«DimPortfolioOwner[Owner]#1»\""]}
]}
```

| kind | fields | PASS means |
|---|---|---|
| `compiles` | `expr`, `table` | DAX binds (syntax, names, types) without evaluating |
| `executes` | `expr` | evaluates without error |
| `nonblank` | `expr`, `where?` | scalar is not BLANK |
| `nonempty` | `expr` (table) | table has at least one row |
| `equal` | `left`, `right`, `by?`, `where?`, `tol?` | equal overall, or in every `by` group (BLANK = BLANK only) |
| `differs` | same as equal | not equal (somewhere) |
| `additive` | `expr`, `by` | total equals the sum over `by` groups |
| `filter_independent` | `expr`, `column`, `by?` | unchanged when that column's filters are removed |
| `unique` | `column` | no duplicate values |
| `no_orphans` | `from`, `to` | every non-blank `from` value exists in `to` |
| `no_blank_keys` | `column` | no blanks |
| `sign` | `expr`, optional `expect` | PASS when `expect` is omitted or includes the result; otherwise FAIL. Adds `sign`: NEG, ZERO, POS, or BLANK. A non-numeric value is `TYPE_MISMATCH` |
| `subset` | `column`, `of` | values of `column` are all in `of` |
| `rls` | `expect`, `role` or `user`, and `query` or `column` or `table` | role-scoped count matches `expect`. Real models need a human terminal. A verified `pbz-synth` tree does not. `OLS_INACCESSIBLE` passes only when the role query fails as an unknown object |
| `perf` | `query`, `expect` = `NOT_SLOWER` | median is not slower than the saved baseline (`FASTER`, `SAME`, or `NOISY`). No milliseconds leave the engine |

Statuses: `PASS`, `FAIL`, `EMPTY` (nothing to compare), `UNSUPPORTED`, `BUDGET`,
`ERROR` + `class` (SYNTAX, UNKNOWN_OBJECT, TYPE_MISMATCH, CONVERSION, CIRCULAR, DUPLICATE_KEY,
TIMEOUT, ENGINE_UNAVAILABLE, BAD_REQUEST, OTHER).

Grouped checks add `mismatch`: `NONE`, `ONE`, `FEW` (2-5), `MANY` (>5), and up to 10 failing
`groups`. Keys of date and category columns are shown as values; other keys come back as tokens
(`«Table[Column]#n»`, or `«Table[Column]@g#n»` when the value only exists in the data). Feed a
column token into `where` to narrow the next check. Opaque `@g` tokens cannot be used in `where`.

In the default `shape` disclosure tier, `unique` / `no_orphans` / `no_blank_keys` also report an
`offenders_bucket` (0, 1-9, 10-99, 100-999, 1k-9k, 10k+). The `strict` tier returns booleans only.

## Limits

- Constants: literals that already appear in the model, tokens, and values the user gave you
  are fine. At most 3 distinct new constants per target expression per day. No `between`.
- Budget: 300 checks per day per workspace, 60 per target expression. `BUDGET` = stop and ask.
  Only the user can reset (`pbz admin budget-reset` in their terminal).

## Snapshots (regression)

```json
{"queries": [
  {"id": "by-month", "keys": ["DimDate[Year]", "DimDate[Month]"],
   "dax": "EVALUATE SUMMARIZECOLUMNS(DimDate[Year], DimDate[Month], \"total\", [Total Hours], \"util\", [Utilization %])"}
]}
```

`pbz snapshot save before-x --spec q.json`, edit + push + reopen, then
`pbz snapshot compare before-x`. Per query: `SAME`, `DIFF` (+ changed `columns`, `mismatch`),
`NEW`, `MISSING`, or `STALE_DATA` (the model was refreshed in between, so compare is meaningless).
Only HMACs of results are stored, in the private store.

## Smoke and probe

- `pbz smoke [--page <name or id>]`: per visual `OK`, `EMPTY`, `ERROR` with a separate `class`,
  `UNSUPPORTED`, `N/A` (text box, image, shape, button, navigator, or iframe), `BUDGET`, or
  `PROTOCOL_ERROR`. Coverage is `FULL`, `PARTIAL`, or `NONE`. The query uses bindings plus simple
  In and comparison filters. A skipped filter, or a page that has slicers, sets coverage to
  `PARTIAL`. Cross-filtering from other slicers is not applied.
- `pbz probe <Table> [--columns A B]`: `has_blanks`, `has_negatives` on numeric columns,
  `has_future_dates` on date columns, plus row, blank-share and cardinality buckets in the
  shape tier. It does not return duplicate-key or orphan counts, and it does not return
  calendar year endpoints. Duplicate keys and orphans are `pbz check` kinds (`unique`,
  `no_orphans`).
- `pbz health`: objects in error. A measure may include a scrubbed message. Column, partition,
  hierarchy, and relationship errors are the class alone. An unknown class from health, probe,
  index, or snapshot is `ENGINE_UNAVAILABLE`.
- `pbz index`: type for every column; text columns also get `class` (`empty`, `category`, or
  `high`). In the shape tier, text columns may include `card_bucket`, and every column may
  include `blank_bucket`. There is no calendar year field. A link at the private `index.json`
  is refused before it is opened.

## Disclosure ledger

Budgets live in the private store and are charged before anything reaches the engine:

- per target (the checked expression), at most `constants_per_target` (3) distinct constants within
  30 days. Constants are the literals in the check and, since an edited measure is a new question,
  every literal you pushed into a measure the target reaches (pushes record their new literals as
  hashes);
- per day: `session_checks` (300), `per_target` (60), `probes_per_day` (100),
  `snapshot_queries_per_day` (200), `smoke_runs_per_day` (50).

`BUDGET` is final for the day (or the 30-day window for constants); only the user can reset it. To
test logic freely use `pbz synth` (fake data) instead of the real model.
