# Leak vectors and controls

The guarantee: no value read from a data source reaches the agent. Authored literals reach it
only when classified non-sensitive. Real-data feedback is limited to verdicts, error classes and
fixed coarse buckets under a budget. This is accident prevention on native Windows (hooks,
deny rules, a broker CLI), not an OS security boundary.

| Vector | Control |
|---|---|
| `.pbi\cache.abf`, `cache.abf.stale`, `unappliedChanges.json`, `localSettings.json` | Read deny rules (Claude) + guard everywhere |
| `DAXQueries\*.dax`, `TMDLScripts\`, `cultures\*.tmdl` (linguistic) | not mirrored; guard denies in workspaces |
| Desktop app data: TempSaves, Backups, AutoRecovery, AnalysisServicesWorkspaces, Traces | guard denies `~\Microsoft\Power BI Desktop Store App\` and `%LOCALAPPDATA%\Microsoft\Power BI Desktop\` |
| Measure Killer: `*.measurekiller` analysis files (user emails, column sizes), `%APPDATA%\Measure Killer\` (activity logs, save files, exports, deletion backups) | guard everywhere; Read deny rules after the next `install.py` run |
| Source files, dumps, exports, images, PDFs, Office docs in the workspace | guard default denies + `pbz scan` policy entries |
| Filter / slicer / bookmark / selector literals in PBIR | tokenized in the mirror (column-bound) |
| DATATABLE, `#table`, Enter Data blobs, RLS members, M literals | tokenized or payload markers |
| Figures and names in descriptions, comments, titles, text boxes | free-text scan (`«num»`, `«lit»`, `«pii»`) |
| DAX query results, counts, distinct values | only via pbz-engine verdicts |
| Engine / DMV statistics (`DISCOVER_STORAGE_*`, sessions, traces) | engine allows `TMSCHEMA_*` and `DBSCHEMA_CATALOGS` only |
| Error text echoing values ("duplicate value 'X'", `Details:`) | engine emits classes; raw text DPAPI-encrypted, human-only log |
| Boolean oracle / bisection | no `between`, 3 constants per target, daily budget, opaque group keys |
| Timing side channel | no durations printed |
| ADOMD / XMLA / executeQueries / OData / clipboard from shells or scripts | guard command and script scanning |
| Power BI MCP servers, bridge `screenshot`, report-author `preview` | guard denies |
| Screenshots, UI automation, browser page reads | guard denies in workspaces; no twin |
| Memory / notes / transcripts | memory-write guard + `pbz memscrub`; SKILL.md rule 4 |
| Agent rewriting the guard or policy | self-protection: skill, hooks, settings and rule files read-only from workspace sessions; policy edits may only tighten |

Residual risks: a novel indirect script can dodge pattern checks; hooks have had firing bugs;
Desktop auto-updates may move folders or bump schemas (rerun the canary tests); table and column
names derived from pivoted data stay visible as metadata; heuristic classification can miss a
lone first name until `pbz index` runs. Hardening tier (later): run agents under a separate
Windows account with NTFS deny ACLs on data paths and reach the engine through a named-pipe broker.
