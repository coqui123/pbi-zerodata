# Refactoring and authoring verbs

All of these edit the sanitized mirror only. Review with `pbz diff`, check with `pbz validate`
(TMDL lint, field references, built-in PBIR integrity checks), then `pbz push`, which stages the
real files and validates them again with TOM before writing. None of them reads values.

## Renames and moves

- `pbz rename table <old> <new>` renames the table declaration and its file, `ref table`, the
  partition when it carries the table's name, every DAX reference (`'T'[x]`, `T[x]`, `'T'`, bare `T`),
  M references to the table's query (`#"T"`), relationship endpoints, RLS `tablePermission`,
  perspectives, the `PBI_QueryOrder` annotation, and in PBIR every `Entity`, `From` alias entity,
  report-level measure entity and `queryRef` / `metadata` string. Tokens in the mirror keep their old
  label until the next pull; that is cosmetic. Refused when the table has translations in `cultures/`
  (not mirrored) or when a shared query has the same name.
- `pbz move measure <name> --to <table>` moves the TMDL block (description, format string, KPI,
  annotations) into the target table, rewrites `'Old'[Measure]` references and the PBIR entity of every
  visual, filter, sort and bookmark reference. A filter whose single `From` alias also serves other
  fields of the old table is listed under `manual` instead of being edited.
- `pbz rename measure|column` is unchanged.

## Scaffolds (`pbz new`)

| Kind | What it writes |
|---|---|
| `measure-table <name>` | a calculated table `ROW("Value", 0)` with its only column hidden; move measures into it |
| `date-table <name> --start Y --end Y [--relate 'T'[C]]` | calculated `CALENDAR` table with Year, Quarter, Month (sorted by Month Number), Year Month, Weekday, a Calendar hierarchy, `dataCategory: Time` and `isKey` on Date. Years are explicit (never derived from data). Refused when a marked date table exists |
| `calc-group <name> --date 'Date'[Date] [--items ...] [--precedence N]` | calculation group with time-intelligence items (Current, MTD, QTD, YTD, PY, PY YTD, YoY, YoY %), ordinals, a dynamic format for YoY %, and `discourageImplicitMeasures` on the model |
| `field-parameter <name> --fields [M] 'T'[C] ... [--labels ...]` | the three-column `NAMEOF` table with `ParameterMetadata`, `relatedColumnDetails` and sort-by order, the way Desktop writes it |

Each new object gets a fresh `lineageTag`; the table is added to `model.tmdl`.

## Relationships (`pbz relationship`)

`add <many 'T'[C]> <one 'U'[K]> [--inactive] [--both] [--many-to-many] [--force]` refuses a second
relationship on the same columns (`DUPLICATE`), mismatched data types (`TYPE_MISMATCH`, override with
`--force`) and a second active filter path between the two tables (`AMBIGUOUS_PATH`; add it
`--inactive` and use `USERELATIONSHIP`). `set ... --active|--inactive|--both|--single` changes an
existing one with the same path guard; `remove` deletes it. Key uniqueness and orphans are data
questions: check them with `pbz check` (`unique`, `no_orphans`) while Desktop is open.

## Pages and visuals (`pbz add`)

- `pbz add page "<name>" [--after <page>] [--hidden] [--width --height]` creates the page folder with
  a fresh 20-character id and inserts it into `pages.json`.
- `pbz add visual <type> --page <page> --field <ref> ... [--pos x,y,w,h] [--title] [--text]` builds
  `visual.json` with the query state for the type. Types: `card`, `multiRowCard`, `tableEx`,
  `pivotTable` (Rows, Columns, Values), `slicer`, `textbox`, and charts (Category, Y, Series):
  `clusteredColumnChart`, `clusteredBarChart`, `columnChart`, `barChart`, `lineChart`, `areaChart`,
  `pieChart`, `donutChart`, `scatterChart`, `waterfallChart`, `funnel`, `lineClusteredColumnComboChart`.
  A field without a role goes to the type's default (measures and numeric columns to values, other
  columns to categories); `ROLE='T'[C]` picks one. Numeric columns in value roles become a Sum
  aggregation. Without `--pos` the visual goes to the first free spot on the page
  (`placement: NO_FREE_SPACE` when the page is full). The `$schema` versions follow the report's files.
  New visuals carry no filters, selections or value-specific formatting.

## Seeing a page: `pbz layout`

Prints each page's visuals (label, id, type, box, z, fields per role) and, with `--page`, a text canvas
map with one labelled box per visible visual. Lint: `OVERLAP` (two data visuals covering at least 15%
of the smaller one; shapes, images, buttons and text boxes are ignored), `OFF_CANVAS`, `TINY`,
`ZERO_SIZE`, `DUPLICATE_Z`, `DUPLICATE_TAB_ORDER`.

## Formatting: `pbz format`

Formats measures, calculated columns and calculation items: functions and keywords uppercase, one
space around operators and after commas, `VAR` / `RETURN` on their own lines, and calls longer than 90
characters broken one argument per line (4-space indents under the TMDL tab indent). The result must
lex to exactly the same tokens (functions and keywords compared without case), so mirror tokens and
literals cannot change; expressions with comments or ``` blocks are skipped. Check by default,
`--apply` to write. Nothing leaves the machine.

## Validation and repair

`pbz validate` and every push run built-in PBIR integrity checks (no external validator needed):
dataset reference, `report.json`, page order and active page, page and visual names equal to their
folders, positions, groups, visual interactions, duplicate page bindings, report-page tooltips,
projections without `queryRef`, sync groups binding different fields, bookmark files and lists, stale
bookmark targets, duplicate report-level measures; plus TMDL lint including copied lineageTags.

`pbz repair [--dry-run]` fixes the structural ones without deleting a page or visual; `--fields` also
drops projections on fields the model no longer has; `--lineage` gives objects that copy another
object's lineageTag a fresh one (the first keeps it).

## Property fixes: `pbz bpa --fix`

`HIDE_FOREIGN_KEYS` (isHidden), `NUMERIC_COLUMN_SUMMARIZE_BY` (summarizeBy: none),
`ISAVAILABLEINMDX_FALSE_NONATTRIBUTE_COLUMNS` (isAvailableInMdx: false) and
`DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE` (dataCategory: Time plus isKey on the date
column; confirm uniqueness with `pbz check`). Rules that need judgement (DAX rewrites, format strings,
relationship direction) are never auto-fixed. Objects with a Tabular Editor ignore annotation are
skipped.

## Runtime dependencies

None beyond Python and, for engine verbs, the bundled pbz-engine. Three-way merge (`pull --rebase`)
is built in (no git), PowerShell 7 falls back to Windows PowerShell, and Microsoft's npm report
validator and Desktop bridge are optional extras. No Measure Killer, Tabular Editor, DAX Studio or
Bravo binary is ever called.
