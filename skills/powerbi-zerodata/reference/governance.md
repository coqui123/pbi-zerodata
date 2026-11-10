# Translations, perspectives, roles, incremental refresh, selective push

All of these edit the sanitized mirror; review with `pbz diff` / `pbz validate`, apply with
`pbz push` (TOM validates the staged model, so a dangling reference is refused). Output is names and
counts only.

## Cultures and translations

Culture files (`cultures/<code>.tmdl`) are mirrored. The Q&A linguistic schema in them (synonyms,
phrasings) can be data-derived, so it is one opaque payload token (`«payload:linguisticMetadata#n»`):
keep it as it is. Translated captions, descriptions and display folders are visible and screened as
free text like any description.

| Command | Does |
|---|---|
| `pbz culture add <code>` / `remove <code>` | creates or deletes `cultures/<code>.tmdl` and its `ref cultureInfo` line |
| `pbz culture list` | cultures, caption counts, whether a linguistic schema exists |
| `pbz culture missing <code>` | visible tables, columns, measures and hierarchies with no caption |
| `pbz translate <code> <object> [--caption] [--description] [--folder]` | sets one object's translation (creates the path in the file) |
| `pbz translate --export <file>` / `--import <file>` | round-trips a flat `{version, translations[]}` file. Stdout is counts. A value that matches a mirror token is refused. The destination must be a regular file |

`rename`, `move measure` and `unused --remove` keep translations in step: renamed objects are renamed
in every culture, a moved measure's translation moves to its new table, removed objects lose their
translation blocks. (Mirrors pulled before cultures were mirrored still treat translated objects as
manual; `pbz pull` once to bring the cultures in.)

## Perspectives

`pbz perspective add <name>` / `remove <name>`; `pbz perspective include <name> <objects...>` and
`exclude <name> <objects...>` with `Table` (all its visible columns, measures and hierarchies),
`'T'[Column]`, `[Measure]` or `T.Hierarchy`. `rename` and `move measure` keep perspective membership
in step.

## Security roles

| Command | Does |
|---|---|
| `pbz role add <name> [--permission read]` / `remove <name>` | role file plus `ref role` line |
| `pbz role filter <name> <table> "<DAX>"` | sets (or replaces) the table's row filter; every reference is resolved first (`UNKNOWN_REF` otherwise) |
| `pbz role unfilter <name> <table>` | drops the filter |
| `pbz role member-add <name> <member>` / `member-remove <name> <member or token>` | members are personal data: they appear as `«pii#n»` tokens after the next pull; remove one by its token |
| `pbz role ols <name> <Table or 'T'[C]> none\|read\|default` | object-level security: `metadataPermission` on the table permission or a `columnPermission` (none hides the object from the role; default removes the rule) |

Effective security is a data question: ask the user to test with View as role in Desktop.

## Incremental refresh

`pbz refresh-policy set <table> --date <dateTime column> --store N [--store-unit year] --refresh N
[--refresh-unit day] [--detect <column>] [--complete] [--hybrid]`:

- creates the `RangeStart` / `RangeEnd` DateTime parameters when missing (a small default window, so
  Desktop stays light);
- appends `#"pbz Incremental Filter" = Table.SelectRows(<last step>, each [Date] >= RangeStart and
  [Date] < RangeEnd)` to the table's query (skipped when the query already filters on them);
- writes the `refreshPolicy` block: archive and incremental windows, `incrementalPeriodsOffset: -1`
  with `--complete`, `mode: hybrid` with `--hybrid`, and with `--detect` a polling expression that
  returns the maximum of that column.

Status is `CONFIGURED_NOT_REFRESH_VERIFIED`: partitions appear when the service refreshes after
publish, and query folding cannot be proven offline. `pbz refresh-policy show` lists policies;
`remove` drops the block (the filter step and parameters stay).

## Selective push and revert

- `pbz push --only <paths...>` pushes only the matching mirror files (globs such as `*/cultures/*`
  or folder prefixes); everything else stays pending and is listed as `held_back`. The staged
  validation sees the real files plus only those changes, so a dependency on a held-back file fails
  validation instead of slipping through.
- `pbz revert [<paths...>]` puts mirror files back to their last pulled or pushed text (deleting
  added files); `pbz revert --object "measure 'Table'[Name]"` restores one object's block (also
  column, hierarchy, partition, calculationItem).
