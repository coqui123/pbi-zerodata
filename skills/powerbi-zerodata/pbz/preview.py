"""Synthetic-only SVG layout previews. Never queries an engine or expands private tokens.

The renderer deliberately substitutes invented labels and values, including in titles. It is
an approximation of PBIR geometry, not a Power BI screenshot or a native visual renderer.
Images are placeholders unless an exact-file human-approved PNG brand receipt is supplied.
"""
from __future__ import annotations

import hashlib
import base64
import html
import json
import math
import os
from pathlib import Path
import re

from .common import PbzError, commit_bytes, write_text

VERSION = 1
CONTROL_TYPES = {"actionButton", "slicer", "advancedSlicerVisual"}
TABLE_TYPES = {"tableEx", "pivotTable"}
CARD_TYPES = {"card", "cardVisual", "kpi", "multiRowCard"}
CHART_TYPES = {"barChart", "columnChart", "clusteredBarChart", "clusteredColumnChart",
               "lineChart", "areaChart", "stackedAreaChart", "pieChart", "donutChart",
               "lineClusteredColumnComboChart", "lineStackedColumnComboChart", "scatterChart"}
KNOWN_TYPES = CONTROL_TYPES | TABLE_TYPES | CARD_TYPES | CHART_TYPES | {
    "textbox", "shape", "basicShape", "image", "group"}
SPEC_KEYS = {"version", "regions", "assignments", "allow_overlaps", "min_font_pt", "min_region_fill", "asset_receipt", "asset_scaling"}


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise PbzError("UNSAFE_PATH", "preview paths must stay inside the sanitized mirror")
    return resolved


def _read(path: Path, mirror: Path) -> dict:
    _inside(path, mirror)
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise PbzError("BAD_JSON", "preview requires valid sanitized PBIR metadata")
    if not isinstance(obj, dict):
        raise PbzError("BAD_JSON", "preview requires JSON objects")
    return obj


def _write(path: Path, text: str, mirror: Path):
    _inside(path, mirror)
    parent = path.parent
    if parent.exists():
        _inside(parent, mirror)
    write_text(str(path), text)


def _number(v, default=0.0) -> float:
    if isinstance(v, bool):
        return default
    try:
        n = float(v)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default


def _rect(value, what="region") -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
        raise PbzError("BAD_REQUEST", f"{what} is [x,y,width,height] with finite numbers")
    x, y, w, h = map(float, value)
    if w <= 0 or h <= 0:
        raise PbzError("BAD_REQUEST", f"{what} dimensions must be positive")
    return x, y, w, h


def _contract(spec, mirror: Path) -> dict:
    if isinstance(spec, (str, os.PathLike)):
        path = Path(spec)
        spec = _read(path if path.is_absolute() else mirror / path, mirror)
    if spec is None:
        spec = {}
    if not isinstance(spec, dict) or set(spec) - SPEC_KEYS:
        raise PbzError("BAD_REQUEST", "unknown preview contract fields; data and assets are not accepted")
    if isinstance(spec.get("version", VERSION), bool) or spec.get("version", VERSION) != VERSION:
        raise PbzError("BAD_REQUEST", "unsupported preview contract version")
    out = dict(spec)
    for key in ("regions", "assignments", "min_region_fill"):
        if not isinstance(out.get(key, {}), dict):
            raise PbzError("BAD_REQUEST", f"{key} must be an object")
    out["regions"] = {name: _rect(value) for name, value in out.get("regions", {}).items()
                      if isinstance(name, str)}
    if len(out["regions"]) != len(spec.get("regions", {})):
        raise PbzError("BAD_REQUEST", "region names must be strings")
    for visual, region in out.get("assignments", {}).items():
        if not isinstance(visual, str) or not isinstance(region, str) or region not in out["regions"]:
            raise PbzError("BAD_REQUEST", "assignments map visual identifiers to declared regions")
    pairs = out.get("allow_overlaps", [])
    if not isinstance(pairs, list) or any(not isinstance(p, list) or len(p) != 2 or
                                          any(not isinstance(x, str) for x in p) for p in pairs):
        raise PbzError("BAD_REQUEST", "allow_overlaps contains pairs of visual identifiers")
    for region, fraction in out.get("min_region_fill", {}).items():
        if region not in out["regions"] or isinstance(fraction, bool) or not isinstance(fraction, (int, float)) \
                or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise PbzError("BAD_REQUEST", "min_region_fill maps declared regions to fractions from zero to one")
    font = out.get("min_font_pt", 9)
    if isinstance(font, bool) or not isinstance(font, (int, float)) or not math.isfinite(font) or not 1 <= font <= 100:
        raise PbzError("BAD_REQUEST", "min_font_pt must be a finite font size")
    out["min_font_pt"] = font
    if 'asset_receipt' in out and (not isinstance(out['asset_receipt'], str) or not re.fullmatch(r'[0-9a-f]{32}', out['asset_receipt'])):
        raise PbzError('BAD_REQUEST', 'asset_receipt must be an exact approved consent identifier')
    if out.get('asset_scaling', 'fill') not in ('fit', 'fill') or ('asset_scaling' in out and 'asset_receipt' not in out):
        raise PbzError('BAD_REQUEST', 'asset_scaling requires an asset receipt and is fit or fill')
    return out


