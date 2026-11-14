"""Visual catalog for `pbz add visual` and `pbz visual`.

Built-in types come from Microsoft's core visual capabilities (rules/visuals.json, compacted from
@microsoft/powerbi-core-visual-schema by install/build_visual_catalog.py). Custom visuals come from
three kinds of registration in definition/report.json:

- AppSource (public): the visual's guid in `publicCustomVisuals`; Desktop downloads it on open.
- Private .pbiviz: a `resourcePackages` entry of type CustomVisual named <guid>, plus the unpacked
  package in <Report>/CustomVisuals/<guid>/ (package.json + resources/<guid>.pbiviz.json).
- Organizational store: a `resourcePackages` entry of type OrganizationalStoreCustomVisual named
  <guid>_OrgStore; the visual's visualType carries the same suffix.

Data roles (queryState keys) are the internal `capabilities.dataRoles[].name` values. pbz reads only
identities, data roles, kinds, mapping conditions and object names from a package. Package code is
never run, shown or mirrored, and .pbiviz files are treated as untrusted zips.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import zipfile

from . import pbir
from .common import PbzError, _contained, _reparse_link, commit_bytes, read_definition_text, write_text

KINDS = {0: "grouping", 1: "measure", 2: "either", "grouping": "grouping", "measure": "measure",
         "groupingormeasure": "either", "either": "either"}
ORG = "_OrgStore"
GUID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,159}$")
MAX_ENTRIES, MAX_ENTRY, MAX_TOTAL = 4000, 64 * 2 ** 20, 256 * 2 ** 20
CONTRACTS = (".pbz", "visuals")          # under the workspace root: metadata only (roles, kinds, names)
PENDING = "visual-packages"              # under the private store: unpacked private packages until push
NO_DATA_TYPES = {"textbox", "image", "shape", "basicShape", "actionButton", "pageNavigator",
                 "bookmarkNavigator"}

_CORE: dict | None = None


# ---------------------------------------------------------------- contracts

def compact_capabilities(caps: dict, include_objects: bool = False) -> dict:
    """{roles: [{name, kind, display?, types?}], mappings?: [[{role: {min?, max?}}]], objects?: {...}}
    from a capabilities object (core catalog or a custom visual's capabilities.json)."""
    caps = caps if isinstance(caps, dict) else {}
    raw = [r for r in caps.get("dataRoles") or [] if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"]]
    order = {id(r): (r.get("displayOrder") if isinstance(r.get("displayOrder"), (int, float)) else 1000 + i)
             for i, r in enumerate(raw)}
    roles = []
    for r in sorted(raw, key=lambda r: order[id(r)]):
        k = r.get("kind")
        rec = {"name": r["name"], "kind": KINDS.get(k.lower() if isinstance(k, str) else k, "either")}
        disp = r.get("displayName")
        if isinstance(disp, str) and disp and disp != r["name"]:
            rec["display"] = disp
        types = sorted({t for rt in r.get("requiredTypes") or [] if isinstance(rt, dict)
                        for t, on in rt.items() if on})
        if types:
            rec["types"] = types
        roles.append(rec)
    mappings = []
    for m in caps.get("dataViewMappings") or []:
        alts = []
        for c in (m.get("conditions") or []) if isinstance(m, dict) else []:
            if not isinstance(c, dict):
                continue
            alt = {}
            for role, lim in c.items():
                if isinstance(lim, dict):
                    b = {k: lim[k] for k in ("min", "max") if isinstance(lim.get(k), int) and not isinstance(lim.get(k), bool)}
                    if b:
                        alt[role] = b
            alts.append(alt)
        mappings.append(alts)
    out: dict = {"roles": roles}
    if mappings and all(mappings):  # a mapping without conditions accepts any field counts
        out["mappings"] = mappings
    objs = caps.get("objects")
    if include_objects and isinstance(objs, dict):
        out["objects"] = {o: sorted((v.get("properties") or {}).keys()) for o, v in sorted(objs.items())
                          if isinstance(v, dict) and isinstance(v.get("properties") or {}, dict)}
    return out


def core() -> dict:
    global _CORE
    if _CORE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules", "visuals.json")
        with open(path, encoding="utf-8") as f:  # pbz-allow-open: packaged rules file
            _CORE = json.load(f)["visuals"]
    return _CORE


def core_name(vtype: str) -> str | None:
    low = (vtype or "").lower()
    return next((n for n in core() if n.lower() == low), None)


def fits(contract: dict, counts: dict) -> bool:
    """Do these per-role field counts satisfy at least one condition of at least one data view mapping?"""
    maps = contract.get("mappings") or []
    if not maps:
        return True
    for alts in maps:
        for alt in alts:
            if all(b.get("min", 0) <= counts.get(role, 0) <= b.get("max", 10 ** 6) for role, b in alt.items()):
                return True
    return False


def conditions_text(contract: dict) -> list[str]:
    out = []
    for alts in contract.get("mappings") or []:
        for alt in alts:
            parts = []
            for role, b in alt.items():
                lo, hi = b.get("min"), b.get("max")
                parts.append(f"{role}={lo}" if lo is not None and lo == hi else
                             f"{role}>={lo}" if hi is None else f"{role}<={hi}" if not lo else f"{lo}<={role}<={hi}")
            text = " ".join(parts) or "any"
            if text not in out:
                out.append(text)
    return out


# ---------------------------------------------------------------- report registrations

def _report_json(report_dir: str) -> tuple[str, bytes, object]:
    path = os.path.join(report_dir, "definition", "report.json")
    if not os.path.exists(path):
        raise PbzError("NO_MIRROR", "report.json missing: run `pbz pull` first")
    text, bom = read_definition_text(path)
    return text, bom, pbir.parse_json(text)


def registrations(report_dir: str) -> dict:
    """{visualType: {"kind": public|private|organizational, "conflict"?: True}} plus the
    organizationCustomVisuals names under "_org_metadata" (metadata only, not a runtime registration)."""
    try:
        data = json.loads(read_definition_text(os.path.join(report_dir, "definition", "report.json"))[0])
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for g in data.get("publicCustomVisuals") or []:
        if isinstance(g, str) and g:
            out[g] = {"kind": "public"}
    for p in data.get("resourcePackages") or []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"]:
            continue
        kind = {"CustomVisual": "private", "OrganizationalStoreCustomVisual": "organizational"}.get(p.get("type"))
        if kind:
            if p["name"] in out and out[p["name"]]["kind"] != kind:
                out[p["name"]]["conflict"] = True
            else:
                out[p["name"]] = {"kind": kind}
    meta = [o.get("name") for o in data.get("organizationCustomVisuals") or [] if isinstance(o, dict) and o.get("name")]
    if meta:
        out["_org_metadata"] = meta
    return out


def used_types(report_dir: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in pbir.report_files(report_dir):
        if not path.replace("\\", "/").endswith("/visual.json"):
            continue
        try:
            v = json.loads(read_definition_text(path)[0]).get("visual") or {}
        except (OSError, ValueError, AttributeError):
            continue
        vt = v.get("visualType") if isinstance(v, dict) else None
        if isinstance(vt, str) and vt:
            counts[vt] = counts.get(vt, 0) + 1
    return counts


def observed_roles(report_dir: str, vtype: str) -> list[str]:
    """Role names used by existing instances of a visual type (Desktop-authored), in first-seen order."""
    seen: list[str] = []
    for path in pbir.report_files(report_dir):
        if not path.replace("\\", "/").endswith("/visual.json"):
            continue
        try:
            v = json.loads(read_definition_text(path)[0]).get("visual") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(v, dict) or v.get("visualType") != vtype:
            continue
        qs = (v.get("query") or {}).get("queryState") if isinstance(v.get("query"), dict) else None
        for role in qs or {}:
            if role not in seen:
                seen.append(role)
    return seen


def base_guid(vtype: str) -> str:
    return vtype[:-len(ORG)] if vtype.endswith(ORG) else vtype


# ---------------------------------------------------------------- contract store

def _contract_dir(ws) -> str:
    return os.path.join(ws.root, *CONTRACTS)


def _contract_path(ws, guid: str) -> str:
    if not GUID_RE.match(guid):
        raise PbzError("BAD_REQUEST", "a custom visual guid has letters, digits and _ only")
    return os.path.join(_contract_dir(ws), guid + ".json")


def contracts(ws) -> dict[str, dict]:
    out = {}
    d = _contract_dir(ws)
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.endswith(".json"):
            try:
                with open(os.path.join(d, f), encoding="utf-8") as h:  # pbz-allow-open: custom-visual contract store
                    c = json.load(h)
            except (OSError, ValueError):
                continue
            if isinstance(c, dict) and c.get("guid"):
                out[c["guid"]] = c
    return out


def _save_contract(ws, c: dict) -> str:
    path = _contract_path(ws, c["guid"])
    c = dict(c, updated=dt.datetime.now().replace(microsecond=0).isoformat())
    write_text(path, json.dumps(c, indent=1, ensure_ascii=False) + "\n")
    return os.path.relpath(path, ws.root).replace("\\", "/")


def _pending_dir(ws, guid: str) -> str:
    if not isinstance(guid, str) or not GUID_RE.match(guid):
        raise PbzError("BAD_REQUEST", "a custom visual guid has letters, digits and _ only")
    return os.path.join(ws.private, PENDING, guid)


def _package_dirs(ws, project, guid: str) -> list[str]:
    """Unpacked private packages for this guid: pending (imported, not pushed yet) and in the real report."""
    if not isinstance(guid, str) or not GUID_RE.match(guid):
        return []
    pending = _pending_dir(ws, guid)
    report = os.path.join(project.report_dir, "CustomVisuals", guid)
    return [d for d in (pending, report) if os.path.isfile(os.path.join(d, "package.json"))]


def package_present(ws, project, guid: str) -> bool:
    return bool(_package_dirs(ws, project, guid))


def _package_contract(pkg_dir: str) -> dict | None:
    try:
        info = _read_package(lambda n: _read_file(pkg_dir, n), _list_dir(pkg_dir))
    except PbzError:
        return None
    return info


def load_contract(ws, project, vtype: str) -> tuple[dict | None, str]:
    """(contract, source) for a visual type: core | contract | package | observed | none."""
    cn = core_name(vtype)
    if cn:
        return core()[cn], "core"
    g = base_guid(vtype)
    c = contracts(ws).get(g)
    if c:
        return c, "contract"
    for d in _package_dirs(ws, project, g):
        info = _package_contract(d)
        if info:
            return info, "package"
    roles = observed_roles(project.mirror_report, vtype)
    if roles:
        return {"roles": [{"name": r, "kind": "either"} for r in roles]}, "observed"
    return None, "none"


# ---------------------------------------------------------------- .pbiviz

def _read_file(root: str, name: str) -> bytes:
    if _reparse_link(root):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    path = root
    for part in name.replace("\\", "/").split("/"):
        if not part or part in (".", "..") or ":" in part or "\\" in part:
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        path = os.path.join(path, part)
        if _reparse_link(path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    try:
        if os.path.normcase(os.path.commonpath([path_abs, root_abs])) != os.path.normcase(root_abs):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    except ValueError:
        raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
    with open(path, "rb") as handle:
        return handle.read()


def _list_dir(root: str) -> list[str]:
    out = []
    for cur, _dirs, files in os.walk(root):
        for f in files:
            out.append(os.path.relpath(os.path.join(cur, f), root).replace("\\", "/"))
    return out


def _json_bytes(b: bytes, what: str) -> dict:
    try:
        d = json.loads(b.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise PbzError("BAD_PACKAGE", f"{what} is not JSON")
    if not isinstance(d, dict):
        raise PbzError("BAD_PACKAGE", f"{what} is not a JSON object")
    return d


def _read_package(read, names: list[str]) -> dict:
    """Identity + contract from a package's manifest and its resource payload."""
    lower = {n.lower(): n for n in names}
    if "package.json" not in lower:
        raise PbzError("BAD_PACKAGE", "package.json missing")
    manifest = _json_bytes(read(lower["package.json"]), "package.json")
    res = [r.get("file") for r in manifest.get("resources") or [] if isinstance(r, dict) and isinstance(r.get("file"), str)]
    res = [lower[f.replace("\\", "/").lower()] for f in res if f.replace("\\", "/").lower() in lower]
    if not res:
        res = [n for n in names if n.lower().startswith("resources/") and n.lower().endswith(".pbiviz.json")]
    if len(res) != 1:
        raise PbzError("BAD_PACKAGE", "expected exactly one resources/*.pbiviz.json")
    payload = _json_bytes(read(res[0]), res[0])
    mv = manifest.get("visual") if isinstance(manifest.get("visual"), dict) else {}
    pv = payload.get("visual") if isinstance(payload.get("visual"), dict) else {}
    guid = pv.get("guid") or mv.get("guid")
    if mv.get("guid") and pv.get("guid") and mv["guid"] != pv["guid"]:
        raise PbzError("BAD_PACKAGE", "visual guid differs between package.json and the resource payload")
    if not isinstance(guid, str) or not GUID_RE.match(guid):
        raise PbzError("BAD_PACKAGE", "visual guid missing or not a plain identifier")
    caps = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else manifest.get("capabilities")
    if not isinstance(caps, dict) or not caps.get("dataRoles"):
        raise PbzError("BAD_PACKAGE", "no capabilities.dataRoles in the package")
    out = {"guid": guid}
    for key, val in (("name", pv.get("name") or mv.get("name")),
                     ("display_name", pv.get("displayName") or mv.get("displayName")),
                     ("version", pv.get("version") or mv.get("version")),
                     ("api_version", payload.get("apiVersion") or mv.get("apiVersion"))):
        if isinstance(val, str) and val:
            out[key] = val[:120]
    out.update(compact_capabilities(caps, include_objects=True))
    out["resource_file"] = res[0]
    return out


def read_pbiviz(path: str) -> tuple[dict, dict[str, bytes]]:
    """Validate a .pbiviz (untrusted zip) and return (identity + contract, {safe name: bytes})."""
    if not os.path.isfile(path) or not zipfile.is_zipfile(path):
        raise PbzError("BAD_REQUEST", "not a .pbiviz (zip) file")
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise PbzError("UNSAFE_PACKAGE", "too many entries")
        total, seen = 0, set()
        for i in infos:
            n = i.filename.replace("\\", "/")
            if i.is_dir():
                continue
            parts = n.split("/")
            if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in parts or "" in parts or "\0" in n:
                raise PbzError("UNSAFE_PACKAGE", "entry path escapes the package")
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise PbzError("UNSAFE_PACKAGE", "symbolic link entry")
            if n.lower() in seen:
                raise PbzError("UNSAFE_PACKAGE", "duplicate entry")
            seen.add(n.lower())
            total += i.file_size
            if i.file_size > MAX_ENTRY or total > MAX_TOTAL:
                raise PbzError("UNSAFE_PACKAGE", "package too large")
            files[n] = z.read(i)
    info = _read_package(lambda n: files[n], list(files))
    with open(path, "rb") as f:
        info["package_sha256"] = hashlib.sha256(f.read()).hexdigest()
    return info, files


# ---------------------------------------------------------------- report.json edits (spans kept)

def _indent_block(text: str, indent: str) -> str:
    return text.replace("\n", "\n" + indent)


def _append(text: str, key: str, item) -> str:
    """Append `item` to the top-level array `key` of a JSON object text (created when missing)."""
    root = pbir.parse_json(text)
    if not isinstance(root, pbir.JObj):
        raise PbzError("BAD_REQUEST", "report.json is not an object")
    arr = root.get(key)
    body = json.dumps(item, indent=2, ensure_ascii=False)
    if isinstance(arr, pbir.JArr) and arr.items:
        last = arr.items[-1]
        ls = text.rfind("\n", 0, last.start) + 1
        indent = text[ls:last.start] if not text[ls:last.start].strip() else "    "
        return text[:last.end] + ",\n" + indent + _indent_block(body, indent) + text[last.end:]
    if isinstance(arr, pbir.JArr):
        ls = text.rfind("\n", 0, arr.start) + 1
        base = re.match(r"[ \t]*", text[ls:]).group(0)
        indent = base + "  "
        return text[:arr.start] + "[\n" + indent + _indent_block(body, indent) + "\n" + base + "]" + text[arr.end:]
    if arr is not None:
        raise PbzError("BAD_REQUEST", f"{key} in report.json is not an array")
    if root.pairs:
        last_val = root.pairs[-1][1]
        k0 = root.pairs[-1][0]
        ls = text.rfind("\n", 0, k0.start) + 1
        indent = text[ls:k0.start] if not text[ls:k0.start].strip() else "  "
        ins = ",\n" + indent + json.dumps(key) + ": [\n" + indent + "  " + _indent_block(body, indent + "  ") + "\n" + indent + "]"
        return text[:last_val.end] + ins + text[last_val.end:]
    return text[:root.start + 1] + "\n  " + json.dumps(key) + ": [\n    " + _indent_block(body, "    ") + "\n  ]\n" + text[root.end - 1:]


def plan_registration(project, guid: str, kind: str, resource_file: str = "") -> tuple[str, str, tuple | None]:
    """Plan the report.json registration. Returns (visualType, 'added'|'present', write op or None).

    Does not write. Callers that touch other files commit this op with them.
    """
    text, bom, _root = _report_json(project.mirror_report)
    reg = registrations(project.mirror_report)
    vtype = guid + ORG if kind == "organizational" else guid
    if vtype in reg:
        if reg[vtype]["kind"] != kind:
            raise PbzError("CONFLICT", f"{vtype} is already registered as {reg[vtype]['kind']}")
        return vtype, "present", None
    if kind == "public":
        if guid in reg:
            raise PbzError("CONFLICT", f"{guid} is registered as {reg[guid]['kind']}")
        new = _append(text, "publicCustomVisuals", guid)
    elif kind == "private":
        if reg.get(guid, {}).get("kind") == "public":
            raise PbzError("CONFLICT", f"{guid} is listed as an AppSource visual; unregister it first")
        res = os.path.basename(resource_file or f"{guid}.pbiviz.json")
        new = _append(text, "resourcePackages", {"name": guid, "type": "CustomVisual",
                                                 "items": [{"name": res, "path": res, "type": "CustomVisualMetadata"}]})
    elif kind == "organizational":
        new = _append(text, "resourcePackages", {"name": vtype, "type": "OrganizationalStoreCustomVisual",
                                                 "items": [{"name": f"resources/{vtype}.pbiviz.json", "path": "",
                                                            "type": "CustomVisualMetadata"}]})
    else:
        raise PbzError("BAD_REQUEST", "register as public, private or organizational")
    pbir.parse_json(new)  # still valid JSON
    path = os.path.join(project.mirror_report, "definition", "report.json")
    return vtype, "added", ("write", path, new, bom)


def _register_in_mirror(project, guid: str, kind: str, resource_file: str = "") -> tuple[str, str]:
    """Add the registration to the mirror's report.json. Returns (visualType, change) where change is
    'added' or 'present'."""
    vtype, change, op = plan_registration(project, guid, kind, resource_file)
    if op:
        commit_bytes([op])
    return vtype, change


# ---------------------------------------------------------------- verbs

def _rel(ws, path: str) -> str:
    return os.path.relpath(path, ws.mirror).replace("\\", "/")


def import_pbiviz(ws, project, path: str, as_: str = "contract", replace: bool = False) -> dict:
    """Read a .pbiviz: keep its contract (roles, kinds, conditions) and optionally register it.
    as_: contract (roles only), public (AppSource guid), private (package staged, installed by push)."""
    if as_ not in ("contract", "public", "private"):
        raise PbzError("BAD_REQUEST", "--as contract, public or private")
    info, files = read_pbiviz(path)
    guid = info["guid"]
    if core_name(guid):
        raise PbzError("BAD_PACKAGE", f"{guid} collides with a built-in visual type")
    old = contracts(ws).get(guid)
    if old and old.get("package_sha256") not in (None, info["package_sha256"]) and as_ == "private" and not replace:
        raise PbzError("CONFLICT", f"a different package for {guid} was imported before; pass --replace")
    contract = dict(info, source="pbiviz", register_as=as_ if as_ != "contract" else (old or {}).get("register_as"))
    if contract["register_as"] is None:
        contract.pop("register_as")
    if as_ == "private":
        real_pkg = os.path.join(project.report_dir, "CustomVisuals", guid)
        if os.path.isdir(real_pkg) and not replace:
            raise PbzError("CONFLICT", f"the report already holds a private package for {guid}; pass --replace")
    out = {"verb": "visual", "action": "import", "guid": guid, "name": info.get("display_name") or info.get("name"),
           "version": info.get("version"), "roles": [f"{r['name']}:{r['kind']}" for r in info["roles"]],
           "conditions": conditions_text(info), "contract": _save_contract(ws, contract)}
    if as_ == "private":
        pending_root = os.path.join(ws.private, PENDING)
        if _reparse_link(pending_root):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        pend = _pending_dir(ws, guid)
        if _reparse_link(pend):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        if os.path.lexists(pend):
            _safe_rmtree(pending_root, pend, ignore_errors=True)
        for name, data in files.items():
            dst = os.path.join(pend, *name.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
        vtype, change = _register_in_mirror(project, guid, "private", info["resource_file"])
        out.update(registered="private", report_json=change, package="staged (installed into the report by `pbz push`)",
                   files=[_rel(ws, os.path.join(project.mirror_report, "definition", "report.json"))] if change == "added" else [])
    elif as_ == "public":
        vtype, change = _register_in_mirror(project, guid, "public")
        out.update(registered="public", report_json=change,
                   note="Desktop downloads AppSource visuals on open (needs internet; tenant settings may block it)",
                   files=[_rel(ws, os.path.join(project.mirror_report, "definition", "report.json"))] if change == "added" else [])
    out["next"] = f"pbz add visual {guid} --page <page> --field ROLE=ref ..., then pbz validate / pbz push"
    return out


def register(ws, project, guid: str, as_: str, roles: list[str] | None = None) -> dict:
    """Register a custom visual by guid without a package (AppSource or organizational store), with an
    optional manual contract: --role NAME:grouping|measure|either (repeatable)."""
    guid = base_guid(guid.strip())
    if not GUID_RE.match(guid) or core_name(guid):
        raise PbzError("BAD_REQUEST", "give the visual's guid (from its package or a Desktop-authored visual)")
    if as_ not in ("public", "organizational"):
        raise PbzError("BAD_REQUEST", "--as public or organizational (private visuals need `pbz visual import`)")
    out = {"verb": "visual", "action": "register", "guid": guid}
    if roles:
        parsed = []
        for r in roles:
            name, _, kind = r.partition(":")
            kind = KINDS.get(kind.strip().lower() or "either")
            if not name.strip() or kind is None:
                raise PbzError("BAD_REQUEST", "--role NAME:grouping|measure|either")
            parsed.append({"name": name.strip(), "kind": kind})
        old = contracts(ws).get(guid) or {}
        out["contract"] = _save_contract(ws, dict(old, guid=guid, source=old.get("source", "manual"),
                                                  register_as=as_, roles=parsed))
    vtype, change = _register_in_mirror(project, guid, as_)
    out.update(visual_type=vtype, registered=as_, report_json=change)
    if change == "added":
        out["files"] = [_rel(ws, os.path.join(project.mirror_report, "definition", "report.json"))]
    if as_ == "organizational":
        out["note"] = "organizational visuals resolve in your tenant only; check the visual in Desktop after push"
    return out


def resolve(ws, project, vtype: str) -> tuple[str, dict, str, list[str], tuple | None]:
    """(visualType, contract, source, notes, pending registration) for `pbz add visual`. A known
    custom visual that is not registered yet comes back with the registration to make
    (`register_pending`) once the visual itself is valid."""
    cn = core_name(vtype)
    if cn:
        return cn, core()[cn], "core", [], None
    reg = registrations(project.mirror_report)
    hit = vtype if vtype in reg else next((k for k in reg if k.lower() == vtype.lower() and not k.startswith("_")), None)
    notes: list[str] = []
    pending = None
    if hit is None:
        low = vtype.lower()
        cands = [c for g, c in contracts(ws).items()
                 if low in (g.lower(), (g + ORG).lower(), str(c.get("name", "")).lower(), str(c.get("display_name", "")).lower())]
        if len(cands) > 1:
            raise PbzError("AMBIGUOUS", f"{vtype} matches several imported visuals: give the guid")
        if not cands:
            raise PbzError("BAD_REQUEST", f"unknown visual type {vtype}: a built-in type ({len(core())} known, see "
                                          "`pbz visual list`), a registered custom visual, or `pbz visual import <file.pbiviz>`")
        c = cands[0]
        how = c.get("register_as")
        if how == "private" and not _package_dirs(ws, project, c["guid"]):
            raise PbzError("NOT_REGISTERED", f"{c['guid']}: package missing, rerun `pbz visual import <file> --as private`")
        if how not in ("public", "private", "organizational"):
            raise PbzError("NOT_REGISTERED", f"{c['guid']} is not registered in this report: `pbz visual import "
                                             "<file.pbiviz> --as public|private` or `pbz visual register`")
        hit = c["guid"] + ORG if how == "organizational" else c["guid"]
        pending = (c["guid"], how, c.get("resource_file", ""))
    if reg.get(hit, {}).get("conflict"):
        raise PbzError("CONFLICT", f"{hit} is registered twice with different kinds: fix report.json first")
    contract, source = load_contract(ws, project, hit)
    if contract is None:
        raise PbzError("NO_CONTRACT", f"no data roles known for {hit}: `pbz visual import <file.pbiviz>` (roles only), "
                                      f"or `pbz visual register {base_guid(hit)} --as ... --role NAME:kind`")
    if source == "observed":
        notes.append("roles taken from existing visuals of this type (kinds unknown)")
    return hit, contract, source, notes, pending


def register_pending(project, pending: tuple | None) -> str | None:
    """Make the registration `resolve` deferred; returns a note when report.json changed."""
    if not pending:
        return None
    guid, how, resource = pending
    vtype, change, op = plan_registration(project, guid, how, resource)
    if op:
        commit_bytes([op])
    return f"registered {vtype} as {how} in report.json" if change == "added" else None


def list_visuals(ws, project) -> dict:
    reg = registrations(project.mirror_report)
    used = used_types(project.mirror_report)
    cons = contracts(ws)
    custom = []
    names = [k for k in reg if not k.startswith("_")] + [g for g in cons if g not in reg and g + ORG not in reg] + \
            [t for t in used if t not in reg and not core_name(t)]
    for n in dict.fromkeys(names):
        c, source = load_contract(ws, project, n)
        rec = {"visual_type": n, "registered": reg.get(n, {}).get("kind", "no"), "instances": used.get(n, 0),
               "roles": source if c else "unknown"}
        info = cons.get(base_guid(n)) or {}
        if info.get("display_name") or info.get("name"):
            rec["name"] = info.get("display_name") or info.get("name")
        if info.get("version"):
            rec["version"] = info["version"]
        if reg.get(n, {}).get("kind") == "private":
            rec["package"] = "present" if os.path.isdir(os.path.join(project.report_dir, "CustomVisuals", n)) else \
                ("staged" if isinstance(n, str) and GUID_RE.match(n) and os.path.isdir(_pending_dir(ws, n)) else "missing")
        if reg.get(n, {}).get("conflict"):
            rec["conflict"] = True
        if n in reg and not used.get(n):
            rec["note"] = "registered but unused"
        elif n not in reg and used.get(n):
            rec["note"] = "used but not registered: Desktop shows a placeholder"
        custom.append(rec)
    out = {"verb": "visual", "action": "list", "builtin": len(core()),
           "builtin_used": {t: c for t, c in sorted(used.items()) if core_name(t)}, "custom": custom}
    if reg.get("_org_metadata"):
        out["org_metadata_only"] = reg["_org_metadata"]
    return out


def roles(ws, project, vtype: str) -> dict:
    cn = core_name(vtype)
    name = cn or vtype
    c, source = load_contract(ws, project, name)
    if c is None:
        raise PbzError("NO_CONTRACT", f"no data roles known for {vtype}")
    out = {"verb": "visual", "action": "roles", "visual_type": name, "source": source,
           "roles": [dict(r) for r in c.get("roles", [])], "conditions": conditions_text(c) or ["any"]}
    if not c.get("roles") and name in NO_DATA_TYPES:
        out["note"] = "takes no fields"
    if c.get("objects"):
        out["objects"] = sorted(c["objects"])[:60]
    return out


# ---------------------------------------------------------------- push support

def _safe_rmtree(root: str, path: str, *, ignore_errors: bool = False) -> None:
    """Remove a real directory inside `root`. A link, a junction, or an escape is not removed."""
    if _reparse_link(root) or _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    try:
        _contained(path, root)
    except PbzError:
        raise PbzError("UNSAFE_PATH", "refusing to write through a link") from None
    if _reparse_link(path) or not os.path.isdir(path):
        return
    shutil.rmtree(path, ignore_errors=ignore_errors)


def pending_for(ws, report_json_text: str) -> dict[str, str]:
    """{guid: unpacked dir} for staged private packages that this report.json registers."""
    try:
        data = json.loads(report_json_text)
    except ValueError:
        return {}
    names = {p.get("name") for p in data.get("resourcePackages") or []
             if isinstance(p, dict) and p.get("type") == "CustomVisual"}
    out = {}
    for g in sorted(n for n in names if isinstance(n, str) and GUID_RE.match(n)):
        pend = _pending_dir(ws, g)
        if os.path.isfile(os.path.join(pend, "package.json")):
            out[g] = pend
    return out


def install(pkgs: dict[str, str], report_dir: str, backup_dir: str | None = None) -> list[str]:
    done = []
    base = os.path.join(report_dir, "CustomVisuals")
    for guid, src in pkgs.items():
        if not isinstance(guid, str) or not GUID_RE.match(guid):
            continue
        dst = os.path.join(base, guid)
        _contained(dst, base)
        if _reparse_link(dst):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        if os.path.isdir(dst):
            if backup_dir:
                shutil.copytree(dst, os.path.join(backup_dir, "CustomVisuals", guid), dirs_exist_ok=True)
            _safe_rmtree(base, dst)
        shutil.copytree(src, dst)
        done.append(guid)
    return done


def clear_pending(ws, guids) -> None:
    root = os.path.join(ws.private, PENDING)
    for g in guids:
        if not isinstance(g, str) or not GUID_RE.match(g):
            continue
        path = _pending_dir(ws, g)
        if os.path.lexists(path):
            _safe_rmtree(root, path, ignore_errors=True)
