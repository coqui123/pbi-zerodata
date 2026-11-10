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
`DBSCHEMA_CATALOGS` DMVs may ever be queried by any verb, except the fixed-column reads of `deps` (v1.2) and
`memory` (v1.4).

### `provenance`
Request `{"private_dir", "port", "max_age_hours": 24}`. `max_age_hours` is 1 to 8760.
Response `{"ok": true, "verb": "provenance", "refresh": "FRESH|STALE|UNKNOWN",
"source_publication": "UNKNOWN", "coverage": "PARTIAL|COMPLETE",
"note": "Refresh does not certify source publication. Raw timestamps remain in protected storage."}`.
Partition refresh times stay in `private_dir\refresh-provenance.bin`. They are never printed.

### `tmdl-validate`
Request `{"private_dir", "folder": "<...>\\X.SemanticModel\\definition"}` (no port).
Uses `TmdlSerializer.DeserializeDatabaseFromFolder`. Also inventory: every `ref` in
`model.tmdl` / `database.tmdl` resolves to a file; every `tables\*.tmdl` (and roles, cultures,
perspectives) is referenced.
Response `{"ok": true, "verb": "tmdl-validate", "valid": false,
"errors": [{"file": "tables/X.tmdl", "line": 34, "class": "SYNTAX", "message": "..."}],
"missing_refs": ["tables/Y.tmdl"], "orphan_files": ["tables/Z.tmdl"]}`. Never echo line text.

### `refresh`
Request `{"private_dir", "port", "tables": ["T"]?}` (all tables when omitted). TMSL/TOM refresh
as in v1.1 (calculate, or full on calculated partitions only). Response
`{"ok": true, "verb": "refresh", "status": "PASS"}` or `UNSUPPORTED`. When a ProgressReportEnd
trace is actually captured, PASS may also include
`objects`: `[{"name": "Sales", "duration_band": "LT_100MS"}]`. `duration_band` is
`LT_100MS`, `100_500MS`, `500MS_2S`, `2_10S` or `GT_10S`. Names are known table, partition or
database names. Raw durations stay in the process. If the trace cannot start, `objects` is
omitted and the response stays the old shape. A trace failure does not fail the refresh.

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
`no_blank_keys` (`column`), `sign` (`expr`), `subset` (`column`, `of`), `rls`.
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
- `rls` request fields: `expect` (`EMPTY`, `NON_EMPTY`, `LESS`, `EQUAL` or `OLS_INACCESSIBLE`),
  at least one of `role` and `user` (neither may contain `;`, `'` or `"`), and one of `query`
  (a DAX query, or a table expression), `column` or `table`. The engine compares row counts
  internally. `EMPTY` / `NON_EMPTY` look at the role-scoped count; `LESS` / `EQUAL` compare it
  with the unrestricted count. `OLS_INACCESSIBLE`: unrestricted query succeeds and the role
  query fails as an unknown object → `PASS`; the role query succeeds → `FAIL`; a connection
  failure, timeout, bad request, or any other role-side error → `ERROR`. Status stays `PASS`,
  `FAIL`, `EMPTY`, `UNSUPPORTED` or `ERROR`. The response does not add `role`, `query`, `user`,
  the count or the error text.

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
- Never run DMVs other than `TMSCHEMA_*` / `DBSCHEMA_CATALOGS` (storage DMVs leak counts). The only exceptions are
  the fixed-column reads of `deps` (v1.2) and `memory` (v1.4). `memory` may also try one separate
  `COLUMN_ENCODING` read of `DISCOVER_STORAGE_TABLE_COLUMNS` that is not on the `StorageDmv` allowlist;
  if that query fails, `encoding` is omitted. `memory` keeps every size and count inside the process
  and prints fixed bands only.
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
   - On `PASS` only, and only when a ProgressReportEnd trace was captured, optional `objects`:
     `[{"name", "duration_band"}]` for known tables, partitions and the database. Trace setup
     failure omits `objects` and does not fail the refresh. M partitions stay `UNSUPPORTED`.

## v1.2 addition: `deps`

### `deps`

