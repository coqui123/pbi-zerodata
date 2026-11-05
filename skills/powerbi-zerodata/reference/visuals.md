# Visual types and custom visuals: `pbz visual`, `pbz add visual`

## Built-in types

`pbz add visual <type>` accepts every built-in visual type in Microsoft's core visual capabilities
(`@microsoft/powerbi-core-visual-schema`, compacted into `pbz/rules/visuals.json` by
`install/build_visual_catalog.py`): cards (`card`, `cardVisual`, `multiRowCard`, `kpi`, `gauge`),
tables (`tableEx`, `pivotTable`), slicers (`slicer`, `listSlicer`, `textSlicer`, `advancedSlicerVisual`),
charts, maps, `textbox`, shapes and buttons. `pbz visual roles <type>` prints a type's data roles
(internal names, kind grouping / measure / either) and the field-count combinations it accepts.

- A field goes to the first role that fits: measures to measure roles, numeric columns to a measure
  role as an implicit sum, other columns to grouping roles. `ROLE=field` picks the role.
- Field counts are checked against the visual's data view mapping conditions (for example a card
  takes one field). Images need a registered resource: add them in Desktop.

## Custom visuals

A custom visual is a visual whose `visualType` is the package's `visual.guid` (an identifier such as
`deneb7E15AEF80B9E4D4F8E12924291ECE89A`), plus a registration in `definition/report.json`:

| Kind | Registration | Package files |
|---|---|---|
| AppSource (public) | guid in `publicCustomVisuals` | none: Desktop downloads it on open (needs internet; tenant settings can block it) |
| Private (.pbiviz) | `resourcePackages` entry `{"name": <guid>, "type": "CustomVisual", "items": [{"name": "<guid>.pbiviz.json", "path": "<guid>.pbiviz.json", "type": "CustomVisualMetadata"}]}` | `<Report>/CustomVisuals/<guid>/package.json` and `resources/<guid>.pbiviz.json` |
| Organizational store | `resourcePackages` entry named `<guid>_OrgStore`, type `OrganizationalStoreCustomVisual`; the visual's `visualType` carries the same suffix | none; resolves in your tenant only |

The root `organizationCustomVisuals` array alone does not make a visual render. A visual whose type
is not registered shows a placeholder in Desktop.

Data roles in `queryState` are the internal `capabilities.dataRoles[].name` values (Deneb's Values
well is `dataset`), not display names.

```
pbz visual list                                   # built-in types in use, custom visuals: registration,
                                                  #   instances, package, where roles come from
pbz visual import <file.pbiviz>                   # contract only: roles, kinds, conditions
pbz visual import <file.pbiviz> --as public       # + guid in publicCustomVisuals (AppSource visual)
pbz visual import <file.pbiviz> --as private      # + resourcePackages entry; the package is staged and
                                                  #   installed into CustomVisuals/ by the next push
pbz visual register <guid> --as public|organizational [--role NAME:grouping|measure|either ...]
pbz visual roles <type or guid>
pbz add visual <guid or imported name> --page <p> --field ROLE=ref ...
```

- Contracts live in `.pbz/visuals/<guid>.json` (names, kinds, conditions, object names only).
- Without a contract, roles used by existing instances of the type in the report are accepted
  (`roles_from: observed`, kinds unknown).
- `pbz add visual` registers a visual with a known contract automatically (`register_as` from the import).
- The .pbiviz is treated as an untrusted zip (no path traversal, links, duplicates or oversize
  entries) and its code is never run, shown or mirrored: the mirror skips `CustomVisuals/`.
- Spec-driven visuals (Deneb) need their spec written in Desktop; until then they show a landing page.

## Checks (validate and every push)

| Code | Severity | Meaning |
|---|---|---|
| `PBIR_VISUAL_TYPE_UNREGISTERED` | warning | visualType is neither built-in nor registered |
| `PBIR_PRIVATE_VISUAL_PACKAGE_MISSING` | error | a CustomVisual package is registered but its files are missing |
| `PBIR_ORGSTORE_NAME` | error | an organizational package without the `_OrgStore` suffix |
| `PBIR_CUSTOM_VISUAL_CONFLICT` | error | one guid registered as two kinds |
| `PBIR_ROLE_UNKNOWN` | warning | a built-in visual binds a role its type does not have |

Offline checks cannot prove a custom visual renders (a wrong but registered guid validates): the user
confirms it in Desktop (`pbz handoff`).