def _literal(obj):
    if isinstance(obj, dict):
        raw = obj.get("expr", {}).get("Literal", {}).get("Value")
        if isinstance(raw, str):
            if raw.startswith("'") and raw.endswith("'"):
                return raw[1:-1].replace("''", "'")
            if raw in ("true", "false"):
                return raw == "true"
            return raw.rstrip("DL")
    return obj if isinstance(obj, (str, int, float, bool)) else None


def _font(value, default=9.0):
    value = _literal(value)
    if isinstance(value, str):
        value = re.sub(r"(?:pt|px)$", "", value)
    return _number(value, default)


def _formatting(obj, name):
    visual = obj.get("visual", {})
    result = {}
    for section in ("objects", "visualContainerObjects"):
        for item in visual.get(section, {}).get(name, []):
            if not item.get("selector") or item.get("selector", {}).get("id") == "default":
                result.update(item.get("properties", {}))
    return result


def _font_and_length(obj, kind):
    """Use text lengths to flag fit risks, but never emit source text into artifacts."""
    font = 18.0 if kind == "textbox" else 10.0
    lengths = []
    title = _formatting(obj, "title")
    if _literal(title.get("show")) is not False:
        raw = _literal(title.get("text"))
        if isinstance(raw, str):
            lengths.append(len(raw))
            font = _font(title.get("fontSize"), font)
    if kind == "textbox":
        for item in obj.get("visual", {}).get("objects", {}).get("general", []):
            for paragraph in item.get("properties", {}).get("paragraphs", []):
                runs = paragraph.get("textRuns", [])
                lengths.append(sum(len(str(run.get("value", ""))) for run in runs))
                for run in runs:
                    font = max(1.0, _font(run.get("textStyle", {}).get("fontSize"), font))
    elif kind in CONTROL_TYPES:
        text = _formatting(obj, "text")
        font = _font(text.get("fontSize"), font)
        label = _literal(text.get("text"))
        if isinstance(label, str):
            lengths.append(len(label))
    return font, max(lengths, default=0)


def _hex_color(value, default):
    if isinstance(value, dict):
        value = value.get('solid', {}).get('color')
    value = _literal(value)
    return value if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value) else default


def _style(obj, kind):
    fill = _formatting(obj, 'fill')
    outline = _formatting(obj, 'outline')
    text = _formatting(obj, 'text')
    shown = _literal(outline.get('show')) is not False
    weight = max(0, min(50, _number(_literal(outline.get('weight')), 1))) if shown else 0
    if _number(_literal(outline.get('transparency')), 0) >= 100:
        weight = 0
    return {'fill': _hex_color(fill.get('fillColor'), '#253260' if kind == 'actionButton' else '#FFFFFF'),
            'ink': _hex_color(text.get('fontColor'), '#FFFFFF' if kind == 'actionButton' else '#253260'),
            'stroke': _hex_color(outline.get('lineColor'), '#C9D0DC'), 'weight': weight,
            'fill_opacity': max(0, min(1, 1 - _number(_literal(fill.get('transparency')), 0) / 100))}


def _overlap(a, b):
    x = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    y = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0, x) * max(0, y)


def _contains(a, b):
    return b[0] >= a[0] and b[1] >= a[1] and b[0] + b[2] <= a[0] + a[2] + 1e-6 \
        and b[1] + b[3] <= a[1] + a[3] + 1e-6