Request `{"private_dir", "port"}`. Response `{"ok": true, "verb": "deps", "edges": [[type, table, object,
ref_type, ref_table, ref_object], ...]}` from `$SYSTEM.DISCOVER_CALC_DEPENDENCY`, the one DMV besides
`TMSCHEMA_*` / `DBSCHEMA_CATALOGS` the engine may read, and only with the fixed column list
`OBJECT_TYPE, TABLE, OBJECT, REFERENCED_OBJECT_TYPE, REFERENCED_TABLE, REFERENCED_OBJECT`. The
`EXPRESSION`, `REFERENCED_EXPRESSION` and `QUERY` columns (formula text) are never selected. Each name
must equal a TMSCHEMA object name (table, column, measure, hierarchy, partition, relationship) or it
becomes `"?"`; each type must match `^[A-Z_]{1,40}$` or it becomes `"?"`. Duplicate rows are dropped.
Used by `pbz unused --verify-engine` to cross-check the metadata parser; never the primary source.

## v1.3 addition: `synth-query`

### `synth-query`

Request `{"private_dir", "port", "dax": "EVALUATE ...", "max_rows": 500}`. Response `{"ok": true,
"verb": "synth-query", "columns": ["..."], "rows": [[...]], "truncated": false}`: the only verb that
returns values, and only for a model built by `pbz synth build` (fake data from the sanitized mirror).
Before evaluating, the engine requires (a) that every partition of every user table is calculated or a
calculation group (TMSCHEMA_PARTITIONS `Type` 2 or 7), and (b) that the model annotation `PBZ_SYNTHETIC`
(TMSCHEMA_ANNOTATIONS, object type Model) equals `hmac("pbz-synthetic:" + ",".join(sorted lineage tags))`
over the non-private tables (TMSCHEMA_TABLES `LineageTag`; `IsPrivate`, `ShowAsVariationsOnly` and
`LocalDateTable_*` / `DateTableTemplate_*` excluded), with the key in `private_dir`. Lineage tags are
regenerated for each synthetic build and `pbz push` refuses the annotation (SYNTHETIC_MARKER_IN_REAL_MODEL),
so the marker cannot be moved onto a real model. Otherwise `BAD_REQUEST` and no rows. The query must pass
the same guard as snapshot / smoke (DEFINE or EVALUATE first, no INFO.* or EVALUATEANDLOG). Rows are capped at
500; DateTime values are `yyyy-MM-ddTHH:mm:ss` strings.

## v1.4 addition: `memory`

### `memory`

Request `{"private_dir", "port", "disclosure"?, "predicates"?: true}` (`predicates` must be a boolean). VertiPaq
storage attributed to model objects the way VertiPaq Analyzer does, released only as fixed bands.

**Sources.** The second exception to the DMV rule and the only read of storage DMVs: `Conn.StorageDmv` accepts exactly
`$SYSTEM.DISCOVER_STORAGE_TABLE_COLUMNS` (columns `DIMENSION_NAME, TABLE_ID, COLUMN_ID, COLUMN_TYPE, DICTIONARY_SIZE`)
and `$SYSTEM.DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS` (`DIMENSION_NAME, TABLE_ID, COLUMN_ID, USED_SIZE`). Callers cannot
choose DMVs or columns, and `Conn.Dmv` still refuses both. Sizes, row counts and segment counts never leave the process.
Object ids, names, hierarchies, relationships and partition modes come from `TMSCHEMA_*`.

**Attribution.** Storage ids embed TMSCHEMA ids: table data `T (tid)` with column `C (cid)`, attribute hierarchies
`H$T (tid)$C (cid)`, user hierarchies `U$T (tid)$H (hid)`, relationships `R$T (tid)$R (rid)` (stored with the from
table). Names inside storage ids are sanitized by the engine (hyphens become spaces), so objects are matched by id first,
then by exact name, then by a letters-and-digits comparison. Per column: dictionary (`COLUMN_TYPE` `BASIC_DATA` rows
only) + its data segments in its table's own storage + its attribute hierarchy. Per user hierarchy: its `U$` segments.
Per relationship: its `R$` segments. Per table: its columns, its RowNumber column, its user hierarchies and the
relationships whose from column it holds. Model: all storage. Auto date/time and other private tables (and relationships
touching them) are not listed; together they are `auto_datetime`. Storage that cannot be attributed to a listed object
is `other` and makes `coverage` `PARTIAL`; its storage ids (never sizes) go to the DPAPI log only. Storage ids are never
printed: every printed name is a TMSCHEMA object name.

