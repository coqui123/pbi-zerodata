# Offline execution and rollout

The normal inventory is `skills/powerbi-zerodata/tests/offline_suites.json`.
`offline_baseline.json` independently freezes the old hosted suite IDs and argv,
including the separate synth provenance invocation. Do not regenerate the baseline
from the manifest. Only `test_e2e.py` is excluded as a whole script. Timing weights
are median suite seconds computed by `tests/ci_weights.py` from hosted serial
report JSON (five green reports at the last refresh, rounded to tenths with a
0.1 floor); they live only in `offline_suites.json`, while
`offline_additions.json` carries ids and argv only. A suite with no measured
samples carries no weight and defaults to one. Refresh by rerunning the tool
on at least five green serial reports and applying its output to
`offline_suites.json` with `apply_weights`. Allocation is
longest-first, serial inside each fresh Windows VM, with a separate publish per VM.
No engine artifact transfer is used.

Run `python tests/run_offline.py` from the skill directory. `--check` validates
registration without executing suites; `--shard 0 --shards 2` selects a shard.
Each suite runs in a fresh Python process through `ci_suite.py`, which preserves
script argv and captures unittest skip reasons. `PBZ_SUITE` records actual
executions, exits, errors, skips and durations. Case counts are deliberately not
an equivalence criterion: existing standalone scripts have incompatible counters.
The runner scans the checkout, captured logs and explicitly designated output
roots in its finalizer. Read/enumeration errors and missing roots fail the scan.
Fixture exclusion uses path components. Console output and report artifacts are
redacted; raw captured logs remain in scan coverage and are not uploaded.

The authoring and governance suites print `PBZ_TIMING`, separately measuring real
Desktop process discovery and TOM calls, including unsuccessful calls. Their
fixtures and discovery results have not changed. Linux stub measurements are not
Windows performance evidence.

## Shadow evidence

`offline-shadow.yml` is dispatch-only. Dispatch `serial` and `sharded` modes on
exactly the same ref/SHA, retaining each run ID. Each path has its own final
aggregate, so measure run creation through aggregate completion, including setup,
scan and aggregation. There is no shared mutable checkout or engine payload.
Shard jobs have a 25-minute timeout and matrix fail-fast is disabled. The
aggregate runs even after failure and rejects missing or cancelled jobs. Shadow
and production concurrency groups differ; main runs cannot cancel predecessors.

Download the serial and both shard JSON reports into a pair directory, then run:

```sh
python tests/ci_equivalence.py evidence/pair-1
```

The comparator checks the independent inventory, exact multiplicity, argv, exits,
errors, skip reasons, completed scans and final verdicts. It compares recorded
OS/image, Python, SDK, Python dependencies, resolved NuGet packages and engine
source/project identity. Separate engine binary hashes are recorded, not assumed
equal. `offline_skips.json` starts empty and fails closed. Any existing Windows
skip allowance needs independent baseline evidence and review; agreeing new skips
on both paths are rejected.

Collect completed-run metrics with:

```sh
python tests/ci_hosted_metrics.py OWNER/REPO RUN_ID --observed-billed-minutes MINUTES
```

This reads GitHub run/job metadata with `gh api`. Supply actual observed billed
minutes from hosted billing data. The separately printed per-job rounding figure
is an estimate and is never substituted for observed billing. Cancelled attempts
are not evidence and must be retained separately in `cancelled_attempts`.

Retain at least ten distinct completed pairs and one pair dispatched with
`cold_cache: true` on both paths. That input clears pip and NuGet caches. Pair
records embed `reference`, `shards`, `serial_metrics`, and `sharded_metrics`, plus
`pair_id`, `sha`, `serial_run`, `sharded_run`, `cold_cache`, `equivalent`,
`serial_seconds`, `sharded_seconds`, `serial_billed_minutes`, and
`sharded_billed_minutes`. The rollout gate rechecks the embedded reports and
collected metric identities/values. It requires a median elapsed reduction of at
least 30%, no worse maximum, and at most 50% more total billed runner-minutes.
The plan's elapsed and cost projections remain estimates, not measured results.

For expected-failure experiments, the shadow `controls` input names a checked-in
JSON array. Each record has `id`, `argv`, `purpose` (`historical` or `injected`),
nonzero `exit_code`, and a specific output `reason`. A historical record replaces
a known suite ID with the known failing historical script copied into the current
`tests/` directory under a name that does not start with `test_`; this preserves
its path calculations and keeps it outside normal discovery. Run that script with
the current runner on both paths. An injected record adds a distinct suite ID;
`tests/test_ci_registration.py --injected-failure` provides an explicit failure control. The comparator
requires both the expected exit and specific reason, not just two red verdicts.
A historical revision and expected failure have not been selected in this Linux
session; do not manufacture historical acceptance evidence.

Rollout evidence includes `historical_failure` and `injected_failure` objects,
each containing `run_id`, `controls`, `reference`, and `shards`; the historical
object also requires its full `historical_revision` SHA. The comparator verifies
these expected failures. Control runs remain red at their final aggregate.

For the canary experiment, select `canary_shard` in shadow dispatch. Choose the
shard whose allocation does not contain engine-offline. The runner plants a
fixture canary in its output root; that shard and the aggregate must fail. Retain
`run_id`, `reference`, `shards`, and `aggregate_verdict: failure` under
`canary_without_engine`. The gate checks the failed scan and verifies the complete
executed inventory after normalizing only the planted scan failure.

## Activation and rollback

Production keeps the stable `offline` aggregate check. It defaults to serial,
whose aggregate independently verifies actual coverage and scans without
inventing a historical skip allowance.
After hosted evidence passes, commit the reviewed evidence as
`.github/offline-rollout-evidence.json`; setting repository variable
`PBZ_OFFLINE_MODE=sharded` enables the gated two-shard path. An absent or invalid
evidence file refuses activation. Clearing that variable switches the same
required check back to serial, without requiring a branch-protection change.
Branch protection itself must still require this check; workflow source alone
cannot prove that setting.

The hook installer supports `--required-ci-verified` as an explicit attestation
that hosted offline is required on main. Only after that verification does the
pre-commit hook stop after fast staged checks. Until then it retains the full
engine/offline gate. Both modes first check the staged tree without changing the
index or working tree. Pre-push always publishes and runs the full offline suite.
Testing pushed commits in a separate clean tree remains the plan's later work.

## Installer verification

JSON settings, Codex TOML and rule-file operations lock the entire per-file
read/merge/backup/replace transaction. Persistent `.pbz-lock` sidecars coordinate
installers; unique same-directory temporary files prevent writer collisions.
Interrupted writes leave the destination intact. Host-added keys or nested tables
inside `mcp_servers.pbz` cause a named refusal on apply/update/uninstall, leaving
that file unchanged. These locks coordinate cooperating installers, not arbitrary
host editors, and are not a multi-file transaction.

Acceptance tests were added to existing test files: CI machinery in
`test_ci_registration.py`, scanner controls in `test_engine.py`, and installer and
hook behavior in `test_mcp.py`. No new test suite file was created or existing test
removed. Installer filesystem/concurrency fixtures run in normal Windows CI too.
