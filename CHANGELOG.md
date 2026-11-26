# Changelog

All notable user-facing changes to `pbi-zerodata` are listed here. Internal hardening rounds
(R10–R17) are summarized; see `skills/powerbi-zerodata/reference/improvements.md` for the detailed
honesty and atomicity notes.

## Unreleased

- `pbz snapshot save` and `pbz snapshot compare` no longer move legacy snapshot files. An invalid query list is rejected before Desktop is contacted. A request rejected in preflight, before lock acquisition begins, leaves the snapshot directory unchanged; a rejection from lock acquisition onward can leave the pending directory and the per-name lock file behind, as any locked operation can. New saves still write `snapshots/results/` and `snapshots/specs/`. `migrate_snapshot_layout` remains available for a direct call. A save now refuses a file or link where the named result or spec directory must be a real directory, before any port probe, query, or budget spend. An explicit empty compare spec is rejected even when a saved spec exists. Omitting the compare spec still uses the saved spec. Pair publication is journaled under the per-name pair lock. Once a request has passed validation, a pending publication is resolved from its journal: a published journal whose new pair is already live is discarded without reading the stage; a published journal with the new result live beside the prior spec is completed from the staged spec when the staged spec reads back intact, or blocks with RECOVERY_REQUIRED if that completion itself fails, and when the staged spec does not read back intact that state is not cleaned up, so a compare blocks with RECOVERY_REQUIRED and only an eligible save may supersede it; a journal whose prior pair is still live and coherent is cleaned up, leaving that pair in place, and a cleanup that itself fails blocks the operation with RECOVERY_REQUIRED; in the remaining cases an eligible save supersedes the pending publication and any other operation blocks with RECOVERY_REQUIRED. A query spent by an interrupted save is not refunded. The lock and journal coordinate PBZ processes only: they do not protect against a writer outside PBZ replacing the result bytes between the engine write and the readback that records the result hash, and in that interval the recorded hash binds whatever bytes are on disk.

- `pbz doctor ci` does not treat a local report as proof that tests ran. A serial row counts only when `launched` is true, the scan completed successfully, and the report names its workflow run and attempt. A missing `launched` value is not a successful run. An earlier live PASS does not hide a later FAIL for the same case. Conflicting live results are not acceptance. The POSIX exemption report is read as that lane only and is not Windows or Linux serial coverage. This build cannot retrieve the hosted artifact, so the public command stays incomplete until that binding exists. A labeled replay of an authenticated artifact can still satisfy the decision. Optional skipped jobs still do not block required checks.

- `pbz doctor ci` reports the installer value `main-offline-required` as its own hook mode. That mode means the staged pre-commit skips the suite and the pre-push hook still runs the advisory suite. It is not branch protection. An optional skipped shard does not mark required checks pending. A `serial.json` report is compared as a multiset of suite, test, and reason. The report platform must match, and every suite in the offline manifest must have run. A running Desktop process is not live-test evidence. A live record counts only when its revision matches and its result is PASS. The public launchers pass `-B`, so they write no bytecode. The git hooks drop hook git variables before the offline suite, so a fixture repository cannot commit into this one.

- `tests/ci_weights.py` can recompute dormant shard weights from hosted sharded reports. Checked-in weights and serial execution stay unchanged.

