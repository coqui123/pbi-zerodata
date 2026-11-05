# Visual formatting: `pbz style`

Formatting edits in the mirror, checked against Microsoft's formatting catalog
(`pbz/rules/visual-objects.json`, compacted from `@microsoft/powerbi-core-visual-schema`): every
formatting object and property of the built-in visuals, the visual container objects (title,
subtitle, background, border, shadow, padding, header, tooltip), value types, enumeration members and
instance states. Then `pbz validate` and `pbz push --reload` (a report-only change).

```
pbz style <page> <visual> --set title.text="Hours by status" --set title.fontSize=14 --set labels.show=true
pbz style <page> <visual> --set dataPoint.fill=#118DFF --series "[Total Hours]"   # one series
pbz style <page> <button> --set fill.fillColor=theme:1:-25 --state hover           # one instance state
pbz style <page> --type clusteredColumnChart --set labels.show=true                 # every chart of a type on a page
pbz style --type card --set labels.fontSize=20                                       # every card in the report
pbz style <page> --all --set container.border.show=true                             # every visual on the page
pbz style <page> <visual> --unset labels.show
pbz style <page> <visual> --show                                                     # current formatting
pbz style --list --type lineChart [--object legend]                                  # objects, properties, types
```

- `<visual>` is the visual id (folder name) or its label from `pbz layout --page <page>`.
- Keys are `object.property`. An object that exists both on the visual and on its container
  (background, border, padding, divider, spacing, general) resolves to the container; prefix
  `visual.` or `container.` to choose.
- In bulk mode (`--type`, `--all`) a visual whose type lacks a property is skipped and listed.
- Unknown names get close matches (`did you mean labels.show, ...`).

## Values

| Type | Write | Stored as |
|---|---|---|
| bool | `true` / `false` (also on/off, yes/no) | `true` |
| numeric, font size | `14`, `0.5` | `14D` |
| integer | `2` | `2L` |
| display units | `auto`, `none`, `thousands`, `millions`, `billions`, `trillions` or the number | `1000D` |
| enumeration | one of the listed members (case-insensitive) | `'Top'` or `1D` |
| text, font family, format string | any text without mirror tokens | `'text'` |
| alignment | `left`, `center`, `right` | `'center'` |
| color | `#RRGGBB`, `#RGB`, `theme:<n>[:<shade %>]` (theme color n, -100 darker to 100 lighter), `measure:[Measure]` (field value: the measure returns a color) | `{"solid": {"color": ...}}` |
| text from a field | `measure:[Measure]` | a field-value expression |
| date | `YYYY-MM-DD[THH:MM:SS]` | `datetime'...'` |

Images, conditional formatting rules, error bars, filters and other complex values are refused:
set those in Desktop. Data-point selectors (one bar, one category value) are never written: they
carry data values. Custom visuals accept their contract's object and property names
(`pbz visual import`), with the value type inferred.

## Checks (validate and every push)

| Code | Meaning |
|---|---|
| `PBIR_OBJECT_UNKNOWN` | a built-in visual (or its container) has no formatting object of that name |
| `PBIR_PROPERTY_UNKNOWN` | the object has no property of that name |
| `PBIR_PROPERTY_VALUE` | a literal value does not fit the property's type (for example text in a flag) |

Desktop ignores unknown formatting names, so these are warnings: they catch typos in hand edits.
On a large Desktop-authored report the catalog matched every object, property and value.

Report-wide defaults (colors, fonts for every visual) belong in the theme, not in each visual.
