# Model audit: efficiency and consolidation (`pbz audit`, `pbz consolidate`)

`pbz bpa` checks rules. `pbz audit` finds where the model wastes memory or query time and where logic is
copied that could live once, and connects each finding to a fix and a way to prove the fix changed nothing.
It reads the mirror (names, DAX, M) and, when available, two kinds of bucketed evidence:

| Evidence | From | What it adds |
|---|---|---|
| static | the mirror | every rule runs on names and formulas |
| index buckets | `pbz index` (Desktop open) | distinct and row buckets (`0`, `1-9`, ... `10k+`) raise or lower severity |
| storage bands | `pbz audit memory` (Desktop open) | per column and table: size band, share band, dictionary-heavy flag, and three yes/no predicates |

A smell is a candidate, not a measurement: a nested iterator or a large column says "look here", not "this
is slow". The top distinct bucket is `10k+`, so rules whose published thresholds are higher (Microsoft's
100k distinct values for bidirectional relationships) say so and use `confidence: low`.

## Use

```
pbz audit                                   # all findings, most severe first
pbz audit --category consolidation          # or efficiency, design
pbz audit --rule CALC_COLUMN_AS_MEASURE --rule NESTED_ITERATOR
pbz audit --min-severity medium
pbz audit --coverage                        # every rule: source, evidence it needs, ran or skipped
pbz audit memory                            # storage bands from the open Desktop model, saved to .pbz/memory.json
```

