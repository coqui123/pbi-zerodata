# Workspace verbs: status, init, describe, admin, handoff

Day-one and administrative commands. All print metadata or closed messages; none return company
cell values.

## Capability matrix: `pbz capabilities`

Prints a closed JSON matrix of what pbz can certify today (static layout, metadata checks,
synthetic tests, Desktop closed verdicts) versus what stays human (native interactions, visual
acceptance of real data). Use it before promising coverage in a review packet.

## Status and doctor

```
pbz status          # workspace root, projects, mirror sync, Desktop, engine, guard
pbz doctor          # read-only OS-isolation checklist; changes nothing
```

`status` is the start-of-session command. `doctor` never mutates config; treat findings as host
hardening steps ([leak-vectors.md](leak-vectors.md)).

## Init and scan

```
pbz init            # write .pbz/policy.json if missing, scan, first pull
pbz scan            # classify data-bearing / sensitive files
pbz scan --dry-run
```

`init` is required once per workspace before agents edit. Policy defaults are fail-closed for
disclosure; present-but-unusable policy locks down rather than falling open
([improvements.md](improvements.md)).

## Mirror lifecycle (summary)

```
pbz pull [--force|--rebase]
pbz diff [--patch]
pbz push [--dry-run|--reload|--only PATH...]
pbz revert [paths...] [--object "measure 'T'[M]"]
pbz validate
pbz preflight [--only PATH...]
pbz recover [--transaction ID]
```

Full token and push semantics: [tokens.md](tokens.md). Alias/hardlink refusals on `diff --patch`
and `push` are Round-17 honesty rules; see [improvements.md](improvements.md).

## Describe and values

```
pbz describe                     # model overview from the mirror
pbz describe Sales               # one table
pbz describe "[Total Sales]"     # one measure
pbz describe "'Sales'[Amount]"   # one column
pbz values "'Date'[Year]"        # category labels, or a distinct bucket and mirror tokens
```

`describe` reads the sanitized mirror, plus index buckets when they exist. A category column's
labels come from the private index, the same labels the mirror leaves readable. `values` returns
those labels, or a distinct-count bucket and any mirror tokens for that column. Neither command
is a live query grid.

## Handoff and memory scrub

```
pbz handoff            # checklist for the human visual pass
pbz handoff --all
pbz memscrub --dir <folder>            # dry-run scrub plan
pbz memscrub --dir <folder> --apply    # human-only
pbz memscrub --dir <folder> --show <file>  # human-only scrubbed text
```

Agents prepare the checklist; humans confirm visuals. A page id that is not one path segment
is skipped and is not read. `--apply` and `--show` refuse non-human terminals. A missing
private index still scrubs with the pattern rules and reports `name_detection` as
`none (run pbz index first)`. `applied` is whether `--apply` was passed.

## Admin (human-only)

```
pbz admin budget-reset
pbz admin allow <relative-glob>          # add a policy allow entry
pbz admin category <Table[Column]>       # mark a category column
pbz admin synthetic on|off
pbz admin log [N]                        # decrypt last N engine log lines locally
pbz admin consumer-remove <id>
```

Agents receive `HUMAN_ONLY`. Run these in your own interactive terminal after you understand the
policy impact. Prefer fixing a broken `.pbz/policy.json` by hand if lockdown engaged, then re-run
the refusing verb.

## Related

- [workflow.md](workflow.md) — `review`, recipes, evidence stages
- [improvements.md](improvements.md) — consent, preview, freshness, requirements, recover
- [docs/cli-catalog.md](../../../docs/cli-catalog.md) — full verb index