- `pbz doctor ci` treats a ruleset `workflows` rule as required and does not accept a check-name match for it. Check runs are read with `filter=all`. Empty `required_checks` still honors `required_contexts`. A green unrelated check does not pass it. Any other ruleset type outside a fixed inactive list stays required and unproven. A `required_checks` entry without a string context is an API error, including when other entries are valid. A workflow or unproven requirement stays even when a same-named check has an app id. An any-app requirement from one source stays when another source names an app. A skipped run of a required context blocks hosted success and `SUCCEEDED`. Commit status contexts match required checks without regard to case. A pull request review flag that is not a boolean, or a review url that is not an https string, is an API error. A present null status-check list is an API error. A non-integer app id is an API error. A repeated key in a GitHub API JSON object, a skip record, skip provenance, or the skip manifest is an API or parse error. Branch ancestry is checked against the captured commit, not a later HEAD. A repeated message key in a GitHub error body is not read as branch not protected. A non-string error message is not branch not protected. When `gh` returns a JSON error body, that body is classified instead of the stderr text, and a repeated key in it is an API error. A 404 is unprotected only when its message is exactly the branch-not-protected text. Dirty status includes submodule changes that Git would otherwise ignore. Dirty status includes assume-unchanged and skip-worktree content changes, and a missing file with those bits when sparse checkout is off. A checked-out submodule is checked the same way, including when its HEAD is not the recorded gitlink. An executable-bit change is dirty when Git tracks file mode, and a symlink-type change is dirty when Git tracks symlinks. A symlink blob is the link text, not the target file. A worktree symlink over an index entry that is not a symlink is dirty even when Git does not track symlinks. A symlink index entry checked out as a regular file stays clean when Git does not track symlinks. An index listing that is not valid UTF-8 is not a clean tree. A checkout path keeps trailing spaces, so a different directory is not that checkout. A trailing carriage return in that path stays part of the name. The pre-push hook path keeps an embedded carriage return, so a different hook file is not that hook. A git path that is not valid UTF-8 is not opened. A leading space in the pre-push hook path stays part of that path. The executable bit compared is the owner bit. If HEAD changes during the dirty check, the tree is not clean. That check hashes blobs and does not write the index. Online branch membership uses only the ls-remote line whose ref is exactly `refs/heads/<branch>`. A failing commit status for a required context blocks a green check run of the same name. A skipped or neutral run of a required context is not hidden by another success. Unreadable branch protection does not report hosted checks as success. Online branch membership uses `git ls-remote` on the origin URL captured with the repository slug. Branch protection, check runs, and commit statuses use that same slug. Evidence from a second origin is not combined into hosted success. The checked-out branch is the full `refs/heads` name with that prefix removed once, so a tag of the same name is not a different branch. A padded origin URL is not rewritten into another URL, and that remote is not hosted success. A repeated padded path component is compared at its own position, so another copy cannot hide a collapsed name. A git SSH URL that uses a slash instead of a colon is not a GitHub repository. Branch ancestry ignores replacement refs and a legacy grafts file, so neither can put a commit on the protected branch. The skip directory is the `PBZ_SKIP_OUTPUT` path with its spacing left intact. A `gh` body that is not valid UTF-8 is an API error, so a damaged byte cannot stand in for a check name. A valid name that contains a replacement character still matches. A symlink in place of a submodule entry is dirty even when the link target is a clean checkout of the recorded commit. A commit status whose sha is not the captured commit is an API error. `git ls-remote` runs only when that command would keep the captured origin URL, so a second `insteadOf` rewrite is not branch membership. Padded `--branch` values are rejected.

- `pbz check` keeps an explicit `tol` on `filter_independent`, including zero. `pbz refresh` keeps `UNSUPPORTED`. A sign check with an empty `expect` fails. Snapshot compares that are only `NEW` or `MISSING`, and health responses with errors, are not a success.
- CI finding fingerprints at version 1 hash rule, code, object, and path. The path is `where`, or `file` when `where` is missing. Saved baselines that used the truncated fingerprint need to be saved again.

- `pbz revert` takes the project transaction lock, refuses pending recovery, and preserves external object saves made during planning.

- `pbz pull --rebase` preserves mirror deletions and reports upstream changes as conflicts without advancing their manifest or base.

- Recovery preserves external saves made between its two rollback passes and keeps the transaction pending as a recovery conflict.

- Push, status and pull no longer treat a withheld file's designed absence as a deletion,
  while recreated withheld files remain refused as lossy.

- Multiline dynamic format expressions (`formatStringDefinition`) written by `pbz format-expr`
  now reload as one complete DAX region on the measure, so deps, rename, usage and tokenization
  see every reference. Renaming a referenced measure updates its reference inside the format
  body. Mirrors already written at the old indent are not repaired on load: the definition
  still gets no region of its own, and individual body lines may parse as phantom declarations.
  Rewrite the format expression with `pbz format-expr` before relying on rename to update all
  references in it.

- Private indexes publish through an `index-current.json` manifest pointing to a completed,
  hash-checked `index-<uuid>` generation directory. Publication pins the hardened artifacts
  and prunes older generations. New engine indexes include a model stamp over sorted model
  structure and the latest refresh time, so changes on the same endpoint invalidate reveals.

- `pbz probe` and `pbz index` do not return calendar year endpoints. Probe reports blanks,
  negatives, future dates, and coarse buckets. Duplicate keys and orphans stay on `pbz check`.
- Best-practice data rules run when `pbz audit memory` has saved a document. A missing
  referential-integrity status is not a passed check. The high-cardinality bidirectional rule
  is clean only when both ends are under 1 MiB; a missing band on that rule is not a clean pass.
- `pbz revert` refuses a link under the private base before reading it. Handoff skips a page id
  that is not a single path segment. A link at the private `index.json` is refused; a missing
  index still means heuristic mode.
- Creating a junction, symlink, or hardlink with `New-Item` no longer skips a denial of a
  private or other denied path. Creating an ordinary directory is unchanged.
- Health, probe, index, and snapshot errors use a closed class. An unknown class is reported
  as `ENGINE_UNAVAILABLE`.
- `pbz smoke` reports `N/A` for a text box, image, shape, button, navigator, or iframe, and
  coverage `FULL`, `PARTIAL`, or `NONE`. `pbz values` returns category labels from the private
  index, or a distinct bucket and mirror tokens. It is not a live grid.

## 2026-11-10

- Fail closed on a few edges that could reset a good file or publish the wrong name. An unreadable
  checks ledger is refused. Native SQL is parsed only when the argument is one select string.
  `pbz probe` rejects a column reference for a different table. Vendor-host matching is the domain
  or a subdomain, not a name that merely ends with `microsoft.com`. A human `pbz watch` polls until
  interrupted; MCP runs one pass.