Each finding: `id` (stable fingerprint, from the object's lineageTag where it has one, so renames keep it),
`rule`, `category`, `severity` (high, medium, low, info), `confidence`, `object`, `message`, `evidence`,
and usually `fix`, `verify`, `blockers` (why an automatic change is unsafe) and `members`.

## Rules

Efficiency

| Rule | Finds | Evidence |
|---|---|---|
| `ITERATOR_MEASURE_CALL` | `SUMX('Fact', [Measure])` style: a measure evaluated per row of a fact table (context transition per row) | rows bucket |
| `NESTED_ITERATOR` | an iterator over a table inside another iterator's row expression, also through called measures | rows bucket |
| `SUMMARIZE_EXTENSION_COLUMNS` | `SUMMARIZE` with name/expression pairs (use `ADDCOLUMNS(SUMMARIZE(...))` or `SUMMARIZECOLUMNS`) | static |
| `CALC_COLUMN_AS_MEASURE` | a numeric calculated column only used as `SUM/AVERAGE/MIN/MAX(column)` in measures; lists the measures | rows bucket, bands |
| `CALC_COLUMN_UPSTREAM` | a row-local calculated column (same-table columns, scalar functions) that Power Query or the source could compute | distinct bucket |
| `UNUSED_HIGH_CARDINALITY` | unused columns with `10k+` distinct values or a large band: remove these first | bucket, bands |
| `HIGH_CARDINALITY_BIDIRECTIONAL` | a bidirectional relationship on a `10k+` key | distinct bucket |
| `MIXED_STORAGE_DIMENSION` | an Import dimension filtering a DirectQuery table (limited relationship): make it Dual | static |
| `MEMORY_HOTSPOT` | a column with `5-20%` / `>=20%` of storage or `100-1000 MiB` and up, with its usage state | bands |
| `DATETIME_TIME_PART` | date/time columns that store times of day (split or truncate; a format string keeps the time) | predicate |
| `DOUBLE_WITHOUT_FRACTIONS` | decimal columns holding whole numbers only | predicate |
| `LONG_TEXT` | text columns with values over 100 characters and a large band | predicate |
| `DICTIONARY_HEAVY` | columns whose storage is mostly the dictionary of distinct values | bands |
| `LARGE_RELATIONSHIP` | a relationship structure of `10-100 MiB` or more: a key with very many distinct values | bands |
| `AUTO_DATETIME_STORAGE` | hidden auto date/time tables holding `1-5%` of the model or more | bands |
| `M_BUFFER_BEFORE_FILTER` | `Table.Buffer` before a later `Table.SelectRows` | static |

Consolidation

| Rule | Finds | Fix |
|---|---|---|
| `DUPLICATE_MEASURE` | identical DAX (keeps the most used one) | `pbz consolidate merge "[Copy]" --into "[Keep]"` |
| `CALC_GROUP_FAMILY` | one transformation (YTD, PY, ...) applied to several base measures | `pbz consolidate calc-group --family <id>` |
| `FILTERED_FAMILY` | at least three measures that differ only by literal filter values | review: base measure plus slicer, field parameter or calculation group |
| `PARAMETERIZED_FAMILY` | at least three measures with one formula over different columns | `pbz function add` (DAX user-defined function, level 1702) |
| `REPEATED_SUBEXPRESSION` | a sub-expression of 15+ tokens copied into 3+ measures; says whether a helper measure is safe (filter context only) or a VAR / function is needed (row or filter-argument context, where a measure would add context transition) | `pbz function add` or a new measure |
| `DUPLICATE_TABLE` | tables loaded from the same query with the same columns; role-playing copies are called out | load once, relate to it |
| `MULTIPLE_DATE_TABLES` | more than one date table, with the tables each one filters | one date table plus `USERELATIONSHIP`, unless two date roles must filter at once |
| `RELATED_COPY_COLUMN` | a calculated column that only copies a dimension column with `RELATED` | bind to the dimension column, remove the copy |
| `COPY_COLUMN` | a calculated column that only repeats another column of its table | use that column |
| `M_SHARED_STEPS` | queries repeating the same source and first steps | one staging query or function (code consolidation; each query still evaluates the source) |

Design

| Rule | Finds | Fix |
|---|---|---|
| `UNFOLDERED_MEASURES` | a table showing more than 10 measures without display folders | `pbz set --table T --kind measure --match ... displayFolder=...` |
| `MEASURES_SCATTERED` | measures spread over 4+ tables (20+ measures) and no measure table | `pbz new measure-table`, `pbz move measure` |

Rules already in `pbz bpa` (DIVIDE, IFERROR, FILTER on whole tables, counts of bidirectional and
many-to-many relationships, RELATED in calculated columns, auto date/time, inactive relationships) are not
repeated.

## Consolidate

`pbz consolidate` turns consolidation findings into mirror edits that keep every report binding working.
Review with `pbz diff`, then `pbz push` (TOM and PBIR validation).

```
pbz consolidate families                                  # calculation-group candidates with ids
pbz consolidate merge "[Sales Copy]" --into "[Total Sales]" [--dry-run] [--force] [--keep-alias]
pbz consolidate calc-group --family <id> --name "Time Calc" [--column Name] [--precedence 10] [--dry-run]
```

- `merge` retargets every reference from one measure to the other (measures, calculated columns and
  tables, calculation items, format strings, detail rows, KPIs, RLS, functions; visual projections,
  filters, sorts, conditional formatting, bookmarks, report-level measures; perspectives; translations),
  then removes it. Output counts the rewrites per area. Refusals: `NOT_EQUIVALENT` (DAX differs) and
  `FORMAT_DIFFERS` (static or dynamic format differs), both lifted by `--force`; `CYCLE` (the kept measure
  reaches the duplicate, never lifted); `AMBIGUOUS` (a column has the same name); `UNSUPPORTED` (a
  cross-table merge where one report filter alias also serves other fields of the duplicate's table, the
  same limit as `pbz move`). It keeps the duplicate as a hidden alias with the same lineageTag
  (`[Copy] = [Keep]`) when registered consumers use it, its usage is unknown or the analysis is incomplete.
  Warnings: `USED_EXTERNALLY`, `DROPPED_WITH_DUPLICATE` (its KPI or detail rows), `NAME_IN_STRINGS` (DAX
  text such as `SELECTEDMEASURENAME()` comparisons or field-parameter labels still spell the old name),
  `UNRETARGETED`. Visual headers now show the kept measure's name, as after a rename.
- `families` lists calculation-group candidates: templates (the member's DAX with the base measure
  replaced by `SELECTEDMEASURE()`) applied to at least three base measures. Notes: `NESTED_MEASURES`
  (a base calls other measures), `ITEM_REFERENCES_MEASURES`, `DUPLICATE_MEMBERS`.
- `calc-group` creates the calculation group (one item per template, fresh lineageTags, compatibility
  level raised to 1500 when needed, `discourageImplicitMeasures` set) and rewrites each member into
  `CALCULATE([Base], 'Group'[Name] = "Item")`; only the expression changes, so name, lineageTag,
  formats, folder, description and visibility stay and every visual keeps working. Refusals:
  `NAME_TAKEN` (table, file or precedence taken), `NOT_FOUND`. Warnings: `IMPLICIT_MEASURES` (visuals
  that aggregate columns: a calculation group turns implicit measures off), `OTHER_CALC_GROUPS` (check
  precedence). It writes `pbz-workspace\consolidate-<id>.check.json`: one `equal` check per member, the
  old expression against the wrapper, by the date table's Year and Month (push ignores the file).
  Checked in Desktop on fake data (`tests/test_consolidate.py --live`): YTD and PY wrappers equal the old
  measures by year and month, also when the base calls another measure (the item applies once, so PY does
  not shift twice). Other items and other models still need their spec run: TOM checks only syntax.

Verify a consolidation on fake data first, then on real data with verdicts only:

```
pbz synth build ; pbz synth open
pbz synth check --spec <the spec the verb wrote>     # counterexamples on fake data
pbz push ; pbz desktop open --wait 300
pbz check <the same spec>                            # PASS / FAIL per measure on the real model
```

## Baselines, waivers, gate

```
pbz audit --save-baseline release-12        # fingerprints, rules and names only (.pbz/audit/baselines/)
pbz audit --since release-12                # each finding new or persistent; resolved ones listed
pbz audit --since release-12 --fail-on new  # exit code 1 when a change adds findings (CI)
pbz audit --fail-on high                    # or any (everything but info)
pbz audit waive <id> --reason "accepted until the Q3 rewrite" --expires 2027-03-31
pbz audit waive <id> --remove
```

Waived findings move to `waived` and do not count toward the gate until they expire (`expired_waivers`).
Waivers record who added them (agent or human).

## Storage bands: what `pbz audit memory` releases

The engine reads two storage DMVs (`DISCOVER_STORAGE_TABLE_COLUMNS`, `DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS`,
fixed column lists) inside the broker and maps dictionary, data, attribute-hierarchy, user-hierarchy and
relationship structures to model objects by their ids, like VertiPaq Analyzer. Only fixed bands leave the
process: size `<1 MiB`, `1-10 MiB`, `10-100 MiB`, `100-1000 MiB`, `>=1000 MiB`; share of the model `<1%`,
`1-5%`, `5-20%`, `>=20%`; `dictionary_heavy`; and three yes/no predicates computed with fixed DAX on Import
tables only (`time_part` for date/time columns, `fractional` for decimal columns, `long_text` for text over
100 characters). Output is sorted by name, never by size.

```
{"verb": "memory", "model": {"size_band"}, "tables": [{"name", "size_band", "share_band",
  "columns": [{"name", "size_band", "share_band", "dictionary_heavy", "time_part" | "fractional" | "long_text"}],
  "hierarchies": [{"name", "size_band"}]}], "relationships": [{"name", "from", "to", "size_band"}],
 "other": {...}, "auto_datetime": {...}, "coverage": "FULL" | "PARTIAL", "predicates": "OFF" | "DONE" | "PARTIAL"}
```

- Relationship structures count toward the from-table, as in VertiPaq Analyzer; auto date/time tables are
  summed into `auto_datetime`; storage that maps to no listed object is `other` and makes coverage PARTIAL.
- Each run is charged to the daily probe budget (`BUDGET` when spent). `--no-predicates` skips the DAX
  checks; predicates are capped (largest columns first, bounded time) and a failing one is left out.
- Strict disclosure (`"disclosure": "strict"` in the policy) withholds every band; names,
  `dictionary_heavy` and the predicates stay, and audit then reports the predicate rules at low severity.
- Text columns reserve dictionary space, so small text columns sit in `1-10 MiB`; audit ignores shares
  when the whole model is under 10 MiB and needs `10-100 MiB` or more before calling an unused column
  expensive from bands alone.
- `.pbz/memory.json` records the mirror revision, so audit marks the bands `stale` after mirror edits. A
  data refresh in Desktop changes sizes without changing the mirror: rerun `pbz audit memory` after one.

Bands are coarse facts about real data, like row buckets: treat them as internal, never quote them as
business facts, and do not copy them into notes or memory.

## Sources

Rules and thresholds follow Microsoft's Best Practice Analyzer rules and optimization guidance, SQLBI's
DAX patterns, VertiPaq Analyzer's storage attribution, Tabular Editor's conventions, Bravo's date-table
practice and Measure Killer's consumer-aware cleanup; no tool or rule file is copied or executed.