def _union_area(rects):
    xs = sorted({r[0] for r in rects} | {r[0] + r[2] for r in rects})
    area = 0
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((y, y + h) for x, y, w, h in rects if x < right and x + w > left)
        end = -math.inf
        covered = 0
        for start, stop in intervals:
            covered += max(0, stop - max(start, end))
            end = max(end, stop)
        area += (right - left) * covered
    return area


def _svg(vis, width, height, regions, background=None, scaling='fill'):
    # No source title, category, value or expression enters SVG. Only the explicitly approved,
    # metadata-stripped static PNG may enter as a data URI; report images stay withheld.
    esc = lambda s: html.escape(str(s), quote=True)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}" height="{height:g}" '
             f'viewBox="0 0 {width:g} {height:g}" role="img" aria-label="Synthetic layout preview">',
             '<rect width="100%" height="100%" fill="#F4F6FA"/>']
    if background is not None:
        encoded = base64.b64encode(background).decode('ascii')
        fit = 'meet' if scaling == 'fit' else 'slice'
        parts.append(f'<image x="0" y="0" width="{width:g}" height="{height:g}" preserveAspectRatio="xMidYMid {fit}" href="data:image/png;base64,{encoded}"/>')
    parts.append('<defs>')
    for i, v in enumerate(vis):
        x, y, w, h = v["box"]
        parts.append(f'<clipPath id="clip{i}"><rect x="{x:g}" y="{y:g}" width="{max(0,w):g}" height="{max(0,h):g}"/></clipPath>')
    parts.append('</defs>')
    for name, (x, y, w, h) in regions.items():
        fill = '#253260' if name == 'sidebar' and background is None else 'none'
        parts.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" fill="{fill}" stroke="#A0A9B8" stroke-dasharray="4 4"/>')
    for i, v in enumerate(vis):
        if v["hidden"] or v["kind"] == "group":
            continue
        x, y, w, h = v["box"]
        if w <= 0 or h <= 0:
            continue
        kind = v["kind"]
        label = ("Synthetic metric" if kind in CARD_TYPES else "Synthetic table" if kind in TABLE_TYPES else
                 "Synthetic filter" if "slicer" in kind.lower() else "Navigation" if kind == "actionButton" else
                 "Synthetic heading" if kind == "textbox" else "Image withheld" if kind == "image" else
                 "Unsupported visual" if kind not in KNOWN_TYPES else "Synthetic chart")
        style = v['style']
        fill, ink = style['fill'], style['ink']
        parts.append(f'<g clip-path="url(#clip{i})"><rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" '
                     f'fill="{fill}" fill-opacity="{style["fill_opacity"]:g}" stroke="{style["stroke"]}" stroke-width="{style["weight"]:g}"/>')
        font = min(30, max(8, v["font"] * 4 / 3))
        parts.append(f'<text x="{x+8:g}" y="{y+min(h-3,font+7):g}" font-family="Segoe UI, sans-serif" font-size="{font:g}" fill="{ink}">{esc(label)}</text>')
        if kind in CARD_TYPES and h > 50:
            parts.append(f'<text x="{x+12:g}" y="{y+h-12:g}" font-family="Segoe UI, sans-serif" font-size="28" fill="#253260">123</text>')
        elif kind in CHART_TYPES and h > 65:
            for k, fraction in enumerate((0.24, 0.57, 0.82)):
                bx = x + 18 + k * max(5, (w - 36) / 3)
                bw = max(3, (w - 48) / 4)
                bh = max(5, (h - 42) * fraction)
                parts.append(f'<rect x="{bx:g}" y="{y+h-10-bh:g}" width="{bw:g}" height="{bh:g}" fill="#144FCC"/>')
        elif kind in TABLE_TYPES:
            for row in range(min(5, max(0, int((h - 32) / 24)))):
                parts.append(f'<text x="{x+9:g}" y="{y+45+row*24:g}" font-family="Segoe UI, sans-serif" font-size="12" fill="#535D70">Example row {row+1}     Sample category     123</text>')
        parts.append('</g>')
    parts += [f'<rect x="0" y="{max(0,height-24):g}" width="{width:g}" height="24" fill="#253260" opacity="0.92"/>',
              f'<text x="8" y="{max(14,height-8):g}" font-family="Segoe UI, sans-serif" font-size="12" fill="#FFFFFF">SYNTHETIC PREVIEW - invented labels and values; native appearance not certified</text>', '</svg>']
    return '\n'.join(parts)


