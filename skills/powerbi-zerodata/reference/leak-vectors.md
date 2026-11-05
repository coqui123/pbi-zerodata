# Leak vectors and controls

The guarantee: no value read from a data source reaches the agent. Authored literals reach it
only when classified non-sensitive. Real-data feedback is limited to verdicts, error classes and
fixed coarse buckets under a budget. This is accident prevention on native Windows (hooks,
deny rules, a broker CLI), not an OS security boundary.

| Vector | Control |
|---|---|
| `.pbi\cache.abf`, `cache.abf.stale`, `unappliedChanges.json`, `localSettings.json` | Read deny rules (Claude) + guard everywhere |
| `DAXQueries\*.dax`, `TMDLScripts\` | not mirrored; guard denies in workspaces |
| `cultures\*.tmdl` | mirrored: the Q&A linguistic schema is one opaque payload token; translated captions, descriptions and folders are screened as free text |
| Literals in general | `"literals": "strict"` in `.pbz/policy.json` tokenizes every literal except known category labels, model / source-column names and format codes; only the user can remove it |
| Synthetic test model (`pbz synth`) | built from the mirror only, fake rows, outside the workspace; `synth-query` returns rows only for a model carrying the HMAC marker of its own lineage tags, and push refuses that marker in a real model |
| Desktop app data: TempSaves, Backups, AutoRecovery, AnalysisServicesWorkspaces, Traces | guard denies `~\Microsoft\Power BI Desktop Store App\` and `%LOCALAPPDATA%\Microsoft\Power BI Desktop\` |
| Measure Killer: `*.measurekiller` analysis files (user emails, column sizes), `%APPDATA%\Measure Killer\` (activity logs, save files, exports, deletion backups) | guard everywhere; Read deny rules after the next `install.py` run |
| Source files, dumps, exports, images, PDFs, Office docs in the workspace | guard default denies + `pbz scan` policy entries |
| Filter / slicer / bookmark / selector literals in PBIR | tokenized in the mirror (column-bound) |
| DATATABLE, `#table`, Enter Data blobs, RLS members, M literals | tokenized or payload markers |
| Figures and names in descriptions, comments, titles, text boxes | free-text scan (`«num»`, `«lit»`, `«pii»`) |
| DAX query results, counts, distinct values | only via pbz-engine verdicts |
| Engine / DMV statistics (`DISCOVER_STORAGE_*`, sessions, traces) | engine allows `TMSCHEMA_*` and `DBSCHEMA_CATALOGS` only |
| Error text echoing values ("duplicate value 'X'", `Details:`) | engine emits classes; raw text DPAPI-encrypted, human-only log |
| Boolean oracle / bisection | no `between`; a ledger keeps distinct constants per target for 30 days, including literals the agent pushed into measures the target reaches; daily budgets for checks, probes, snapshot queries and smoke runs; opaque group keys |
| Timing side channel | no durations printed |
| ADOMD / XMLA / executeQueries / OData / clipboard from shells or scripts | guard command and script scanning |
| Power BI MCP servers, bridge `screenshot`, report-author `preview` | guard denies |
| Screenshots, UI automation, browser page reads | guard denies in workspaces; no twin |
| Memory / notes / transcripts | memory-write guard + `pbz memscrub`; SKILL.md rule 4 |
| Agent rewriting the guard or policy | self-protection: skill, hooks, settings and rule files read-only from workspace sessions; policy edits may only tighten |

Residual risks: a novel indirect script can dodge pattern checks; hooks have had firing bugs;
Desktop auto-updates may move folders or bump schemas (rerun the canary tests); table and column
names derived from pivoted data stay visible as metadata; heuristic classification can miss a
lone first name until `pbz index` runs; answers to many allowed checks can still narrow facts
about the data over time (the ledger slows this, it cannot make it impossible).

## Hardening tier: OS-level isolation (for the user to set up)

The hook and pbz are defence in depth inside one Windows account. To make "the agent cannot read a
protected byte" hold even against a program the agent writes, put an OS boundary around the agent.
pbz never changes these settings itself; they are security settings for the machine owner.

1. **Separate account.** Create a standard (non-admin) local account for agent sessions and run the
   agent CLI only there (`runas /user:<agent-account>` or a separate Windows login).
2. **NTFS deny ACLs** for that account on the real data: the workspace's real `*.SemanticModel` and
   `*.Report` folders, every data file location, `%USERPROFILE%\.pbz-private`, the Power BI Desktop
   app data folders and Measure Killer's `%APPDATA%\Measure Killer`. Grant it read / write on
   `pbz-workspace\` only.
3. **Broker.** pbz itself must keep its access: run `pbz` as the data owner (for example a small
   scheduled-task or service wrapper the agent account can invoke, or a named pipe), so the agent
   account only ever sees pbz's closed-vocabulary output.
4. **Network.** Block the agent account from Power BI and Fabric endpoints (`*.powerbi.com`,
   `*.analysis.windows.net`, `*.fabric.microsoft.com`) and from your OData hosts with Windows
   Defender Firewall rules scoped to the account or the agent's executable.
5. **Disposable alternative.** Run the agent inside Windows Sandbox or a VM that holds only the
   mirror folder and reaches pbz through a shared broker endpoint.

Check it: from the agent account, try to read a real TMDL file, `cache.abf`, the private store and a
data file. Every attempt must fail at the OS level, independent of the hook.
