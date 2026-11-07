# Synthetic layout preview

`pbz preview` renders sanitized mirror geometry into SVG and a matching JSON review record under
`pbz-workspace/previews/`. It never queries an engine, expands private tokens, copies caches,
or renders company labels and values. Every preview is visibly marked SYNTHETIC.

```text
pbz preview --page Overview --spec pbz-workspace/layout-contract.json
```

The Python entry point is `preview.run(ws, project, page=None, spec=None)`. `spec` accepts a parsed
object or a JSON file contained in the sanitized mirror. A relative path is relative to the mirror.
No arbitrary output path, data payload, URL or unapproved image asset is accepted.

```json
{
  "version": 1,
  "regions": {
    "sidebar": [0, 0, 144, 720],
    "content": [164, 76, 1100, 628]
  },
  "assignments": {"nav_overview": "sidebar"},
  "allow_overlaps": [["kpi_button", "kpi_card"]],
  "min_font_pt": 9,
  "min_region_fill": {"content": 0.8}
}
```

Buttons and slicers are automatically checked against a declared sidebar unless explicitly assigned
elsewhere. Unlike the geometry-only layout lint, overlaps involving buttons, shapes and text boxes
are checked; legitimate click overlays need an explicit pair exemption. Findings cover canvas and
region bounds, undeclared overlaps, approximate text clipping, small fonts, outline proximity and
underused regions. Geometry is not company data. Source text is used only for a fit-risk estimate
and is never placed in SVG or JSON output.

Limits remain explicit:

- Static report images and canvas backgrounds are withheld by default. An image file having been
  registered in a report is not proof that it contains only branding. An optional `asset_receipt`
  contract field accepts only an exact, unchanged, local-human-approved `brand-asset` PNG receipt
  issued by the consent broker. The human must attest that the file is a static asset without
  sensitive data. The renderer strips PNG metadata, validates bounded non-interlaced pixels and
  embeds that inert raster as the synthetic background. `asset_scaling` is `fill` (default) or `fit`.
  It does not automatically prove the asset matches the native report background binding. Unknown,
  changed, unapproved or wrong-purpose receipts fail closed. No other report images are revealed.
- Custom visual rendering, including Deneb compilation, is UNSUPPORTED. Its placeholder is not
  native visual acceptance. No custom expression or network data source is executed.
- Rendering uses invented content and approximate font/style metrics. Grouped visual positions are
  marked approximate. It cannot certify actual-label fit, native table autosizing, tooltips,
  drill-through, native button states or accessibility.
- `native_visual_acceptance` always remains `NOT_RUN`. A geometric PASS means no detected layout
  contract errors; it never means a production screenshot or interaction passed.
- Output paths, input contracts, and symlinks must remain contained in the mirror. No screenshots
  or raw real-model values are exposed through this feature.
