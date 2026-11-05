# pbz-engine

C# (.NET 10) declassification broker for the `powerbi-zerodata` toolkit. It is the only component
that talks to a live Analysis Services model (the local instance Power BI Desktop starts). It may
read real data internally; its stdout is a closed vocabulary (enums, booleans, buckets, object names,
caller ids, HMACs, log ids, scrubbed messages). Binding contract: `CONTRACT.md` (v1 + v1.1
additions). Response schema: `contract.schema.json` (JSON Schema 2020-12, one `oneOf` branch per verb
plus the error envelope, `additionalProperties: false` everywhere).

## Build

Prerequisites: .NET SDK 10 (10.0.300 used), network access to nuget.org for the first restore.
Packages: `Microsoft.AnalysisServices` 19.118.3 and `Microsoft.AnalysisServices.AdomdClient`
19.118.3 (their transitive Microsoft.Identity.* dependencies come with them). DPAPI is P/Invoke to
`crypt32.dll`; no other packages.

```
cd <skill>\engine
dotnet publish -c Release -r win-x64 --self-contained false -o bin
```

Result: `engine\bin\pbz-engine.exe` (framework-dependent; needs the .NET 10 runtime). Intermediate
output goes to `engine\build\` and `engine\obj\` (safe to delete). `pbz\engine.py` finds the exe at
`engine\bin\pbz-engine.exe`.

## Invocation

```
pbz-engine.exe <verb>      one JSON request on stdin, one JSON response on stdout, exit code 0
```

stderr is never written (it is redirected to null inside the process). Raw diagnostics go only to
`private_dir\logs\engine.log` as `log_id TAB base64(DPAPI(utf8 text))`; responses carry the `log_id`.

Source map: `Program.cs` (dispatch, envelope, watchdog), `Json.cs` (request accessors), `Errors.cs`
(classes, DPAPI log), `Scrub.cs` (message scrubber + word allowlist), `Vault.cs` (DPAPI, key, HMAC,
normalization, buckets), `Dax.cs` (quoting, column refs, query guards), `Connect.cs` (ADOMD connection,
TMSCHEMA metadata, `connect`), `Health.cs`, `TmdlValidate.cs`, `Refresh.cs`, `Compiles.cs`,
`ValueIndex.cs` (`index`, shared column stats), `Probe.cs`, `Checks.cs`, `Snapshots.cs`, `Smoke.cs`, `Deps.cs`,
`SynthQuery.cs`, `Memory.cs` (`memory`: storage bands from the two storage DMVs `Conn.StorageDmv` allows, see
CONTRACT.md v1.4).

## Spike results (Desktop Store build 2.159.1231.0, Windows 11)

1. **ADOMD on .NET 10 against the Store Desktop engine: works.** NuGet AdomdClient 19.118.3 opens
   `Data Source=localhost:<port>` (server 17.0.93.x). `DBSCHEMA_CATALOGS` returns exactly one catalog
   (a GUID name); Desktop upgrades compatibility level 1600 to 1606 on load. TOM `Server.Connect`
   works on the same port.
2. **`TmdlSerializer.DeserializeDatabaseFromFolder` on the fixture: works**, including a nameless
   `database` block, `ref role`, and a role `member` (deserializes as ExternalModelRoleMember; Desktop
   opens it). Findings: files not listed by `ref` are still loaded (refs only order objects) and a
   `ref` to a missing file is silently ignored, so the ref/file inventory is the only way to catch
   them. `TmdlFormatException` exposes `Document` ("./tables/X"), `Line` and `LineText`, and its
   message embeds the line text, so the engine rebuilds the message without the Document/Line parts.
   Reference errors (`TmdlDeserializationWithReferenceErrorsException`) carry `SourceObject` strings
   such as `{ Relationship: '...' }` and no line. File names percent-encode characters that are
   invalid in file names (`/` -> `%2F`, `:` -> `%3A`, ...).
3. **Cheapest bind-only `compiles` form:**
   `DEFINE MEASURE 'T'[__pbz_c] = <expr>` + newline + `EVALUATE ROW("x", IF(FALSE(), [__pbz_c]))`.
   It surfaces syntax errors and bind errors (unknown column, measure, table or function; naked
   column reference) and never evaluates the expression (ERROR() and text-to-number conversions stay
   silent); the only cell returned is BLANK and is not read. `FILTER(ROW(...), FALSE())` also binds
   without evaluating but relies on the optimizer; `TOPN(0, ...)` does evaluate (ERROR() fires), so
   it is not used. Syntax errors echo the whole query text; the engine strips it before scrubbing.
4. **Refresh from outside Desktop:** a TOM/TMSL full refresh of M partitions succeeded once in the
   spike but is not reliable: on a later run Desktop showed a modal "Something went wrong" (its Power
   Query session is not available to external clients) and the refresh hung. Contract v1.1 therefore
   forbids engine refreshes of M partitions (see CONTRACT.md). A PBIP whose tables are all calculated
   (DATATABLE / SELECTCOLUMNS / CALENDAR) opens with data and needs no cache.abf, so the fixture is
   built that way; DAX-only recalculation (`calculate`, or `full` on calculated partitions) works.
5. **Port discovery (what `pbz` must do):** the Store `PBIDesktop.exe` command line contains the
   opened `.pbip` path; its child `msmdsrv.exe` has `-s "<...>\AnalysisServicesWorkspaces\AnalysisServicesWorkspace_<guid>\Data"`;
   `<that Data dir>\msmdsrv.port.txt` holds the port as UTF-16LE text without BOM.
   `Get-NetTCPConnection -OwningProcess <msmdsrv pid> -State Listen` agrees. `powerbi-desktop status`
   lists `pid`, `currentFilePath` and `hasUnsavedChanges` per instance (the flag turned true after an
   external refresh). `powerbi-desktop open` cannot find the Store executable
   (`DESKTOP_EXE_NOT_FOUND`, also with `PBI_DESKTOP_PATH` pointing at the WindowsApps alias): open the
   `.pbip` through its file association (`Start-Process` / `os.startfile`) instead.
6. **Closing:** `Process.CloseMainWindow()` on the fixture instance. If Desktop then shows its save
   prompt, the person at the machine answers it; nothing automates the UI.

## Python side: what to call and know

- `pbz/engine.py`: `call(verb, request, timeout=120) -> dict`. Pass `private_dir` (absolute) in every
  request and `port` for live verbs. Wrapper envelopes: missing exe or unparseable stdout ->
  `ENGINE_UNAVAILABLE`; wall-clock timeout -> `TIMEOUT`; request not JSON-serializable -> `BAD_REQUEST`.
- Before any data verb: find the port as in spike 5 for the instance whose file is the workspace's
  `.pbip`, cross-check with bridge status, then call `connect` and compare `tables` with the expected
  model before sending data queries. Never pass a port that belongs to another instance.
- Key: `private_dir\key.bin` = DPAPI(CurrentUser, `CRYPTPROTECT_UI_FORBIDDEN` = 0x1, no entropy,
  no description) of 32 random bytes; the engine creates it when missing (atomic create, first writer
  wins). Read it with `CryptUnprotectData` through ctypes (see `dpapi_unprotect` in
  `tests/test_engine.py`).
- `hmac(value)` = lowercase hex HMAC-SHA256(key, utf8(normalize(value))):
  - text: `s.Normalize(FormC).Trim().ToLowerInvariant()`. Python: `unicodedata.normalize("NFC", s)`,
    strip the .NET whitespace set (Python's `str.strip()` also strips U+001C..U+001F, .NET does not),
    lowercase with 1:1 (simple) mapping (`str.lower()` maps U+0130 to two code points; .NET gives `i`).
  - numbers (int64, double, decimal): round to 12 significant digits (`G12`), fold -0 to 0, then
    .NET `"R"`: shortest round-trip digits, plain notation unless the value is >= 1e17 or < 1e-4
    (then `1E+17`, `1.5E-05`; checked against the .NET 10 runtime), integers without `.0`. Reference: `dotnet_r` / `norm_number` in
    `tests/test_engine.py` (verified against engine output for integers, decimals and dates).
    Int64 values with more than 12 significant digits collapse to the same text (contract rule).
  - dates: `yyyy-MM-ddTHH:mm:ss`; booleans: `true` / `false`; BLANK: the literal string `∅blank`.
  - tuples (snapshot keys and rows): normalized values joined with U+001F, then one HMAC.
- v1.1 `word_hmacs` words: split on whitespace, strip non-letters at both ends, keep words with at
  least 3 letters, then `hmac(word)` (reference: `words` in `tests/test_engine.py`).
- `categories.bin` = DPAPI(UTF-8 JSON `{"T[C]": [values sorted ordinally]}`); `index.json` is plain
  JSON with HMACs only.

## Contract deviations and clarifications

Kept minimal; everything else follows CONTRACT.md.

1. Envelope: every response has `ok` and `verb`. Containers: `compiles` -> `items`, `check` ->
   `checks`, `snapshot-save` / `snapshot-compare` -> `name` + `queries`, `smoke` -> `visuals`,
   `probe` -> `table`, `rows_bucket`, `columns`. `snapshot-save` reports `SAVED` or `ERROR` per query.
2. Group keys: a BLANK key is `{"blank": true}` (revealed or not). Revealed values: text as is (up to
   256 chars), integers without decimals, other numbers in `R` format, dates `yyyy-MM-dd` (with
   `THH:mm:ss` when there is a time), booleans `true` / `false`. Groups are sorted by their JSON text.
3. `sign`: `sign` is returned in both tiers; status is PASS unless the optional `expect` (one value
   or a list of NEG / ZERO / POS / BLANK) is given, then PASS or FAIL.
4. `equal` / `additive`: default `tol` 1e-9 (relative). BLANK equals only BLANK. `additive` treats a
   BLANK total as 0 when groups have values. `mismatch` appears only on grouped `equal`.
5. EMPTY: grouped `equal` with no groups (plus `mismatch: NONE`); `additive` with a BLANK total and
   no groups; `unique`, `no_blank_keys` on an empty table; `no_orphans`, `subset` with no non-blank
   from-values.
6. Key checks: `no_orphans` / `subset` ignore BLANK (and empty text) from-values; `no_blank_keys`
   counts BLANK and, for text, empty strings; `unique` counts duplicated values including BLANK.
   `offenders_bucket` (shape tier) counts offending values or rows.
7. Unknown or missing `kind` (including `differs`, `filter_independent`, `compiles`, `between`) ->
   UNSUPPORTED. A malformed check (bad column reference, missing field, guard hit) -> ERROR
   BAD_REQUEST; a column not in the model -> ERROR UNKNOWN_OBJECT.
8. Optional request field `timeout_s` (1-3600, default 60) sets the per-command timeout. `ping`
   accepts a request without `private_dir`.
9. Messages are scrubbed more strictly than the four contract rules: echoed query text is removed
   first, and every remaining unquoted word must be on an allowlist (engine prose, DAX and TMDL
   keywords, known object names) or it becomes `«w»` (runs collapse). `health` returns messages only
   for measures; other kinds (column, partition, hierarchy, relationship) carry the class only because
   evaluation and refresh errors can quote data. `compiles` messages appear only for engine bind
   errors; request problems carry the class only.
10. Query guards (BAD_REQUEST): expressions and `where` items may not contain `EVALUATE` or `DEFINE`
    outside strings, quoted names and comments; `snapshot-*` and `smoke` queries must start with
    `DEFINE` or `EVALUATE` (never TMSL, MDX or `$SYSTEM` text); `INFO.*` functions and
    `EVALUATEANDLOG` are refused everywhere. This keeps the DMV rule true for caller-written DAX.
11. `smoke`: a visual without `dax` -> UNSUPPORTED.
12. `connect`, `health`, `index` skip private tables (auto date/time `LocalDateTable_*`,
    `DateTableTemplate_*`, `IsPrivate`, `ShowAsVariationsOnly`). `connect` requires exactly one
    catalog (a Desktop instance), else ENGINE_UNAVAILABLE.
13. `index`: `class` and `card_bucket` only on text columns; index.json may carry `"capped": true` for
    high columns over `value_cap`.
14. `tmdl-validate`: `line` omitted when unknown (reference errors); paths are relative with forward
    slashes; orphan files do not make `valid` false, missing refs do.
15. `snapshot-compare`: `STALE_DATA` only when the result differs and the latest partition
    `RefreshedTime` changed (an unchanged result stays SAME); `MISSING` entries follow the requested
    ones; `snapshot-save` replaces the whole snapshot file.
16. `refresh`: v1.1 semantics (CONTRACT.md). A failing refresh uses the error envelope.

## Tests

`python tests\test_engine.py` (stdlib + jsonschema). Offline part: envelope errors, `tmdl-validate`
on the fixture and on broken copies, `pbz/engine.py` wrapper. Live part (opens
`tests\fixture\Canary.pbip` through its file association when no instance has it open, and queries
only the instance whose process or bridge status shows that file and whose TMSCHEMA tables equal the
fixture's): every verb, both tiers, Python HMAC/DPAPI reproduction, forced errors, and a canary scan
over all engine stdout/stderr, every file under the skill folder except the fixture sources, and the
private store outside DPAPI blobs. Flags: `--offline`, `--close` (CloseMainWindow at the end),
`--keep` (keep the temporary private_dir).

`python tests\test_memory.py` covers `memory` and `pbz\memory.py` offline (envelopes, closed bands, save / load / stale,
budget); `--live --ws <folder>` adds the live verb on a copy of the fixture already open in Desktop (run it with the same
`PBZ_PRIVATE_ROOT` as the pbz session there): closed bands only, no numbers, no stray strings, no canaries.
