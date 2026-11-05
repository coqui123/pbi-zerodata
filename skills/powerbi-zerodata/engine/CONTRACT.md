# pbz-engine contract (v1)

`pbz-engine.exe` is the only component that talks to a live Analysis Services model (the local
instance Power BI Desktop starts). It is a **declassification broker**: it may read real data
internally, but everything it writes to stdout is built from a closed vocabulary. Python (`pbz`)
is its only caller.

## Invocation

```
pbz-engine.exe <verb>        request JSON on stdin, one response JSON object on stdout
```

- Exit code 0 whenever a JSON response was written (including `ok:false`). Non-zero only if the
  process could not write JSON at all.
- stderr: never used. All diagnostics go to the private raw log.
- No durations, timings, row counts or values in stdout unless a field below explicitly allows a
  bucket.
- Every request carries `private_dir` (absolute path, e.g. `%USERPROFILE%\.pbz-private\<ws-id>`; kept out
  of AppData because packaged hosts virtualize it). The
  engine creates `private_dir` and its subfolders if missing.

## Common response envelope

```json
{"ok": true, "verb": "health", "...": "verb fields"}
{"ok": false, "verb": "check", "class": "ENGINE_UNAVAILABLE", "log_id": "e-20261104-0007"}
```

Error classes (closed set): `SYNTAX`, `UNKNOWN_OBJECT`, `TYPE_MISMATCH`, `CONVERSION`, `CIRCULAR`,
`DUPLICATE_KEY`, `TIMEOUT`, `ENGINE_UNAVAILABLE`, `BAD_REQUEST`, `OTHER`.

Mapping is by exception type first, then English message patterns (syntax -> SYNTAX; "cannot be
found", "doesn't exist", "not found", "failed to resolve" -> UNKNOWN_OBJECT; "cannot convert",
"type mismatch", "of type" -> TYPE_MISMATCH or CONVERSION; "circular dependency" -> CIRCULAR;
"duplicate value" -> DUPLICATE_KEY; timeout -> TIMEOUT; socket/connection -> ENGINE_UNAVAILABLE).
Anything unrecognized -> OTHER.

The raw exception text is appended to `private_dir\logs\engine.log` as
`<log_id>\t<base64(DPAPI(utf8 text))>` and only the `log_id` is returned.

### `message` fields (only where listed)

Some verbs may return a `message` for metadata-level errors (DAX bind errors, measure state
errors, TMDL parse errors). Before output it is scrubbed:
1. Keep text inside `'...'` or `[...]` only if it equals a model object name (table, column,
   measure, hierarchy) known from TMSCHEMA or the TMDL being validated; else replace with `«q»`.
2. Replace text inside `"..."` with `«q»` always.
3. Replace every remaining digit run that is not part of a kept identifier with `«n»`.
4. Truncate to 300 chars.

## Buckets (shape tier only; omitted when request has `"disclosure": "strict"`)

`count_bucket`: `"0"`, `"1-9"`, `"10-99"`, `"100-999"`, `"1k-9k"`, `"10k+"`
`share_bucket` (blank share): `"none"`, `"<1%"`, `"1-10%"`, `"10-50%"`, `">50%"`
`mismatch` (groups): `"NONE"` (0), `"ONE"` (1), `"FEW"` (2-5), `"MANY"` (>5) — allowed in both tiers.

## Secrets and HMAC

- Key: `private_dir\key.bin` = DPAPI (CurrentUser, `CRYPTPROTECT_UI_FORBIDDEN`, no entropy)
  protected 32 random bytes. Create if missing. Use P/Invoke to `crypt32.dll`
  `CryptProtectData` / `CryptUnprotectData` (no extra NuGet). Python reads the same file with ctypes.
