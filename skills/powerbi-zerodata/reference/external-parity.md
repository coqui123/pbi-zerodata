# External parity contract

Requirements for parity with DAX Studio, Tabular Editor and Measure Killer.

## Sources

The research-plan appendices `daxstudio-features.md`, `tabular-editor-features.md`,
`measure-killer-features.md` and `pbz-current-capabilities.md` are not in this repo, so feature
counts were not re-derived from them.

## Storage

Storage statistics belong on the memory verb, not the value index.

## Custom BPA

Custom BPA is a safe expression subset, not full Dynamic LINQ. The identifiers `Vertipaq`,
`Cardinality`, `RowCount`, `DistinctCount`, `DataSize`, `UsedSize`, `SegmentCount`,
`Statistics`, `Min` and `Max` are refused before eval.

## Rewrites

A DIVIDE rewrite requires an explicit accept-semantic flag.

## Human gates

Performance Analyzer replay and real-model RLS are human-gated. Synthetic models are the
default for RLS.

## Out of scope

Rejected and still out of scope: raw timings, raw VPAX, query grids, tenant scans, XMLA
deploy, and arbitrary C# scripts.

## Disclosure

Stdout never includes raw milliseconds, row counts, xmSQL, plan text, referential-integrity
key samples, min/max, UPNs, or query text.

## OpenLineage

OpenLineage output is a local file. No network.

## Translations

Translation interchange is a flat table of culture, object type, table, object, property, and
value.
