# Changelog

All notable user-facing changes to `pbi-zerodata` are listed here. Internal hardening rounds
(R10–R17) are summarized; see `skills/powerbi-zerodata/reference/improvements.md` for the detailed
honesty and atomicity notes.

## Unreleased

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
