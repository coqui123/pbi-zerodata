# Changelog

All notable user-facing changes to `pbi-zerodata` are listed here. Internal hardening rounds
(R10–R17) are summarized; see `skills/powerbi-zerodata/reference/improvements.md` for the detailed
honesty and atomicity notes.

## Unreleased

- Offline CI shard weights use per-suite median seconds measured in hosted sharded runs, with sparse suites keeping their prior weights.

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
