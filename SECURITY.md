# Security policy

## What this project protects

`pbi-zerodata` (`pbz`) is built so coding agents can edit Power BI PBIP projects **without
company data values entering the agent context**. Safety rests on several layers:

1. **Sanitized mirror** (`pbz-workspace/`) — agents edit tokenized metadata only.
2. **PreToolUse / SessionStart guard** (`pbz/guard.py`) — blocks reads and writes of real
   model/report folders, caches, data files, and policy-denied paths.
3. **Engine broker** (`pbz-engine`) — Desktop checks return closed-vocabulary verdicts, never
   cell values.
4. **Policy fail-closed** — a present but unusable `.pbz/policy.json` locks down disclosure
   rather than falling open.
5. **Human-only gates** — consent approval, review acceptance, admin, and memscrub apply require
   an interactive human terminal.

This is **not** a claim of OS isolation. See
[skills/powerbi-zerodata/reference/leak-vectors.md](skills/powerbi-zerodata/reference/leak-vectors.md)
and run `pbz doctor` for the host checklist.

## Supported versions

Security fixes land on the default branch (`main`). There are no long-lived release trains yet;
upgrade by pulling `main` and re-running `skills/powerbi-zerodata/install/install.py` when hooks
or the engine change.

## Reporting a vulnerability

Please **do not** open a public issue for security problems that could bypass the zero-data
boundary (guard escapes, mirror disclosure of real values, engine leakage, consent/admin bypass).

Report privately via GitHub Security Advisories on this repository (Prefer **Security** →
**Report a vulnerability**), or contact the repository owner through GitHub if advisories are
unavailable.

Include:

- Affected commit or tag
- Reproduction steps that use **synthetic / fixture** data only (never attach company PBIX/PBIP)
- Expected vs actual disclosure boundary
- Whether the path is agent-reachable (hook) or human-only CLI

We will acknowledge receipt and work a fix before any public write-up.

## Safe disclosure practices for contributors

- Never commit real `.pbix`, `.xlsx`, caches, `.pbi/`, or production `pbz-workspace/` trees.
- Prefer the canary fixture under `skills/powerbi-zerodata/tests/fixture/`.
- Do not paste engine query results, Desktop screenshots of populated visuals, or policy file
  contents that might contain allow-list paths to sensitive hosts into issues or PRs.