- `hmac(value)` = lowercase hex HMAC-SHA256(key, utf8(normalize(value)))
  where `normalize(s) = s.Normalize(FormC).Trim().ToLowerInvariant()` for text; for numbers use
  invariant `R` format of the double rounded to 12 significant digits; dates ISO `yyyy-MM-ddTHH:mm:ss`;
  BLANK -> the literal string `∅blank`.

## Verbs

### `ping`
Request `{"private_dir": "..."}` -> `{"ok": true, "verb": "ping", "version": "1"}`. No connection.

### `connect`
Request `{"private_dir": "...", "port": 52341}`
Response `{"ok": true, "verb": "connect", "database_id": "<guid>", "compat_level": 1600,
"tables": ["DimDate", "..."]}`. Metadata only (TMSCHEMA_TABLES / DBSCHEMA_CATALOGS).

### `health`
Request `{"private_dir", "port"}`
Response:
```json
{"ok": true, "verb": "health",
 "objects": {"tables": 12, "columns": 403, "measures": 155, "relationships": 10},
 "errors": [{"kind": "measure", "table": "T", "name": "M", "class": "UNKNOWN_OBJECT", "message": "..."}],
 "unprocessed_tables": ["T2"]}
```
Source: `$SYSTEM.TMSCHEMA_TABLES`, `TMSCHEMA_COLUMNS`, `TMSCHEMA_MEASURES`,
`TMSCHEMA_RELATIONSHIPS`, `TMSCHEMA_PARTITIONS` (`State`, `ErrorMessage`). Only `TMSCHEMA_*` and
`DBSCHEMA_CATALOGS` DMVs may ever be queried by any verb.

### `tmdl-validate`
Request `{"private_dir", "folder": "<...>\\X.SemanticModel\\definition"}` (no port).
Uses `TmdlSerializer.DeserializeDatabaseFromFolder`. Also inventory: every `ref` in
`model.tmdl` / `database.tmdl` resolves to a file; every `tables\*.tmdl` (and roles, cultures,
perspectives) is referenced.
Response `{"ok": true, "verb": "tmdl-validate", "valid": false,
"errors": [{"file": "tables/X.tmdl", "line": 34, "class": "SYNTAX", "message": "..."}],
"missing_refs": ["tables/Y.tmdl"], "orphan_files": ["tables/Z.tmdl"]}`. Never echo line text.

### `refresh`
Request `{"private_dir", "port", "tables": ["T"]?}` (all tables when omitted). TMSL/TOM full
refresh. Response `{"ok": true, "verb": "refresh", "status": "PASS"}` or class on failure.

### `compiles`
Request `{"private_dir", "port", "items": [{"id": "m1", "table": "T", "expression": "<DAX scalar>"}]}`
Bind-check without returning values, e.g.
`DEFINE MEASURE 'T'[__pbz_c] = <expr> EVALUATE ROW("x", IF(FALSE(), [__pbz_c]))`
(spike the cheapest form that still surfaces bind errors). Response
`{"items": [{"id": "m1", "status": "PASS"}, {"id": "m2", "status": "ERROR", "class": "UNKNOWN_OBJECT", "message": "..."}]}`.

### `index`
Request `{"private_dir", "port", "category_max": 50, "value_cap": 50000, "disclosure": "shape"}`
For every column: type; for text columns distinct count (internal) -> `category` when
`<= category_max`, else `high`; `empty` when no non-blank rows. Writes:
- `private_dir\index.json`: `{"version": 1, "columns": {"T[C]": {"type": "string", "class": "high", "hmacs": ["..."]}}}`
  (HMACs for every distinct non-blank value of `high` text columns, capped at `value_cap`; numeric
  and date columns: no hmacs).
- `private_dir\categories.bin`: DPAPI-protected UTF-8 JSON `{"T[C]": ["Active", "Closed"]}` for
  `category` columns.
Response (no values):
```json
{"ok": true, "verb": "index", "tables": [{"table": "T", "rows_bucket": "100-999",
  "columns": [{"column": "C", "type": "string", "class": "category", "card_bucket": "1-9", "blank_bucket": "none"},
              {"column": "D", "type": "dateTime", "year_min": 2023, "year_max": 2028, "blank_bucket": "<1%"}]}]}
```
`year_min`/`year_max` and buckets only in shape tier.

