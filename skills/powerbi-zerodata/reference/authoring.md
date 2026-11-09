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
- `--alt "<text>"` sets the alt text of any new visual (screen readers; `pbz a11y` checks it).

## Themes, page backgrounds and images

All of these are report-only edits: `pbz validate`, then `pbz push --reload` (Desktop may stay open),
then `pbz handoff` so the user looks at the result.

**Themes.** The mirror holds the report's theme files as JSON under
`<Name>.Report/StaticResources/`: `SharedResources/BaseThemes/*.json` is Microsoft's base theme (never
edit it; Desktop replaces it) and `RegisteredResources/<theme>.json` is the custom theme (colors,
`textClasses` fonts, `visualStyles` defaults per visual type). Edit the custom theme like any mirror
file. To add one: write the file, then register it in `definition/report.json`:
`themeCollection.customTheme = {"name": "<theme>.json", "reportVersionAtImport": <copy of the base
theme's>, "type": "RegisteredResources"}` plus an item `{"name": "<theme>.json", "path": "<theme>.json",
"type": "CustomTheme"}` in the `RegisteredResources` package. The theme file's own `"name"` must be the
same `<theme>.json`. Push runs Microsoft's report validator, which checks the registration, the JSON,
value types (`dataColors` an array, positive font sizes) and the formatting object names in
`visualStyles`. Images embedded in a theme (`"url": "data:image/..."`) show as `«payload:image#n»`:
keep, move or delete the token, never type inside it.

**Page canvas and wallpaper.** `pbz style <page> --canvas` (or `--all` for every page) edits the
page's `background` (canvas) and `outspace` (wallpaper) objects:

    pbz style "Overview" --canvas --set background.color=#F3F2F1 --set wallpaper.color=theme:0
    pbz style "Overview" --canvas --set background.image=<item> --set background.scaling=fill
    pbz style "Overview" --canvas --set background.transparency=20 --unset wallpaper.image --show

Properties: `color` (`#RRGGBB` or `theme:<n>[:<shade %>]`; a measure is refused because Desktop does
not evaluate field values for page colors), `transparency` (0 opaque to 100 invisible), `image` (a
registered image item) and `scaling` (`fit`, `fill`, `normal`; default `fit`). Setting an image without
a transparency sets the transparency to 0: Desktop's default canvas transparency hides a background
image.

**Images.** An image is a file in `StaticResources/RegisteredResources/`, registered in report.json
(package `RegisteredResources`, item type `Image`, item name = path = file name) and referenced
from formatting with a `ResourcePackageItem` expression (`PackageType` 1). The mirror never holds
image files, and the guard blocks reading or copying them, because an image can show data (a
screenshot, a pasted chart). pbz handles the bytes without showing them:

- `pbz image add <file> [--name N]` reads the file inside pbz, checks the type from its header (PNG,
  JPEG, GIF, BMP or SVG; an SVG with scripts, event handlers, embedded HTML or a DTD is refused;
  10 MiB at most), stages a copy in the private store and registers the item in the mirror's
  report.json. The item name follows Desktop: the file name plus unique digits. The output shows the
  item, file kind, size and pixel size, never content. The same bytes added twice reuse the item.
