# Synthetic test model (`pbz synth`)

Every other engine verb answers with verdicts and buckets because it runs on real data. `pbz synth`
gives you a copy of the model where nothing is real, so you may look at query results directly: use it
to debug DAX logic (does this YTD reset in January, does this filter keep blanks, which rows does this
measure count) before you check the real model with `pbz check`.

## Use

```
pbz synth build [--rows 200] [--seed 7]   # writes %USERPROFILE%\pbz-synth\<ws-id>\<Project>Synth.pbip
pbz synth open                            # opens it in Desktop and waits for the engine
pbz synth query "EVALUATE SUMMARIZECOLUMNS('Date'[Year], \"Sales\", [Total Sales])"
pbz synth status | close | remove
```

Rebuild after you change the model (`build` replaces the previous copy; close it first). The copy is
a normal PBIP folder marked synthetic, so you may also read its files and run `pbz --workspace <that
folder> health | smoke | check` there.

## Regression tests: `pbz synth test`

Golden results on the synthetic copy catch logic changes in a refactor without real data.

```
pbz synth test save <name> --measure "[Total Hours]" --by "'Project'[Status]"   # or --spec '{"queries": [...]}'
# edit DAX, then: pbz synth build (same --seed / --rows), pbz synth open
pbz synth test run <name> [--tol 1e-9]
pbz synth test list | remove <name>
```

`save` runs the queries through `pbz synth query` (a spec is `{"queries": [{"id", "dax", "keys"}]}`,
as a JSON string or file) and stores the fake results, the build's seed, rows and tables, and a hash
of the mirror model in `.pbz/synth-tests/<name>.json`. `run` answers per query SAME, DIFF (changed
cells, missing and added rows, keyed by the grouping columns; floats within `--tol`, relative) or
ERROR:<class>. Drift is reported separately: `drift.model` (the mirror changed since the save:
expected after a refactor, so review each DIFF), `drift.synth_stale` (the copy is older than the
mirror: rebuild and reopen it first), `drift.synth` (rebuilt with another seed or row count: the
verdict is NOT_COMPARABLE). Results over 500 rows are refused (TOO_MANY_ROWS): group by fewer columns
or use TOPN. Only the synthetic copy ever answers: the engine checks its marker. `pbz synth-test` is
the same verb.

## What it contains

- Built from the sanitized mirror only. The real `*.SemanticModel` / `*.Report` folders are never read.
- Every data partition (M, entity, DirectQuery, and calculated tables that embed literal rows) becomes a
  calculated `DATATABLE` of fake rows. Dimension tables (the one side of a relationship) get up to 50
  rows with unique keys; other tables get `--rows` rows. Foreign keys are drawn from the generated keys,
  and the last rows are orphans (plus an empty text key) on purpose, so relationship checks have
  something to find.
- Text values are `<Column> 1`, `<Column> 2`, ... plus every literal and mirror token that the DAX and
  the report filters compare the column with. Filtered measures and slicers therefore return rows: a
  measure filtering `Person[Name] = "«Person[Name]#3»"` finds that token as a fake name.
- Tokenized numbers in DAX (bare `«...»` tokens) become a fixed fake number per token, and the same number
  appears in the column's fake data. Dates fall inside the model's calendar (the `CALENDAR` range of a
  calculated date table, else 2024 to 2026). Numbers are seeded random.
- Measures, calculated columns, calculated tables without embedded rows, calculation groups, field
  parameters, hierarchies, relationships and roles (members dropped) are kept. Shared M queries are
  dropped. Every object gets a new lineageTag.

Fake data says nothing about real magnitudes, distributions or cardinalities: never report numbers from
a synthetic query as facts about the business.

## Why it cannot leak real values

- Inputs are the mirror (names, formulas, tokens) and a random generator. No real value is in the copy.
- `synth-query` is the only engine verb that returns values. Before evaluating, the engine checks that
  every partition of every user table is calculated (no data source can feed the model) and that the
  model annotation `PBZ_SYNTHETIC` equals an HMAC, with the workspace's private key, of the model's table
  lineage tags. A real model has neither: its tags differ from the regenerated synthetic ones and you
  cannot compute the HMAC without the key, which lives in the guard-denied private store.
- `pbz push` (and `pbz validate`) refuse a `PBZ_SYNTHETIC` annotation in a real project
  (`SYNTHETIC_MARKER_IN_REAL_MODEL`), so the marker can never be moved onto a real model.
- `pbz synth query` also requires that the Desktop instance holds the synthetic PBIP file itself.
- The query text passes the same guard as `snapshot` and `smoke` (DAX only, no `INFO.*`, no
  `EVALUATEANDLOG`); results are capped at 500 rows.

## Calling it from a real workspace session

Run it through the `pbz` launcher (`pbz synth query "EVALUATE ..."`). The guard allows a single `pbz`
invocation with DAX in its arguments, but blocks the same DAX passed to `python ... cli.py`, since
from the shell it cannot tell that call apart from a direct engine query.