### `check`
Request:
```json
{"private_dir", "port", "disclosure": "shape",
 "reveal": ["DimDate[Year]", "Project Info[Status]"],
 "checks": [
   {"id": "c1", "kind": "nonblank", "expr": "[Total Hours]", "where": ["'Project Info'[Status] = \"Active\""]},
   {"id": "c2", "kind": "equal", "left": "[A]", "right": "[B] + [C]", "by": ["DimDate[Year]"], "tol": 1e-9},
   {"id": "c3", "kind": "unique", "column": "'Projects'[ProjectID]"},
   {"id": "c4", "kind": "no_orphans", "from": "'Timesheet'[Project ID]", "to": "'Projects'[ProjectID]"}
 ]}
```
Kinds the engine implements: `executes`, `nonblank`, `nonempty` (table expr), `equal`
(`left`, `right`, optional `by`, `where`, `tol` relative, BLANK equals BLANK only), `additive`
(`expr`, `by`: total equals sum over `by` groups), `unique` (`column`), `no_orphans` (`from`, `to`),
`no_blank_keys` (`column`), `sign` (`expr`), `subset` (`column`, `of`).
Python rewrites `differs`, `filter_independent` and `compiles` into these before calling.
`where` items are boolean single-column filter predicates applied with CALCULATE/CALCULATETABLE.
Response per check:
```json
{"id": "c2", "status": "FAIL", "mismatch": "FEW",
 "groups": [{"DimDate[Year]": {"reveal": "2026"}}, {"Resource[Name]": {"hmac": "9f2c..."}}]}
```
- `status`: `PASS`, `FAIL`, `EMPTY` (no rows/groups to compare), `UNSUPPORTED`, `ERROR` (+ `class`, `log_id`).
- `groups`: at most 10 failing groups. A key is revealed only if its column is in `reveal`;
  otherwise `hmac` (Python turns it into a token). Strict tier: no `groups`, still `mismatch`.
- `sign` adds `"sign": "NEG|ZERO|POS|BLANK"`. `unique`/`no_orphans`/`no_blank_keys` add
  `"offenders_bucket"` in shape tier.

### `probe`
Request `{"private_dir", "port", "table": "T", "columns": ["C"]?, "disclosure": "shape"}`
Response `{"table": "T", "rows_bucket": "...", "columns": [{"column": "C", "type": "...",
"has_blanks": true, "blank_bucket": "1-10%", "card_bucket": "10-99", "has_negatives": false,
"has_future_dates": false, "year_min": 2023, "year_max": 2028}]}` (booleans in both tiers,
buckets and years only in shape tier).

### `snapshot-save` / `snapshot-compare`
Request `{"private_dir", "port", "name": "before-refactor", "queries": [{"id": "q1",
"dax": "EVALUATE SUMMARIZECOLUMNS(DimDate[Year], \"m\", [Total Hours])", "keys": ["DimDate[Year]"]}]}`
- Save: run each query, store in `private_dir\snapshots\<name>.json` per query: map
  `hmac(key tuple)` -> list of per-column `hmac(cell)`; no keys -> multiset of row hmacs. Also
  store `refreshed`: max `RefreshedTime` over TMSCHEMA_PARTITIONS (stored only, never printed).
- Compare: rerun, respond per query `{"id": "q1", "result": "SAME"}` or
  `{"id": "q1", "result": "DIFF", "columns": ["m"], "mismatch": "ONE"}`; `NEW` (no saved query),
  `MISSING` (saved query not in request), `STALE_DATA` (model refreshed since save).

### `smoke`
Request `{"private_dir", "port", "visuals": [{"id": "page/visual", "dax": "EVALUATE TOPN(1, ...)"}]}`
Response `{"visuals": [{"id": "page/visual", "status": "OK|EMPTY|ERROR", "class": "..."}]}`.

