# Analysis verbs: unused, lineage, remove, suggest, m-lineage

All of these read the sanitized mirror (`pbz-workspace\`) and print object names, states, counts
and codes. They never read values. Mirror tokens `«...»` are treated as opaque literals, never as
references.

## Usage graph

`pbz` builds one graph per project from the mirror:

- **Model objects**: tables, columns, measures, hierarchies, calculation items, relationships,
  roles, shared M queries and parameters, DAX user-defined functions.
- **Model edges**: every DAX expression an object owns (measure expression, KPI target / status /
  trend, dynamic format string, detail rows, calculated column, calculated table, calculation item,
  RLS filter, UDF body), sort-by columns, hierarchy levels, relationships and their key columns,
  auto date/time variations, aggregations (`alternateOf`), refresh policies, M query references.
- **Report usages**: every PBIR file of every report bound to the model by path (`definition.pbir`
  `byPath`), plus thin reports passed with `--report <folder>`: visual projections, visual / page /
  report filters, sorts, conditional formatting, slicer states, bookmarks, mobile layouts and
  report-level measures (`reportExtensions.json`).

DAX resolution follows Tabular Editor's rules: `'T'[x]` is a column of T, else a measure; a bare
`[x]` is a measure, else a column of the iterator table in scope, else of the home table; calculated
columns, RLS filters and calculation items look in the home table first. `VAR` tables and
`ADDCOLUMNS` / `SELECTCOLUMNS` names are extension columns, not model references. Anything that
matches nothing is listed under `warnings` as `UNRESOLVED_REF`.

## States

| State | Meaning |
|---|---|
| `used` | reached from a report usage |
| `used_external` | reached only from a registered external consumer (`consumers` names them; see [consumers.md](consumers.md)); never removed |
| `locked` | needed by the model itself: kept relationships (both tables alive), RLS / OLS, security roles, aggregation tables, marked date table key, incremental refresh policy |
| `used_by_unused` | only dead objects refer to it (`blocked_by` names them). Remove those first, or use `--cascade` |
| `unused` | nothing refers to it |
| `unknown` | a report file or a registered consumer could not be read or fully covered: nothing is called unused |

A table is unused only when nothing outside it (other than its own relationships) refers to it or
to one of its objects. Hidden is not unused. Auto date/time tables are counted
(`auto_date_tables`) and listed only with `--all`. Bookmarks no button or navigator points at are
`unreferenced`, not unused: viewers can still open them from the Bookmarks pane.

## Removal

`pbz unused --remove` edits the mirror only:

- deletes the TMDL blocks (with their `///` descriptions) of unused measures, columns, hierarchies,
  relationships, functions and queries; whole tables go as a file plus their `ref table` line;
- drops removed tables and queries from the `PBI_QueryOrder` annotation and from perspectives;
- removes unused report-level measures and undeclared custom visuals (bookmarks only with
  `--only bookmark`);
- for import columns, appends `#"pbz Removed Columns" = Table.RemoveColumns(<last step>, {...},
  MissingField.Ignore)` to the table's M query (`--m-step keep` uses `Table.SelectColumns` of the
  remaining columns, `--m-step none` leaves the query alone; Desktop then adds the column back on
  the next refresh). Queries that are not a simple `let ... in <step>` are skipped with a reason;
- removes the auto date/time table and relationship of a removed date column.

Manual only (listed under `skipped`): columns of calculated tables (edit the table expression),
columns of tables with an incremental refresh policy, objects with translations in `cultures/`
(never mirrored; TOM would refuse the push), parameters, calculation items, roles.

Each run writes a restore point to `pbz-workspace\.pbz-trash\<stamp>\` (the previous mirror text of
every touched file). `pbz unused --restore <stamp>` puts it back if the files have not changed since;
restore later points first. Push as usual; push validates the staged files with TOM, the report
schema, field references and lint, so a removal that breaks a reference is refused.

## Suggestions (`pbz suggest`)

`UNUSED_OBJECTS`, `AUTO_DATE_TIME`, `INACTIVE_RELATIONSHIP_UNUSED`, `VISIBLE_KEY_COLUMN` (many-side
relationship key never shown), `IS_AVAILABLE_IN_MDX` (hidden helper column with an attribute
hierarchy), `UNUSED_VARIABLE`, `SINGLE_FIELD_PARAMETER`, `EMPTY_FILTER`, `VISUAL_WITHOUT_FIELDS`,
`HIDDEN_PAGE_UNREFERENCED` (not a tooltip or drillthrough page and nothing navigates to it),
`HIDDEN_VISUAL` (no bookmark captures it), `UNUSED_CUSTOM_VISUAL`.

## Power Query lineage (`pbz m-lineage`)

A let-step analyser reads each import table's query: source functions, select / remove / rename /
add / duplicate / expand / group / merge / split / combine / unpivot steps, and transforms that keep
the column set. Column names come only from column-name argument slots. Steps it cannot follow make
the column set `dynamic` (the source schema is not visible to pbz). Problem codes:

- `COLUMN_REMOVED_IN_QUERY`: a model column's `sourceColumn` is removed by a step (refresh fails).
- `COLUMN_NOT_IN_QUERY`: the query's known output has no such column.
- `QUERY_COLUMN_NOT_IN_MODEL`: the query outputs a column the model lacks; Desktop adds it on the
  next refresh.

## Engine cross-check (`pbz unused --verify-engine`)

With the project open in Desktop, pbz-engine reads `DISCOVER_CALC_DEPENDENCY` (object identity
columns only, names checked against the model) and pbz compares measure and column edges.
`engine_only` edges are parser misses; `pbz_only` edges are usually unpushed mirror edits. The
engine knows nothing about reports, so it never decides what is unused.
