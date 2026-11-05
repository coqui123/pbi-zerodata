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
  - `--tooltip`: a report-page tooltip (hidden, 320x240, actual size, Tooltip page binding). Point a
    visual at it with `pbz link <page> <visual> --tooltip "<tooltip page>"`.
  - `--drillthrough "'T'[C]"` (repeatable): a drillthrough target. pbz writes the Drillthrough page
    binding with one parameter per column, the bound page filters (`howCreated: Drillthrough`) and a
    Back button, as Microsoft's validator expects. A button elsewhere opens it with
    `pbz link <page> <button> --drillthrough "<drillthrough page>"`.
- `pbz add visual <type> --page <page> --field <ref> ... [--pos x,y,w,h] [--title] [--text]` builds
  `visual.json` with the query state for the type. Types: every built-in type in Microsoft's core
  visual capabilities (cards, `kpi`, `gauge`, tables, slicers, charts, maps, `textbox`, shapes,
  buttons; `pbz visual roles <type>` lists a type's roles) and registered custom visuals with a known
  contract ([visuals.md](visuals.md)). A field without a role goes to the first role that fits
  (measures and numeric columns to measure roles, other columns to grouping roles); `ROLE='T'[C]`
  picks one. Numeric columns in measure roles become a Sum aggregation. Field counts must fit one of
  the type's data view mapping conditions (a card takes one field). Without `--pos` the visual goes
  to the first free spot on the page (`placement: NO_FREE_SPACE` when the page is full). The
  `$schema` versions follow the report's files. New visuals carry no filters, selections or
  value-specific formatting.
- `pbz style` formats visuals (titles, data labels, colors, fonts, axes, legends, borders, one
  series, button states), one at a time or every visual of a type: [style.md](style.md).

## Button actions and tooltips: `pbz link`

`pbz link <page> <visual> --to-page <page> | --back | --bookmark <name> | --drillthrough <page> |
--url <https://...> | --clear` sets the action of a button, shape or image (the container's
`visualLink`: show, type, and the target page id or bookmark name). `--tooltip <page>` makes any
visual show a tooltip page on hover (`visualTooltip` type Canvas). Targets must exist: drillthrough
targets must be drillthrough pages, tooltip targets tooltip pages. Validate flags dangling targets
(`PBIR_LINK_TARGET_MISSING`) and drillthrough links to plain pages (`PBIR_LINK_NOT_DRILLTHROUGH`).

## Filters: `pbz filter`

```
pbz filter add "'Customer'[Segment]" --page <p> --in Retail Corporate        # basic (Categorical, In)
pbz filter add "'Sales'[Region]" --page <p> --not-in North                    # Not In
pbz filter add "'Date'[Year]" --report --where ">= 2020" --where "<= 2030"    # advanced, AND
pbz filter add "'Customer'[Name]" --page <p> --visual <v> --top 5 --by "[Total Sales]" [--bottom]
pbz filter add "[Total Sales]" --page <p> --visual <v> --where "> 0"         # measure: visual level only
pbz filter add "'Sales'[Comment]" --page <p>                                  # the field, no condition
pbz filter list [--report | --page <p> [--visual <v>]]                        # every scope without a target
pbz filter remove <filter name or field> --page <p>
```

Values are category labels, values you type (typed by the column: whole numbers `L`, decimals `D` /
`M`, dates `datetime'...'`, text quoted), or mirror tokens (`«T[C]#n»` stands for the whole literal
and becomes the real value on push; an invented token fails push with `UNKNOWN_TOKEN`). Filters use
From aliases in `Where`, names unique across the report, and `howCreated: User`, matching what Desktop
writes. Drillthrough filters belong to their page and are not removed here.

## Bookmarks: `pbz bookmark`

`pbz bookmark add "<name>" --page <p> [--hide v ...] [--show v ...] [--only v ...]` captures a page
and which of its visuals are shown or hidden (every other visual keeps its saved visibility). It never
captures data state: `suppressData` is set, so applying it keeps the reader's filters and slicers.
`--only` limits it to some visuals. Point a button at it with `pbz link <page> <button> --bookmark
"<name>"`. `pbz bookmark list`, `pbz bookmark remove <name>` (validate then flags buttons that still
point at it). Bookmarks that capture filters or slicer selections are made in Desktop.

## Interactions: `pbz interaction`

`pbz interaction <page> <source> <target|all> filter|highlight|none|default` writes the page's
`visualInteractions` (DataFilter, HighlightFilter, NoFilter); `default` removes the entry so Desktop
decides.

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
