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
| `sign` | `expr` | always PASS; adds `sign`: NEG, ZERO, POS or BLANK |
| `subset` | `column`, `of` | values of `column` are all in `of` |

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

- `pbz smoke [--page <name or id>]`: per visual `OK`, `EMPTY`, `ERROR:<class>`, `UNSUPPORTED`,
  `N/A` (text boxes, images, buttons). The query approximates the visual (bindings plus simple
  In / comparison filters; `note: filters partial` when some were skipped). Cross-filtering from
  other slicers is not applied.
- `pbz probe <Table> [--columns A B]`: has_blanks, dup keys, negatives, future dates, plus
  row / blank-share / cardinality buckets and year span in the shape tier.

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
