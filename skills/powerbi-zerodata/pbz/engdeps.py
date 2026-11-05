"""`pbz unused --verify-engine`: compare pbz's metadata dependency edges with the engine's own list
(DISCOVER_CALC_DEPENDENCY through pbz-engine `deps`). Needs the project open in Desktop. Names only.

The engine reflects the model as loaded in Desktop, not unpushed mirror edits, and knows nothing about
reports, so it is a cross-check of the parser, never the source of `pbz unused`."""
from __future__ import annotations

from . import desktop, usage
from .common import PbzError, engine_call
from .usage import key

KINDS = {"MEASURE": "measure", "CALC_COLUMN": "column", "COLUMN": "column", "TABLE": "table", "CALC_TABLE": "table"}


def _k(typ: str, table: str, obj: str):
    kind = KINDS.get(typ)
    if kind is None or "?" in (table, obj) or not obj:
        return None
    return key(kind, "" if kind == "table" else table, obj)


def verify(ws, project, extra_reports=()) -> dict:
    port = desktop.require_port(project)
    res = engine_call("deps", {"private_dir": ws.private, "port": port}, timeout=300)
    if not res.get("ok"):
        raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"))
    g = usage.build(ws, project, extra_reports)
    engine_edges = set()
    for e in res.get("edges", []):
        if len(e) != 6:
            continue
        src, dst = _k(e[0], e[1], e[2]), _k(e[3], e[4], e[5])
        if src and dst and src != dst and src[0] in ("measure", "column") and dst[0] in ("measure", "column"):
            engine_edges.add((src, dst))
    ours = {(e.src, e.dst) for e in g.edges if e.via == "dax" and e.src[0] in ("measure", "column")
            and e.dst[0] in ("measure", "column")}
    engine_only = sorted(engine_edges - ours)
    pbz_only = sorted(ours - engine_edges)

    def lab(p):
        return f"{g.label(p[0])} -> {g.label(p[1])}"

    return {"verb": "unused-verify-engine", "project": project.name,
            "result": "MATCH" if not engine_only and not pbz_only else "DIFF",
            "edges": {"engine": len(engine_edges), "pbz": len(ours), "both": len(engine_edges & ours)},
            "engine_only": [lab(p) for p in engine_only][:50], "pbz_only": [lab(p) for p in pbz_only][:50],
            "note": "engine_only = edges the parser missed (report them); pbz_only = mirror edits not in Desktop "
                    "yet, or references the engine resolves differently"}
