# TMDL / PBIR / DAX / Desktop gotchas

Technical lessons only. No values.

## Desktop and files
- Desktop overwrites PBIR and TMDL when it saves, and reformats TMDL. Close it before pushing
  model changes (`pbz desktop close`); after the user saves in Desktop, `pbz pull --rebase`.
- Opening a .pbip takes minutes; batch edits, then reopen once (`pbz desktop open --wait 300`).
- A stray second Desktop instance can overwrite pushed files on its own save.
- Desktop refresh against SharePoint can fail on some networks; refresh is the user's call.
- External tools cannot refresh Power Query (M) tables inside Desktop: Desktop shows "Could not
  find a PackageSession for the given sessionID". `pbz refresh` only recalculates calculated
  tables and columns; ask the user to press Refresh in Desktop for source data.
- Publishing and service refresh are not available to agents here.
- OneDrive can briefly lock folders; retry the push.
- A custom theme survives publish only when imported through Desktop (Themes > Browse).

## TMDL
- Tab indentation. A multi-line expression starts on the line after `=` and sits two levels
  deeper than the object line; properties sit one level deeper. Long or special expressions can
  use triple-backtick blocks.
- Measure names are unique across the whole model.
- Never change existing `lineageTag`s; new objects may omit them (Desktop assigns them).
- Renaming a column changes the model name only; `sourceColumn` stays the source field name.
  Use `pbz rename` so DAX, sort-by, hierarchies, relationships and visuals all follow.
- Check `crossFilteringBehavior: bothDirections` before adding relationships; inactive
  relationships are still cardinality-checked, so prefer `TREATAS` / `USERELATIONSHIP`.

## DAX
- `REMOVEFILTERS('Fact')` / `ALL('Fact')` clears the fact's expanded table, which includes its
  dimensions (date included). Scope it to columns, or use `ALLSELECTED` for "the selected window".
  `pbz validate` flags this pattern (EXPANDED_TABLE_CLEARS_DATE).
- `CONTAINSSTRING` / `SEARCH` are case-insensitive substring matches: a short code matches inside
  longer words. Prefer exact comparisons or anchored checks (flagged as SHORT_SUBSTRING).
- `SUMMARIZECOLUMNS` drops rows where all measures are blank; empty visuals often come from
  a measure returning BLANK under the visual's filters, not from the binding.

## PBIR
- Visuals are discovered by folder (`pages/<page>/visuals/<visual>/visual.json`); the folder name
  is the visual name. Page order lives in `pages/pages.json`.
- Literals are single-quoted with `''` escaping (`'Project'`); ints end in `L`, doubles in `D`.
  The same escaping applies inside Deneb `jsonSpec` strings.
- A filter without a `Where` condition does nothing.
- Slicers: keep `responsive` false, header off, height at least 64 for range sliders.
- Hierarchy slicer `expansionStates`: the leaf level omits `identityKeys`.
- Keep each file's existing `$schema` URL; Desktop bumps versions itself.
- Deneb fields use the field's display name; layered sorts need an `op` (min/max/count).
  Validate specs structurally only; never inline data rows.
- Python visuals run on Desktop's own Python; the script cannot contain apostrophes.