**Bands** (fixed, lower bounds inclusive): `size_band` `"<1 MiB"`, `"1-10 MiB"`, `"10-100 MiB"`, `"100-1000 MiB"`,
`">=1000 MiB"`; `share_band` (share of the whole model's storage) `"<1%"`, `"1-5%"`, `"5-20%"`, `">=20%"`.
`dictionary_heavy`: the dictionary is at least half of the column's size. Everything is sorted by name (no ranking).
VertiPaq reserves about 1 MiB of dictionary per text column, so even tiny text columns show `"1-10 MiB"`.

**Predicates** (booleans; constant-free DAX written by the engine, no caller input), only on columns of imported tables
(every partition in Import mode, directly or through the model default; never DirectQuery, Dual or DirectLake):
`time_part` (dateTime: some non-blank value `<> TRUNC(value)`), `fractional` (double / decimal: the same test),
`long_text` (string: some value with `LEN(value) > 100`). Every eligible column when the model is under 100 MiB, else only
columns of at least 1 MiB; at most 500 columns (largest first) and at most 5 x `timeout_s` (30-900 s) in total. One `ROW`
query per table; after an error, one query per column. A predicate that errors or times out is omitted (the error goes
to the DPAPI log, never to stdout). Response `predicates`: `OFF` (request `"predicates": false`), `DONE`, or `PARTIAL`
(some eligible column has no answer).

Optional column fields, all closed strings, omitted when unknown. Existing responses without them stay valid.
`encoding` is `Hash` or `Value` from a separate `COLUMN_ENCODING` query (not added to `StorageColumns`); the field is
omitted when that query fails or the value is neither. `segment_band` counts segment rows already read for that column
(`dictionary: false`, default storage kind) and is only `Buckets.Count`: `"0"`, `"1-9"`, `"10-99"`, `"100-999"`,
`"1k-9k"`, `"10k+"`. Referential integrity runs `COUNTROWS` / `FILTER` / `RELATED` on active imported relationship ends
that can be tested without printing keys. `ri` is `RI_OK` or `RI_VIOLATION` and `ri_count` is `Buckets.Count` of the
mismatch count. The same pair may appear on the relationship. A failing test omits that end. No min/max, no raw counts,
no key samples.

```json
{"ok": true, "verb": "memory", "model": {"size_band": "10-100 MiB"},
 "tables": [{"name": "Sales", "size_band": "10-100 MiB", "share_band": ">=20%",
   "columns": [{"name": "OrderDate", "size_band": "1-10 MiB", "share_band": "1-5%", "dictionary_heavy": false,
     "encoding": "Value", "segment_band": "1-9", "ri": "RI_OK", "ri_count": "0", "time_part": true}],
   "hierarchies": [{"name": "Calendar", "size_band": "<1 MiB"}]}],
 "relationships": [{"name": "SalesDate", "from": "Sales[DateKey]", "to": "Date[DateKey]", "size_band": "<1 MiB",
   "ri": "RI_OK", "ri_count": "0"}],
 "other": {"size_band": "<1 MiB", "share_band": "<1%"},
 "auto_datetime": {"size_band": "1-10 MiB", "share_band": "5-20%"},
 "coverage": "FULL", "predicates": "DONE"}
```

`other` appears only when something was not attributed; `auto_datetime` only when the model has private tables.
Strict tier: not refused; every band (`model`, `size_band`, `share_band`, `segment_band`, `ri_count`, `other`,
`auto_datetime`) is omitted, while names, `dictionary_heavy`, `encoding`, `ri`, the predicate booleans, `coverage`
and `predicates` stay (booleans are allowed in both tiers, as in `probe`). Engine failures use the error envelope.

## v1.5 addition: `perf`

### `perf`

Request `{"private_dir", "port", "dax", "runs", "baseline"?, "action"?}`. `dax` must pass the snapshot/smoke
guard (`DEFINE` or `EVALUATE` first; no `INFO.*`, no `EVALUATEANDLOG`). `runs` is 1 to 20. `action` is `save`,
`compare`, or absent. `baseline` is a file name (`^[A-Za-z0-9._-]{1,64}$`) required when `action` is set.
Each run is a cold XMLA `ClearCache` (`DatabaseID` is the catalog name) followed by the query. Result rows are
discarded. The median and the run-to-run spread stay in the process. The baseline file is DPAPI in
`private_dir\perf\<name>.bin` and holds only those two numbers.

Trace events, subscribed only when `Enum.TryParse` finds the name: `QueryEnd`, `VertiPaqSEQueryEnd`,
`VertiPaqSEQueryCacheMatch`, `DirectQueryEnd`, `AggregateTableRewriteQuery`, `DAXQueryPlan`. Unknown names are
skipped. `ExecutionMetrics` is not required. If trace setup throws, the verb still returns
`trace: UNAVAILABLE`. A guard refusal is still an error. If no `QueryEnd` is captured, `timing` is `UNAVAILABLE`.

`fe_share` is `LOW` when formula-engine time is under 25% of `QueryEnd`, `HIGH` when over 75%, otherwise `MID`.
Formula-engine time is `QueryEnd` duration minus the sum of storage-engine durations, computed in-process and
clamped at zero. `se_queries` and `dq_events` are `0`, `1_5`, `6_20`, `21_100`, `GT_100`. `cache_hit` is `NONE`
when there is no cache-match event, `MOST` when cache matches outnumber storage-engine queries, otherwise `SOME`.
`callback` is true when private storage-engine text contains `CallbackDataID` or `EncodeCallback`; that text is
never emitted. `agg_rewrite` is `HIT` when rewrite text contains `matchFound`, `MISS` when a rewrite event fired
without it, and `NONE` when the event did not fire. `duration_band` is `LT_100MS`, `100_500MS`, `500MS_2S`,
`2_10S`, `GT_10S`.

`plan`, when a plan event was captured, counts operator classes as `0`, `1`, `2_5`, `6_20`, `GT_20` only:
`CallbackDataID`, `Spool_Iterator`, `CrossApply`, and `LargeMaterialization` (a spool whose record count bands
through `Buckets.Count` to `1k-9k` or `10k+`). Spool record counts are not emitted. No plan text.

Compare uses the same rule as `pbz/perf.py` (`RATIO = 1.25`): if `old <= 0`, `NOISY`. Otherwise if
`new/old > 1.25` or `new/old < 1/1.25`, then `NOISY` when `abs(new-old) <= spread`, else `SLOWER` when the ratio
is over 1.25 and `FASTER` when it is under `1/1.25`. Otherwise `SAME`. `spread` is the wider of the saved and
the new run-to-run ranges. Stdout carries the verdict only.

```json
{"ok": true, "verb": "perf", "timing": "BANDED", "duration_band": "100_500MS",
 "fe_share": "LOW", "se_queries": "1_5", "dq_events": "0", "cache_hit": "NONE",
 "callback": false, "agg_rewrite": "NONE", "trace": "CAPTURED", "verdict": "SAME",
 "plan": {"CallbackDataID": "0", "Spool_Iterator": "1", "CrossApply": "0", "LargeMaterialization": "0"}}
```

`verdict` is present only on a compare that had both medians. `baseline` is present only as `SAVED`
after `action: save`, or `MISSING` when a compare has no saved baseline. A trace that did not start is
`{"ok": true, "verb": "perf", "timing": "UNAVAILABLE", "trace": "UNAVAILABLE"}`.

Implementation clarifications that callers must know (response container names, the blank group
key, optional `timeout_s`, stricter message scrubbing, query guards) are listed in
`engine/README.md` under "Contract deviations and clarifications".
