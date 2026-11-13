"""Import a Performance Analyzer JSON file.

The file is protected because its query text can contain filter literals. This module maps a
visual id onto the mirror and emits latency bands. Query text is never copied into the result
or into an error. Replay runs only for a human, through `pbz perf`, and still does not print
the query.
"""
from __future__ import annotations

import json
import math
import os

from . import perf
from .common import PbzError, _reparse_link

_UNSAFE = PbzError("UNSAFE_PATH", "refusing to write through a link")

_BANDS = ((100, "LT_100MS"), (500, "100_500MS"), (2000, "500MS_2S"), (10000, "2_10S"))


def _band(ms) -> str:
    if isinstance(ms, bool) or ms is None:
        return "UNAVAILABLE"
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return "UNAVAILABLE"
    if not math.isfinite(value) or value < 0:
        return "UNAVAILABLE"
    for limit, name in _BANDS:
        if value < limit:
            return name
    return "GT_10S"


def _first(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _visuals(project) -> dict[str, str]:
    found = {}
    report = project.mirror_report
    if not os.path.isdir(report):
        return found
    if _reparse_link(report):
        raise _UNSAFE
    for dirpath, dirs, names in os.walk(report):
        if _reparse_link(dirpath):
            raise _UNSAFE
        for d in dirs:
            if _reparse_link(os.path.join(dirpath, d)):
                raise _UNSAFE
        for name in names:
            if name != "visual.json":
                continue
            path = os.path.join(dirpath, name)
            if _reparse_link(path):
                raise _UNSAFE
            try:
                doc = json.loads(open(path, encoding="utf-8").read())
            except (OSError, UnicodeError, ValueError):
                continue
            ident = str(doc.get("name") or "")
            if ident:
                found[ident] = os.path.basename(os.path.dirname(dirpath))
    return found


def _events(doc):
    if isinstance(doc, dict):
        for key in ("events", "Events"):
            if isinstance(doc.get(key), list):
                return doc[key]
    if isinstance(doc, list):
        return doc
    return []


def load(ws, project, path: str, replay: bool = False, human: bool = False) -> dict:
    try:
        raw = open(path, encoding="utf-8").read()
        doc = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as err:
        raise PbzError("BAD_REQUEST", "performance analyzer file could not be read") from err
    known = _visuals(project)
    visuals = []
    queries = []
    for event in _events(doc):
        if not isinstance(event, dict):
            continue
        ident = str(event.get("visualId") or event.get("VisualId") or event.get("id") or "")
        metrics = event.get("metrics") or event.get("Metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        row = {"visual": ident if ident in known else "UNMAPPED",
               "query": _band(_first(metrics.get("queryDuration"), metrics.get("QueryDuration"))),
               "render": _band(_first(metrics.get("renderDuration"), metrics.get("RenderDuration"))),
               "other": _band(_first(metrics.get("otherDuration"), metrics.get("OtherDuration")))}
        visuals.append(row)
        query = event.get("queryText") or event.get("QueryText") or event.get("dax")
        if isinstance(query, str) and query.strip():
            queries.append(query)
    out = {"verb": "perf", "source": "performance-analyzer", "visuals": visuals[:200],
           "mapped": sum(1 for v in visuals if v["visual"] != "UNMAPPED")}
    if replay:
        if not human:
            raise PbzError("HUMAN_ONLY", "replay is the reviewer's: run it yourself in a terminal")
        if not queries:
            out["replay"] = "NO_QUERY"
        else:
            try:
                out["replay"] = perf.run(ws, project, queries[0]).get("timing", "UNAVAILABLE")
            except PbzError as err:
                raise PbzError(err.code, "replay failed") from None
    return out
