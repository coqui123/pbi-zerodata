# Disclosure contract

What an agent may see from pbz. This is the closed contract for Release 1. It does not add new
derived outputs. The full slot allowlist tokenizer remains an R2 follow-up; Release 1 stays fail-closed without it. Storage bands and the three memory predicates below are grandfathered as they
already ship; this note cites them and does not reopen that policy.

Heuristic classification is **not** an OS boundary. Closed verdicts do **not** prove that a fact
cannot be inferred from many allowed answers. See [leak-vectors.md](leak-vectors.md) (residual
risks and the Hardening tier) and `pbz doctor`.

## Permitted metadata

Names, ids and structure only:

- table, column, measure, hierarchy, relationship, role, perspective and culture names
- report, page, visual, bookmark and filter ids; visual types; data-role and queryRef names
- DAX and M as authored in the mirror, with sensitive literals already replaced by tokens
- types, format strings, folders, flags, lineage tags, relationships and PBIR layout geometry
- file paths inside the mirror, rule ids, object counts of the model (not row counts of data)

Category labels that policy or the engine index classified as non-sensitive stay readable.
Pivoted names that became table or column names stay visible as metadata. That is a residual risk,
not a license to treat them as data values.

## Closed verdicts and errors

Engine and CLI results use a fixed vocabulary. Raw error text, cell values and durations are not
in it.

| Family | Values |
|---|---|
| Smoke / query | `OK`, `EMPTY`, `ERROR`, `UNSUPPORTED`, `PROTOCOL_ERROR`, `BUDGET`, `PARTIAL` |
| Checks | `PASS`, `FAIL`, `EMPTY`, `BUDGET`, `ERROR` |
| Snapshots | `SAME`, `DIFF`, `NEW`, `MISSING`, `STALE_DATA` |
| Availability | `ENGINE_UNAVAILABLE`, `NOT_AVAILABLE`, `UNSUPPORTED`, `TIMEOUT`, `NOT_READY` |
| Blocks | `DESKTOP_OPEN`, `DESKTOP_DIRTY`, `DESKTOP_STATE_UNKNOWN`, `CONFLICT`, `BUDGET`, `INCOMPLETE` |
| Requests | `BAD_REQUEST`, `INVALID`, `UNKNOWN_TOKEN` |
| Outcome envelope | `SUCCEEDED`, `FAILED`, `BLOCKED`, `UNAVAILABLE`, `INVALID`, `PARTIAL` |

`ERROR` is a class (`SYNTAX`, `UNKNOWN_OBJECT`, …), never the engine's message. Anything outside
these sets is not a successful disclosure; treat it as a bug.

## Approved derived outputs (grandfathered)

Already released by `pbz audit memory`. Cited from [leak-vectors.md](leak-vectors.md) (storage
DMV exception) and [audit.md](audit.md) (Storage bands). Not expanded here.

- Size bands: `<1 MiB`, `1-10 MiB`, `10-100 MiB`, `100-1000 MiB`, `>=1000 MiB`
- Share bands: `<1%`, `1-5%`, `5-20%`, `>=20%`
- `dictionary_heavy`
- Three yes/no predicates on Import columns only: `time_part`, `fractional`, `long_text`

Sorted by name, never by size. Strict disclosure withholds the bands; names, `dictionary_heavy`
and the predicates stay. Other coarse buckets already documented for checks and probes (row
buckets, `NONE`/`ONE`/`FEW`/`MANY`, blank/duplicate/orphan flags) stay as specified in
[checks.md](checks.md). No new band, predicate or statistic is in scope.

## Private-only artifacts

These never enter chat, the mirror, memory notes or commit messages:

- token map and category index (`tokens.bin`, `categories.bin`, `index.json` value HMACs)
- DPAPI engine log, diagnostic tracebacks, push journals and file backups
- `.pbi` caches, `cache.abf`, images, custom-visual packages, consumer cell/pivot caches
- consent file bytes (the receipt exposes digest, purpose and status only)
- unbanded storage DMV rows

## Excluded operations

pbz does not offer, and agents must not reconstruct:

- native interaction execution (static contracts only; native stays `UNSUPPORTED`)
- screenshots, UI automation or browser reads of Desktop or the service
- real DAX dumps, `executeQueries`, ADOMD, XMLA, OData or DMV browsing beyond the memory exception
- `between` checks, printed durations, or error text that echoes a value
- copying data files, report images or the private store into the workspace

Synthetic models (`pbz synth`) may show fake rows. They are not this contract's real-data output.

## What a verdict is not

A run of `OK` / `PASS` / `SAME` means the asked question was answered inside the budget. It does
not mean the underlying values are hidden from inference, and it does not mean the OS isolation
checklist in [leak-vectors.md](leak-vectors.md) is in place. Heuristic mode (no `pbz index`) misses
literals the index would catch. `pbz doctor` reports that checklist and does not change it.