def run(ws, project, page: str | None = None, spec=None) -> dict:
    """Render mirror geometry only, with optional region and intentional-overlay contracts.

    `spec` is a dict or a JSON path within the mirror. The result includes synthetic SVG paths,
    approximation warnings, region/overlap findings and explicit unsupported-rendering states.
    """
    workspace = Path(ws.root).resolve()
    mirror = _inside(Path(ws.mirror), workspace)
    report = _inside(Path(project.mirror_report), mirror)
    pages_dir = _inside(report / 'definition' / 'pages', mirror)
    if not pages_dir.is_dir():
        raise PbzError('NO_MIRROR', 'run pbz pull before preview')
    contract = _contract(spec, mirror)
    asset, background = None, None
    if contract.get('asset_receipt'):
        from . import consent
        asset, background = consent.approved_png(ws, contract['asset_receipt'])
    dirs = [p for p in sorted(pages_dir.iterdir()) if p.is_dir()]
    selected = []
    for folder in dirs:
        p = folder / 'page.json'
        if not p.is_file():
            continue
        obj = _read(p, mirror)
        if page is None or page.casefold() in (folder.name.casefold(), str(obj.get('displayName', '')).casefold()):
            selected.append((folder, obj))
    if not selected:
        raise PbzError('NOT_FOUND', 'no matching sanitized report page')
    output = _inside(mirror / 'previews', mirror)
    output.mkdir(exist_ok=True)
    results, findings, planned = [], [], []
    for folder, obj in selected:
        pid = folder.name
        width = _number(obj.get('width'), 1280)
        height = _number(obj.get('height'), 720)
        if not 0 < width <= 100000 or not 0 < height <= 100000:
            raise PbzError('BAD_REQUEST', 'preview canvas dimensions are invalid')
        visuals = []
        for file in sorted((folder / 'visuals').glob('*/visual.json')):
            visual = _read(file, mirror)
            pos = visual.get('position', {})
            kind = visual.get('visual', {}).get('visualType', 'group' if 'visualGroup' in visual else 'unknown')
            font, length = _font_and_length(visual, kind)
            visuals.append({'id': file.parent.name, 'kind': kind, 'box': tuple(_number(pos.get(k)) for k in ('x','y','width','height')),
                            'z': _number(pos.get('z')), 'font': font, 'length': length, 'hidden': visual.get('isHidden') is True,
                            'style': _style(visual, kind), 'group': visual.get('parentGroupName')})
        visuals.sort(key=lambda v: v['z'])
        current = []
        def finding(code, severity='warning', **extra):
            current.append({'code': code, 'severity': severity, 'page': pid, **extra})
        if background is None and any(_literal(x.get('properties', {}).get('image')) is not None or 'image' in x.get('properties', {})
               for key in ('background','outspace') for x in obj.get('objects', {}).get(key, [])):
            finding('BACKGROUND_ASSET_WITHHELD')
        allowed = {frozenset(pair) for pair in contract.get('allow_overlaps', [])}
        live = [v for v in visuals if not v['hidden'] and v['kind'] != 'group']
        canvas = (0,0,width,height)
        for name, region in contract['regions'].items():
            if not _contains(canvas, region):
                finding('CONTRACT_REGION_OFF_CANVAS', 'error', region=name)
        available = {v['id'] for v in visuals}
        for visual in contract.get('assignments', {}):
            if visual not in available:
                finding('ASSIGNED_VISUAL_NOT_FOUND', 'error', visual=visual)
        for v in live:
            vid, box = v['id'], v['box']
            if box[2] <= 0 or box[3] <= 0:
                finding('ZERO_SIZE', 'error', visual=vid)
            elif not _contains(canvas, box):
                finding('OFF_CANVAS', 'error', visual=vid)
            region = contract.get('assignments', {}).get(vid)
            if region is None and v['kind'] in CONTROL_TYPES and 'sidebar' in contract['regions']:
                region = 'sidebar'
            if region and not _contains(contract['regions'][region], box):
                finding('OUTSIDE_REGION', 'error', visual=vid, region=region)
            if region and v['style']['weight'] > 0:
                pad = v['style']['weight'] / 2
                if not _contains(contract['regions'][region], (box[0]-pad,box[1]-pad,box[2]+pad*2,box[3]+pad*2)):
                    finding('OUTLINE_NEAR_REGION_EDGE', visual=vid, region=region)
            if v['group']:
                finding('GROUPED_LAYOUT_APPROXIMATED', visual=vid)
            if v['font'] < contract['min_font_pt']:
                finding('FONT_BELOW_MINIMUM', visual=vid)
            if v['length'] and box[2] > 0:
                estimated_line = v['length'] * v['font'] * 4 / 3 * .54
                lines = max(1, math.ceil(estimated_line / max(1, box[2] - 16)))
                if lines * v['font'] * 1.6 + 16 > box[3]:
                    finding('TEXT_CLIPPING_RISK', visual=vid)
            if v['kind'] == 'image':
                finding('ASSET_PREVIEW_UNSUPPORTED', visual=vid)
            elif v['kind'] not in KNOWN_TYPES:
                finding('CUSTOM_VISUAL_RENDER_UNSUPPORTED', visual=vid)
        for i, a in enumerate(live):
            for b in live[i+1:]:
                pair = frozenset((a['id'], b['id']))
                if pair not in allowed and _overlap(a['box'], b['box']) > 4:
                    finding('UNDECLARED_OVERLAP', 'error', visuals=[a['id'], b['id']])
        fill = {}
        for name, minimum in contract.get('min_region_fill', {}).items():
            region = contract['regions'][name]
            rects = []
            for v in live:
                x,y,w,h = v['box']; rx,ry,rw,rh = region
                left,top,right,bottom=max(x,rx),max(y,ry),min(x+w,rx+rw),min(y+h,ry+rh)
                if right>left and bottom>top:
                    rects.append((left,top,right-left,bottom-top))
            fill[name] = round(_union_area(rects) / (region[2]*region[3]), 4)
            if fill[name] < minimum:
                finding('REGION_UNDERUSED', region=name)
        def rendering(kind):
            return 'geometry_only' if kind in KNOWN_TYPES else 'unsupported'
        capabilities = {'layout': 'geometry_only', 'text_fit': 'unsupported', 'interaction': 'unsupported',
                        'custom_visuals': 'unsupported' if any(v['kind'] not in KNOWN_TYPES for v in live) else 'not_applicable'}
        visual_capabilities = [{'id': v['id'], 'rendering': rendering(v['kind'])} for v in live]
        stem = hashlib.sha256(pid.encode('utf-8')).hexdigest()[:20]
        svg_path = _inside(output / (stem + '.svg'), mirror)
        metadata_path = _inside(output / (stem + '.json'), mirror)
        svg = _svg(visuals, width, height, contract['regions'], background, contract.get('asset_scaling', 'fill'))
        record = {'page': pid, 'svg': str(svg_path), 'metadata': str(metadata_path),
                  'rendering': 'SYNTHETIC_APPROXIMATION', 'native_visual_acceptance': 'NOT_RUN',
                  'images': 'APPROVED_BACKGROUND_ONLY' if asset else 'WITHHELD',
                  **({'background_receipt': asset['id'], 'background_sha256': asset['sha256'],
                      'background_binding': 'EXPLICIT_APPROVED_ASSET'} if asset else {}),
                  'custom_visual_rendering': 'UNSUPPORTED' if any(v['kind'] not in KNOWN_TYPES for v in live) else 'NOT_APPLICABLE',
                  'capabilities': capabilities, 'visual_capabilities': visual_capabilities,
                  'size': [width,height], 'region_fill': fill, 'findings': current}
        planned.append((svg_path, svg, metadata_path, json.dumps(record, indent=2) + '\n', record))
        findings += current
        results.append(record)
    ops = []
    for svg_path, svg, metadata_path, meta, _record in planned:
        _inside(svg_path, mirror)
        _inside(metadata_path, mirror)
        ops.append(("write", str(svg_path), svg, b""))
        ops.append(("write", str(metadata_path), meta, b""))
    if ops:
        commit_bytes(ops)
    return {'verb':'preview', 'version':VERSION, 'result':'GENERATED', 'project':project.name,
            'gate':'FAIL' if any(f['severity']=='error' for f in findings) else 'PASS',
            'pages':results, 'findings':findings,
            'limits':['Invented labels and values only; no engine queries or token expansion.',
                      'Report images are withheld; an explicit unchanged human-approved PNG receipt may supply the synthetic background. Custom visual compilation is unsupported.',
                      'Font fit is approximate; native rendering and interactions require separate review.']}
