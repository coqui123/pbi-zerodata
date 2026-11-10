"""VertiPaq storage per table, column, user hierarchy and relationship, in fixed bands only (`pbz audit memory`).

pbz-engine `memory` attributes the model's storage the way VertiPaq Analyzer does (per column: dictionary + data
segments + attribute hierarchy; user hierarchies; relationships) and releases fixed bands, never sizes or counts:
`size_band` (<1 MiB, 1-10 MiB, 10-100 MiB, 100-1000 MiB, >=1000 MiB), `share_band` of the whole model (<1%, 1-5%, 5-20%,
>=20%), `dictionary_heavy`, and constant-free booleans on the larger columns of imported tables (`time_part` for date/time
columns, `fractional` for double/decimal, `long_text` for text longer than 100 characters).

run() saves the banded result to <workspace>/.pbz/memory.json (bands, booleans and object names only, so the agent may
read it) together with the mirror revision it ran on; load() returns it with "stale": true once the mirror changed.
Each run is charged to the daily probe budget. In the strict disclosure tier the engine omits every band.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from . import checks, desktop, evidence, policy as policy_mod
from .common import PbzError, engine_call, pbz_state_path, preflight_regular, read_checked, write_text

SIZE_BANDS = ("<1 MiB", "1-10 MiB", "10-100 MiB", "100-1000 MiB", ">=1000 MiB")
SHARE_BANDS = ("<1%", "1-5%", "5-20%", ">=20%")
COUNT_BANDS = ("0", "1-9", "10-99", "100-999", "1k-9k", "10k+")
ENCODING = ("Hash", "Value")
RI = ("RI_OK", "RI_VIOLATION")
COVERAGE = ("FULL", "PARTIAL")
PREDICATE_RUNS = ("OFF", "DONE", "PARTIAL")
PREDICATES = ("time_part", "fractional", "long_text")
SAVED = ".pbz/memory.json"
CLOSED_CLASS = {"ENGINE_UNAVAILABLE", "TIMEOUT", "NOT_READY", "PROTOCOL_ERROR", "BUDGET", "NOT_AVAILABLE"}


def _disclosure(ws) -> str:
    pol = policy_mod.load(ws)
    return "shape" if pol.get("disclosure", "shape") == "shape" else "strict"


def _strip_bands(body: dict) -> dict:
    """Drop size, share, segment and RI-count bands at every level. Encoding and ri stay."""
    drop = {"size_band", "share_band", "segment_band", "ri_count"}

    def walk(value):
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items() if k not in drop}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value
    return walk(body)


def _path(ws) -> str:
    return pbz_state_path(ws, "memory.json")


def _name(v) -> str | None:
    """Object names as the engine prints them (TMSCHEMA names, or Table[Column] for relationship ends)."""
    if isinstance(v, str) and 0 < len(v) <= 1100 and not any(ord(ch) < 32 for ch in v):
        return v
    return None


def _list(v) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _bands(src, share: bool = True) -> dict:
    out = {}
    if isinstance(src, dict):
        if src.get("size_band") in SIZE_BANDS:
            out["size_band"] = src["size_band"]
        if share and src.get("share_band") in SHARE_BANDS:
            out["share_band"] = src["share_band"]
    return out


def _exact(src: dict, key: str, allowed) -> str | None:
    value = src.get(key)
    if isinstance(value, str) and value in allowed:
        return value
    return None


def _by_name(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: (x["name"].lower(), x["name"]))


def closed(res: dict) -> dict:
    """Rebuild an engine `memory` response from the closed vocabulary only: object names, the fixed bands, booleans and
    the coverage / predicates enums. Anything else (unknown keys, numbers, unexpected strings) is dropped, so what is
    saved for the agent stays closed even if the engine output ever drifts. Lists come back sorted by name."""
    res = res if isinstance(res, dict) else {}
    out: dict = {}
    model = _bands(res.get("model"), share=False)
    if model:
        out["model"] = model
    tables = []
    for t in _list(res.get("tables")):
        name = _name(t.get("name"))
        if name is None:
            continue
        cols = []
        for c in _list(t.get("columns")):
            cname = _name(c.get("name"))
            if cname is None:
                continue
            oc = {"name": cname, **_bands(c)}
            if isinstance(c.get("dictionary_heavy"), bool):
                oc["dictionary_heavy"] = c["dictionary_heavy"]
            for p in PREDICATES:
                if isinstance(c.get(p), bool):
                    oc[p] = c[p]
            for key, allowed in (("segment_band", COUNT_BANDS), ("ri_count", COUNT_BANDS),
                                 ("encoding", ENCODING), ("ri", RI)):
                exact = _exact(c, key, allowed)
                if exact is not None:
                    oc[key] = exact
            cols.append(oc)
        hiers = [{"name": hn, **_bands(h, share=False)} for h in _list(t.get("hierarchies"))
                 if (hn := _name(h.get("name"))) is not None]
        tables.append({"name": name, **_bands(t), "columns": _by_name(cols), "hierarchies": _by_name(hiers)})
    out["tables"] = _by_name(tables)
    rels = []
    for r in _list(res.get("relationships")):
        rname = _name(r.get("name"))
        if rname is None:
            continue
        o = {"name": rname}
        for k in ("from", "to"):
            v = _name(r.get(k))
            if v is not None:
                o[k] = v
        for key, allowed in (("ri", RI), ("ri_count", COUNT_BANDS)):
            exact = _exact(r, key, allowed)
            if exact is not None:
                o[key] = exact
        rels.append({**o, **_bands(r, share=False)})
    out["relationships"] = _by_name(rels)
    for k in ("other", "auto_datetime"):
        b = _bands(res.get(k))
        if len(b) == 2:
            out[k] = b
    out["coverage"] = res.get("coverage") if res.get("coverage") in COVERAGE else "PARTIAL"
    if res.get("predicates") in PREDICATE_RUNS:
        out["predicates"] = res["predicates"]
    return out


def _rev(ws, project) -> str | None:
    try:
        return evidence.mirror_rev(ws, project)
    except (OSError, ValueError, PbzError):
        return None


def run(ws, project, predicates: bool = True) -> dict:
    """Storage bands of the model open in Desktop; saves .pbz/memory.json. Raises PbzError (ENGINE_UNAVAILABLE when
    Desktop does not hold the project, else the engine's error class); returns a BUDGET verdict when the daily probe
    budget is spent."""
    # Destination is checked before Desktop, budget or the engine, so an aliased .pbz spends nothing.
    path = _path(ws)
    preflight_regular(path, root=ws.root)
    pol = policy_mod.load(ws)
    disclosure = _disclosure(ws)
    port = desktop.require_port(project)
    if not checks.spend(ws, "probes"):
        return {"verb": "memory", "status": "BUDGET", "note": "daily probe budget reached; the user can reset it"}
    res = engine_call("memory", {"private_dir": ws.private, "port": port, "disclosure": disclosure,
                                 "predicates": bool(predicates)}, timeout=1800)
    if not isinstance(res, dict) or res.get("ok") is not True:
        klass = res.get("class") if isinstance(res, dict) else None
        raise PbzError(klass if klass in CLOSED_CLASS else "ENGINE_UNAVAILABLE")
    body = closed(res)
    if disclosure != "shape":
        body = _strip_bands(body)
    doc = {"verb": "memory", "project": project.name,
           "saved_at": dt.datetime.now().replace(microsecond=0).isoformat(),
           "mirror_rev": _rev(ws, project), "disclosure": disclosure, **body}
    write_text(path, json.dumps(doc, indent=1, ensure_ascii=False) + "\n", root=ws.root)
    return {"verb": "memory", "project": project.name, **body, "saved": SAVED}


def load(ws, project) -> dict | None:
    """The saved result for this project (re-checked against the closed vocabulary) plus "stale": true when the mirror
    changed since it was saved; None when nothing usable is saved."""
    path = _path(ws)
    if not os.path.lexists(path):
        return None
    try:
        doc = json.loads(read_checked(path, root=ws.root).decode("utf-8"))
    except PbzError:
        raise
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(doc, dict) or doc.get("verb") != "memory" or doc.get("project") != project.name:
        return None
    saved_rev = doc.get("mirror_rev") if isinstance(doc.get("mirror_rev"), str) else None
    current = _rev(ws, project)
    disclosure = _disclosure(ws)
    body = closed(doc)
    if disclosure != "shape":
        body = _strip_bands(body)
    out = {"verb": "memory", "project": project.name,
           "saved_at": doc.get("saved_at") if isinstance(doc.get("saved_at"), str) else None,
           "mirror_rev": saved_rev, "disclosure": disclosure, **body}
    out["stale"] = saved_rev is None or current is None or saved_rev != current
    out["saved"] = SAVED
    return out
