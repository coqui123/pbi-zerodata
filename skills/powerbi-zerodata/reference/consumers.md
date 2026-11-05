# External consumers: `pbz consumers`

`pbz unused` reads the model and the local reports. Anything else that uses the semantic model
(thin reports in the service, Excel workbooks, paginated reports, composite models, notebooks and
other XMLA clients) is invisible to it unless you register it. Registered consumers feed the usage
graph of `unused`, `lineage`, `suggest`, `bpa` and `docs`.

## Rules

- **Evidence only keeps.** An object a consumer references becomes `used_external` (and so do its
  dependencies). `unused --remove` never touches it.
- **Missing coverage blocks deletion.** A consumer that is missing, unreadable or opaque (a paginated
  report with an expression-built query, a scanned service report whose definition is not registered)
  marks the analysis `incomplete`: unused objects show as `unknown` and `unused --remove` refuses with
  `INCOMPLETE`.
- **Only model names come out.** pbz reads consumer files inside its own process. It keeps an
  identifier only when it names a table, column, measure, hierarchy or level of this model. Member
  keys and member names, cached pivot items, cell values, parameter values, query and filter literals,
  connection strings, scanner users and expressions are never stored or printed. Names that match
  nothing are only counted (`unresolved`).
- Unregistered consumers stay invisible: ask the user which exist before a removal.

## Commands

```
pbz consumers add <file or folder> [--kind auto|pbir|pbix|xlsx|rdl|model|queries|scan] [--label L]
                  [--model-name "<name in the service>"]
pbz consumers add --object "[Measure]" --object "'Table'[Column]" --label "Board pack"   # manual
pbz consumers list            # status, coverage, binding, objects, unresolved count, gaps
pbz consumers scan            # re-read every consumer now (normally re-read when its file changes)
pbz consumers show <id>       # this model's objects the consumer uses, and how (pivot, slicer, ...)
pbz admin consumer-remove <id>   # the user only: removing a consumer loosens protection
```

The registry is `.pbz/consumers.json`; agents cannot edit it directly (guard), only add to it. The
parsed evidence is cached in the private store and refreshed when a file or the model changes.

## What each kind reads

| Kind | Source | Reads (names only) | Never reads |
|---|---|---|---|
| `pbir` | a `*.Report` folder or a folder of them (PBIP export, Fabric Git) | visual, filter, sort and bookmark field references, report-level measure DAX, `definition.pbir` binding; PBIR-Legacy `report.json` | literals in filters, slicers, objects |
| `pbix` | downloaded thin report (.pbix / .pbit) | `Report/definition/**` or the legacy `Report/Layout` (JSON inside strings included), `Connections` (dataset binding, report id) | `DataModel` (the data), literals |
| `xlsx` | Excel (.xlsx / .xlsm) connected to the model | OLAP PivotTables: placed fields (`cacheField` names), hierarchies with `fieldsUsage`, PivotTable row / column / page hierarchy usage, calculated members and sets; OLAP slicers and timelines; CUBE formulas (connection checked, member keys cut); DAX query tables | `pivotCacheRecords`, `sharedItems`, cell values, shared strings, `xl/model` (the workbook's own data model), pivots on worksheet ranges |
| `rdl` | paginated report or shared dataset (.rdl / .rsd) | `CommandText` (DAX and MDX), `DataField` unique names, data source binding | parameters and defaults, filters, embedded images; an `=` expression query is a coverage gap |
| `model` | downstream composite model (`*.SemanticModel`) | tables with DirectQuery entity partitions onto a semantic model, their source columns and measures, identifiers in its DAX | Direct Lake and other sources |
| `queries` | query-text export (Log Analytics or workspace monitoring `EventText`, trace text; .csv .json .txt .dax .mdx) | DAX and MDX identifiers | every literal; evidence covers only the exported window, so silence proves nothing |
| `scan` | admin scanner result (`getInfo` with lineage) | which reports, paginated reports, composite models and Q&A tiles consume the model | expressions, RLS filters, users, data source details; field use still needs each consumer's definition |
| `manual` | `--object` declarations | the declared objects | |

Excel pivot caches list every hierarchy of the cube; only placed fields count, so a column that a
workbook merely could use stays unused. `[Measures].[Sum of X]`-style implicit measures keep column X.

## Getting consumer definitions without service rights

- Thin reports: "Download this file" in the service (.pbix), or the workspace's Git integration (PBIR
  folders), or the report owner's PBIP.
- Excel: the workbooks themselves (OneDrive / SharePoint copies).
- Paginated reports: download the .rdl.
- Composite models: their PBIP or Git folder.
- Excel and other XMLA clients you cannot enumerate: a Log Analytics or workspace monitoring export
  of query text (needs Premium or Fabric capacity and access to the logs).
- Everything at once: a Fabric admin can run the scanner API (`getInfo?lineage=True`) and hand you
  the JSON; register it as `scan` to see which consumers still lack definitions.

## States in `pbz unused`

`used` (local reports), `used_external` (registered consumers), `locked`, `used_by_unused`,
`unused`, `unknown`. Each `used_external` record names its consumers (or says it is a dependency of
one). `scope.consumers` lists every consumer with its status; `scope.external_coverage` is
`none registered`, `registered consumers complete` or `incomplete`.

`pbz rename` warns (`USED_EXTERNALLY`) when a registered consumer uses the renamed object: thin
reports, Excel and RDL bind by name and break after a rename.
