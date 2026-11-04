# Canary fixture: planted values

Everything in this fixture is synthetic. These strings and numbers must never appear in engine
stdout/stderr, in any file under the skill folder outside `tests/fixture/`, or in the private store
except inside DPAPI blobs (`key.bin`, `categories.bin`, `logs/engine.log` payloads).

`tests/test_engine.py` reads the fenced blocks below (by their info string), so keep one value per
line. Test code never contains these literals itself.

## Model layout (why most tables are calculated)

Power BI Desktop refuses external (TOM/TMSL) refreshes of M partitions (its Power Query session is
not available to external clients; it raises a modal error). The engine therefore never refreshes
M partitions, and every table the tests read is a **calculated table** (DATATABLE / SELECTCOLUMNS /
CALENDAR) that Desktop computes when it opens the PBIP, with no mashup and no cache.abf:

| Table | Partition | Notes |
|---|---|---|
| `Person` | calculated `DATATABLE` | names + emails |
| `Project` | calculated `DATATABLE` | ids, titles, Status category, PlannedEnd |
| `Hours` | calculated `SELECTCOLUMNS(DATATABLE(...))` | fact; a blank person via `IF([p] = "", BLANK(), [p])`; measures |
| `DimDate` | calculated `CALENDAR(2025-01-01, 2026-12-31)` + Year / Month Number columns | |
| `ExceptionType` | calculated `DATATABLE` (triple-backtick TMDL block) | island table |
| `Notes` | **the only M partition**: an M `#table` literal combined with an Enter Data deflate blob | tokenizer coverage only; stays empty unless the user presses Refresh in Desktop; no test relies on its rows |

## Where each canary lives

| Canary | Location (source) |
|---|---|
| Person names `CANARY-PERSON-7f3a1..4` | `Person` DATATABLE; `Hours` DATATABLE; measure `[Canary Person Hours]`; RLS filter; slicer selection; bookmark |
| Emails `canary.7f3a1..3@example.invalid` (7f3a2 twice = planted duplicate) | `Person` DATATABLE |
| Project ids `918273..918276` | `Project` and `Hours` DATATABLE |
| Orphan project ids `555001`, `555002` (not in `Project`) | `Hours` DATATABLE |
| Project titles `CANARY-PROJ-918273-ALPHA` ... | `Project` DATATABLE; page In-filter (`page.json`) |
| Hours values `918273.64`, `7341.25`, ... (one negative) | `Hours` DATATABLE |
| DATATABLE rows `CANARY-DT-01..03`, `CANARY-DT-LABEL-*` | `ExceptionType` calculated table |
| M `#table` literal `CANARY-SEED-NOTE-6d2e1` | `Notes` M partition (never loaded by the engine) |
| Enter Data rows `CANARY-ENTER-DATA-5b9d1/2` | `Notes` M partition, deflate+base64 blob (not visible as plain text) |
| RLS member `canary.user@example.invalid`, filter literal `CANARY-PERSON-7f3a2` | `roles/Canary Reader.tmdl` |
| Text box `CANARY 918273.64` | text box visual `e9a4b1c7d3f8260e1b7a` |
| Slicer state `CANARY-PERSON-7f3a1` | slicer visual `c6d2a8f4e1b7039a5d6e` |
| Bookmark slicer state `CANARY-PERSON-7f3a2` | `bookmarks/Bookmark3f7a9c1e5d2b8a40c6e1.bookmark.json` |
| Broken measure column `CANARY_NOCOL_4411` | measure `Hours[Broken Measure]` (intentionally broken: expected `health` error) |

Planted data-quality facts the checks rely on: `Person[Email]` has a duplicate; `Hours[ProjectID]`
has two orphan values; `Hours[Person]` has one blank; `Hours[Hours]` has one negative value;
`Project[PlannedEnd]` has one date in 2031 (future); `Project[Status]` is a 3-value category
(Active, Closed, On Hold). Status values, years and table/column/measure names are not canaries.

## Planted strings (must exist in the fixture sources)

```canary-strings
CANARY-PERSON-7f3a1
CANARY-PERSON-7f3a2
CANARY-PERSON-7f3a3
CANARY-PERSON-7f3a4
canary.7f3a1@example.invalid
canary.7f3a2@example.invalid
canary.7f3a3@example.invalid
CANARY-PROJ-918273-ALPHA
CANARY-PROJ-918274-BRAVO
CANARY-PROJ-918275-CHARLIE
CANARY-PROJ-918276-DELTA
CANARY-DT-01
CANARY-DT-02
CANARY-DT-03
CANARY-DT-LABEL-ALPHA
CANARY-DT-LABEL-BRAVO
CANARY-DT-LABEL-CHARLIE
CANARY-SEED-NOTE-6d2e1
canary.user@example.invalid
CANARY 918273.64
CANARY_NOCOL_4411
```

Hidden in the Enter Data blob (decoded by the test, never plain text in the sources):

```canary-blob-strings
CANARY-ENTER-DATA-5b9d1
CANARY-ENTER-DATA-5b9d2
```

## Planted numbers (must exist in the fixture sources)

```canary-numbers
918273.64
918273
918274
918275
918276
555001
555002
7341.25
4417.5
2903.75
1288.125
655.5
3141.59
2718.28
1618.03
-42.75
```

## Derived numbers (measure results; never in any file)

`[Total Hours]`, `[Active Hours]`, `[Canary Person Hours]` at the grand total.

```canary-derived-numbers
942314.915
925554.545
925572.14
```

## Request canaries (used only inside test requests to force errors)

```canary-request-strings
CANARY-ERRFN-0a1b
CANARY-CONV-PERSON-7f3a9
CANARY-COL-77
CANARY-SYN-5150
CANARY-TMDLBAD-91
CANARY-TMDLSTR-92
CANARY-TMDLIND-93
CANARY-SNAPERR-1
CANARY-SMOKE-2
```

## Partial-leak stems (scanned case-insensitively)

```canary-stems
CANARY-PERSON-
CANARY-PROJ-
CANARY-DT-
CANARY-SEED-
CANARY-ENTER-DATA-
CANARY-ERRFN-
CANARY-CONV-
CANARY-COL-
CANARY-SYN-
CANARY-TMDL
CANARY-SNAPERR-
CANARY-SMOKE-
CANARY_NOCOL_
canary.7f3a
canary.user@
```

Note: the bare number `918273` is also used as synthetic data by `tests/test_mirror.py`; the
file scan therefore checks it only inside the longer canaries (`918273.64`, `CANARY-PROJ-918273-ALPHA`).
Engine stdout/stderr and private-store files are still scanned for the bare number.