## Safety rules for the implementation

- Never print engine strings except scrubbed `message` fields above.
- Never run DMVs other than `TMSCHEMA_*` / `DBSCHEMA_CATALOGS` (storage DMVs leak counts).
- Default per-command timeout 60 s -> `TIMEOUT`.
- Before any data query on `port`, `connect` logic must succeed; callers pass the same port.
- Tests use only the synthetic fixture model. Canary strings/numbers planted there must never
  appear in stdout, stderr, or any file outside `private_dir` (where they may appear only inside
  DPAPI blobs).

## v1.1 additions

Agreed after v1 (coordinator addendum and the Desktop refresh finding). Implemented by the engine.

1. **`index` word HMACs.** For text columns whose *column name* matches
   `(?i)(name|owner|manager|\bpm\b|leader|lead|supervisor|resource|person|employee|staff|member|assignee|requester|approver|sponsor|contact|user)`,
   index.json also stores, under that column, `"word_hmacs": [...]`: `hmac()` of every
   whitespace-separated word of every distinct value (the same distinct values the column class
   uses: up to `category_max` for category columns, up to `value_cap` for high columns), after
   stripping non-letters at both ends, keeping only words with at least 3 letters. Same key and
   normalization as `hmac()`. Purpose: detect partial names (a lone last name) in free text.
2. **`check` group keys.** `by` and `reveal` entries are DAX column references exactly as the caller
   wrote them (`"'Project Info'[Status]"` or `"DimDate[Year]"`). Each failing group's dict is keyed by
   the exact `by` string, and a key is revealed only when that exact string appears in `reveal`.
3. **`pbz/engine.py`** keeps the signature `call(verb, request, timeout=120) -> dict`; the Python side
   passes `private_dir` (and `port` for live verbs) itself in every request.
4. **`refresh` never refreshes data.** Power BI Desktop refuses external TOM/TMSL refreshes of M
   partitions (its Power Query session is not available to external clients and it shows a modal
   error). Data refresh is the user's job via Desktop's Refresh button.
   - No `tables`: TMSL `refresh` type `calculate` on the database (recalculation of calculated
     tables/columns where needed; no data source is touched).
   - With `tables`: every listed table must consist only of calculated (DAX) or calculation-group
     partitions; those get a forced DAX recalculation. If any listed table has an M (or other
     data-source) partition the response is `{"ok": true, "verb": "refresh", "status": "UNSUPPORTED"}`
     and nothing is attempted.
   - Response `status` is `PASS` or `UNSUPPORTED`; engine failures use the error envelope.

## v1.2 addition: `deps`

Request `{"private_dir", "port"}`. Response `{"ok": true, "verb": "deps", "edges": [[type, table, object,
ref_type, ref_table, ref_object], ...]}` from `$SYSTEM.DISCOVER_CALC_DEPENDENCY`, the one DMV besides
`TMSCHEMA_*` / `DBSCHEMA_CATALOGS` the engine may read, and only with the fixed column list
`OBJECT_TYPE, TABLE, OBJECT, REFERENCED_OBJECT_TYPE, REFERENCED_TABLE, REFERENCED_OBJECT`. The
`EXPRESSION`, `REFERENCED_EXPRESSION` and `QUERY` columns (formula text) are never selected. Each name
must equal a TMSCHEMA object name (table, column, measure, hierarchy, partition, relationship) or it
becomes `"?"`; each type must match `^[A-Z_]{1,40}$` or it becomes `"?"`. Duplicate rows are dropped.
Used by `pbz unused --verify-engine` to cross-check the metadata parser; never the primary source.

Implementation clarifications that callers must know (response container names, the blank group
key, optional `timeout_s`, stricter message scrubbing, query guards) are listed in
`engine/README.md` under "Contract deviations and clarifications".