- Use it: `pbz style ... --canvas --set background.image=<item>` (or `wallpaper.image`),
  `pbz add visual image --page <p> --image <item> [--fit fit|fill|normal] [--alt "..."]` (sized to the
  image's proportions, at most 320 x 200, never enlarged), or an image property of a visual such as a
  button's `fill.image` / `icon.image` (`--state default`), a card's `image.image`. `pbz style --list
  --type <t> --object <o>` shows which properties take an `image` (a registered item) or an `imageUrl`
  (a registered item or a public `https://` image URL). Objects that also have a `url` property
  (`plotArea`, `fillCustom`) are refused: Microsoft's validator rejects the nested image form there,
  so set those images in Desktop.
- `pbz image list` shows every registered image: `staged` (installed by the next push), `present`
  or `missing`, kind, size, pixels and where it is used; also `removed_on_push`, unregistered files and
  references to unregistered items.
- `pbz image remove <item>` (item or file name) unregisters an image nobody uses (`IN_USE` lists the
  users otherwise). The next push deletes the file from the report, backed up with the push.
- Push copies staged images into the staged report before validation and into the real report after
  it, deletes the files of images whose registration was dropped, and reports `images_installed` /
  `images_removed`. Deleting a visual's folder in the mirror removes the visual and its empty folder
  on push; run `pbz repair` for interactions and bookmarks that pointed at it.
- Checks (`pbz validate` and push): `PBIR_IMAGE_FLAT` (an image property written as
  `image: {name, url}` instead of `image: {image: {name, url, scaling}}`; Desktop shows nothing),
  `PBIR_IMAGE_NOT_REGISTERED`, `PBIR_IMAGE_MISSING` (no file in the report and none staged),
  `PBIR_IMAGE_NAME` (an item name that is not a plain image file name), `PBIR_IMAGE_PATH`,
  `PBIR_IMAGE_PACKAGE_TYPE`.

## Model properties: `pbz set` / `pbz get`

`pbz set <object> prop=value ...` edits typed properties of one object (`'T'[C]`, `[M]`, `Table`,
`Table.Hierarchy`), or of a selection: `pbz set --table Sales --kind measure --match "%$"
formatString=0.0%`, `--folder <display folder>`. Properties: `description` (written as `///` lines),
`displayFolder`, `formatString`, `isHidden`, `isKey`, `dataCategory`, `summarizeBy`, `sortByColumn`
(same table, no loops), `isAvailableInMdx`. `--unset prop` removes one, `--dry-run` previews, and
`pbz get <object or selection>` shows the current values. Data types, lineage tags and expressions are
not edited here. To confirm a new sort-by on real data, check that each label maps to one sort value:
`equal` with `COUNTROWS(SUMMARIZE('T', 'T'[Label], 'T'[Sort]))` against `DISTINCTCOUNT('T'[Label])`.

## Hierarchies, dynamic formats and functions

- `pbz hierarchy add <table> <name> --level Year --level Month [--names Year Month]`,
  `pbz hierarchy levels ...` (replace the levels; a level whose name survives keeps its lineage tag),
  `pbz hierarchy remove <table> <name> [--force]` (refused while visuals use it).
- `pbz format-expr "[Measure]" "<DAX returning a format string>"` sets the measure's
  formatStringDefinition, `--remove` drops it. Needs compatibility level 1601 (`--upgrade-compat`).
  It replaces the measure's static `formatString`: Desktop refuses to open a measure that has both
  ("Issues were found"), although TOM accepts it, so `pbz validate` reports `FORMAT_STRING_CONFLICT` and
  `pbz repair` drops the static one. After `--remove`, set a static format again with
  `pbz set "[Measure]" formatString=...`. The expression can return any text: try it on fake data with
  `pbz synth query`. Raising the level is a one-way change for that model; Desktop opened both upgraded
  levels (1601, 1702) in the end-to-end test.
- `pbz function add <Name> --params "(amount : NUMERIC)" --body "amount * 1.1"` writes a DAX
  user-defined function to `definition/functions.tmdl` (compatibility level 1702,
  `--upgrade-compat`). `update` changes the parameters or body, `rename --to` fixes every call site,
  `remove` refuses while something calls it, `list` shows parameters and call counts.

## Copying pages and visuals: `pbz copy`

`pbz copy page <page> --as "<name>" [--after <page>] [--independent-slicers]` duplicates a page with
new names for the page, its visuals and groups (children re-parented). Filter names stay unique across
the report. Tooltip and drillthrough pages get a new page binding and new parameter names, and each
drillthrough parameter's `boundFilter` follows the renamed filter. Interactions between copied visuals
are remapped. A button or tooltip that pointed at the copied page itself now points at the copy; links
to other pages stay. Copied slicers keep their sync group (they stay in sync with the originals)
unless `--independent-slicers`. Bookmarks are not copied: they name the source page and their state
may carry data. `pbz copy visual <page> <visual> [--to-page <page>] [--pos x,y[,w,h]]` duplicates a
visual, or a group with all its descendants, at the first free spot; interactions are not copied and a
copied grouped visual becomes top-level. Tokens are copied as tokens. Then `pbz validate` and
`pbz push --reload`.

## Accessibility and phone layout: `pbz a11y`, `pbz mobile`

`pbz a11y [--page P] [--min-font 9]` audits the mirror and reports codes and visual ids only: data
visuals without alt text, alt text that is only a mirror token or longer than 250 characters, titles
switched off, keyboard tab order that differs from reading order (top to bottom, then left to right,
groups kept together), duplicate tab orders, hidden visuals that keep a tab position, font sizes below
the threshold, WCAG contrast below 4.5:1 (3:1 for large text) where a text color and its background
are both static hex colors, and visible pages with no phone layout. Set alt text with
`pbz style <page> <visual> --set container.general.altText="what the visual shows and why"` (its
purpose, never its values). `pbz a11y --page P --fix-tab-order` rewrites the tab order in reading
order. `pbz mobile <page> <visual|all> [--pos x,y,w,h] [--remove]` writes the visual's `mobile.json`
(phone layout); without `--pos` visuals stack full width in reading order. Metadata checks cannot
certify screen-reader use or rendered text fit, so the user still checks in Desktop.

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
decides. Setting one pair leaves the other stored pairs in place. The page folder id is checked
before any result or file list is built. It must be 1–64 letters, digits, or hyphens. Anything else,
including an underscore, is `REFUSED` with detail `identifier`. A malformed page is `REFUSED` with
detail `UNUSABLE`, and the folder name is not echoed.

`pbz interaction <page> [--source <visual>] --show` lists stored modes (`filter`, `highlight`,
`none`, `default`). Output is page and visual ids only. A missing stored pair is `default`, which
means Desktop's choice. Page and visual ids on `--show` must start with a letter and then use only
letters and digits.

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