- Banded performance (`pbz perf`), refresh duration bands, memory encoding / segment / referential-integrity
  bands, and an RLS check kind. Durations, counts, plan text, and role identity stay inside the engine.
  Real-model RLS and Performance Analyzer replay (`pbz perf-import --replay`) are human-terminal only.
  Synthetic RLS runs only in a verified `pbz-synth` tree.
- Evidence log rotates at 1 MiB into `.pbz/evidence.d/`, keeps an HMAC chain, and prunes segments older
  than 30 days. A file already over the cap is still removed by hand.
- Mirror authoring: `find` / `replace`, pattern `rename`, `rewrite`, `expand`, calendars, KPIs, measure
  scripts, function import, and translation export/import. `DIVIDE` rewrites need `--accept-semantic`.
- Analysis: source-column lineage and impact, native SQL column lists (tokenized SQL stays opaque),
  relationship paths, model overlap, unused what-if from memory bands, custom BPA rules, and CI output
  (`--fail-on`, `--ci`, SARIF/TRX/JUnit). Sample workflow: `docs/examples/bpa-annotations.yml` (not this
  repo's CI). Contract for what stays out of scope: `skills/powerbi-zerodata/reference/external-parity.md`.
- `install.py` junctions the Codex / ChatGPT skill folder as well as the Claude and shared
  agent skill folders. A `git pull` updates the skill and the guard script the hooks run.
  Setup steps: `docs/install.md`.

## Round 18 — 2026-11-09

- `pbz interaction <page> --show` lists stored cross-filter modes (`filter`, `highlight`, `none`,
  `default`, or `REFUSED`). Applying one pair keeps the other pairs. Page JSON is read through the
  mirror check, and the command result uses the page id. `pbz review --interactions-spec` binds a
  static interaction contract to that spec's digest.
- Round 18: interaction contracts move to contract version 2. Per-case `status` and `reason` are the
  closed tokens `MATCH`, `MISMATCH`, `DEFAULT_UNRESOLVED`, `MISSING_ID`, `RUNTIME_ONLY`, `LEGACY`,
  `UNKNOWN_TYPE`, `UNSUPPORTED`, and `REFUSED`. Evidence stage `STATIC_CONTRACT` cannot satisfy a
  runtime requirement or `HUMAN_ACCEPTED`. `validate` field reads now raise `UNSAFE_PATH` on links
  instead of recording `BAD_JSON`. Guard denies quoted protected writes, recursive ancestor
  copy/move/delete, and `robocopy /MIR` of the workspace without flagging a `workspace-old` sibling.
- Public documentation pack: overview, getting started, full CLI catalog, security and contributing
  guides, MIT license.
- Repo hygiene: expanded `.gitignore`, removed internal Round-17 plan scratch from the tree.
- Regression fixes after Round 17 fail-closed policy: `test_memory` expects `REFUSED` for unusable
  disclosure; evidence I/O uses `_workspace_root` so review fixtures keep working; desktop evidence
  tests use canonical revision fingerprints.

## Round 17 — 2026-11-08

- Evidence log and synth-test goldens anchored under the workspace `.pbz` root with a 1 MiB evidence
  cap; review accepts only after validated append.
- Present unusable or aliased policy fails closed (lockdown) instead of loosening disclosure.
- `diff --patch` and `push` refuse aliased/hardlinked mirror entries; recipe snapshot/restore refuse
  aliased mirror trees (`RECOVERY_REQUIRED` if planted mid-run).
- Freshness class `OTHER` maps to closed `ENGINE_UNAVAILABLE`.
- Five offline suites registered: evidence honesty, synth-test state paths, policy fail-closed,
  mirror alias disclosure, recipe paths.

## Round 16 — 2026-11

- Anchored `.pbz` / mirror writers; shared spec input gate; requirement proof binding to revisions.

## Round 15 — 2026-11

- Exclusive writer temps; alias-safe memo; fail-closed disclosure paths for memory and docs.

## Rounds 12–14 — 2026-11

- Atomic `commit_bytes` across authoring, consumers, synth publish, handoff, preview, memscrub;
  honest outcome mapping; pull/push/revert refusal boundaries.

## Round 11 — 2026-11

- Atomic report writers with refuse-before-write for property, style, bookmark, a11y, and copy paths.

## Round 10 — 2026-11

- Atomic unused/governance edits; honest smoke; refuse-before-write foundations.

## Earlier (2026-11)

- Measure Killer–parity analysis verbs (metadata only): unused, lineage, suggest, BPA, similar,
  changes, docs, m-lineage.
- Authoring and governance editors; synthetic model and synth tests; MCP server; consumers;
  audit/consolidate; consent, preview, interactions, freshness, requirements, review/recipes.
- Guard hooks for Claude Code and Codex; `pbz-engine` declassification broker.
