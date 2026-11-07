"""Checklist for the user's real-data visual review. Names only."""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import tmdl
from .common import PbzError, commit_bytes
from .describe import field_usage


START = "<!-- pbz:generated:start -->"
END = "<!-- pbz:generated:end -->"


def _packet_plan(ws, alias: str, text: str, folder: str) -> tuple[str, list]:
    """Plan the immutable packet, a legacy archive and the alias. Nothing is written."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    rel = f"{folder}/{digest}.md"
    packet = os.path.join(ws.mirror, folder, digest + ".md")
    ops: list[tuple] = []
    if os.path.exists(packet):
        with open(packet, encoding="utf-8") as f:
            if f.read() != text:
                raise PbzError("PACKET_CONFLICT", "immutable packet contents differ")
    else:
        ops.append(("write", packet, text, b""))
    target = os.path.join(ws.mirror, alias)
    try:
        with open(target, encoding="utf-8-sig") as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    managed = START + "\n" + text.rstrip() + "\n" + END
    if START in old and END in old and old.index(START) < old.index(END):
        prefix, rest = old.split(START, 1)
        _body, suffix = rest.split(END, 1)
        new = prefix + managed + suffix
    elif old:
        # First migration cannot reliably separate handwritten notes from generated text.
        legacy = hashlib.sha256(old.encode("utf-8")).hexdigest()[:20]
        archive = os.path.join(ws.mirror, folder, "legacy-" + legacy + ".md")
        if not os.path.exists(archive):
            ops.append(("write", archive, old, b""))
        new = managed + "\n\n## Preserved previous checklist and notes\n\n" + old
    else:
        new = managed + "\n\n## Reviewer notes\n\n"
    if new != old:
        ops.append(("write", target, new, b""))
    return "pbz-workspace/" + rel, ops


def write_packet(ws, alias: str, text: str, folder: str) -> str:
    """Immutable generated packet plus an alias preserving all text outside its managed block."""
    rel, ops = _packet_plan(ws, alias, text, folder)
    if ops:
        commit_bytes(ops)
    return rel


def _load_state(path: str) -> dict:
    """Missing state is empty. A present file that cannot be read or parsed is refused."""
    if not os.path.lexists(path):
        return {"version": 2, "projects": {}}
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        raise PbzError("REFUSED", "handoff state is unreadable")
    try:
        state = json.loads(raw)
    except ValueError:
        raise PbzError("REFUSED", "handoff state is malformed")
    projects = state.get("projects", {}) if isinstance(state, dict) else None
    if not isinstance(state, dict) or not isinstance(projects, dict):
        raise PbzError("REFUSED", "handoff state is malformed")
    return {"version": 2, "projects": projects}


def accept_generated(ws, project, mirror_revision: str, real_revision: str) -> bool:
    """Called only after the review command's human-only acceptance check."""
    path = os.path.join(ws.pbz_dir, "handoff.json")
    state = _load_state(path)
    current = state["projects"].get(project.name, {})
    if not isinstance(current, dict):
        return False
    packet = current.get("packet", "")
    match = re.fullmatch(r"pbz-workspace/handoffs/([a-f0-9]{20})\.md", packet) if isinstance(packet, str) else None
    if not match:
        return False
    target = os.path.realpath(os.path.join(ws.mirror, "handoffs", match.group(1) + ".md"))
    base = os.path.realpath(os.path.join(ws.mirror, "handoffs")) + os.sep
    if not os.path.normcase(target).startswith(os.path.normcase(base)):
        return False
    try:
        with open(target, encoding="utf-8") as f:
            digest = hashlib.sha256(f.read().encode("utf-8")).hexdigest()[:20]
    except (OSError, UnicodeError):
        return False
    if digest != match.group(1):
        return False
    if current.get("revision") != {"mirror": mirror_revision, "real": real_revision} or not current.get("generated"):
        return False
    current["accepted"] = current["generated"]
    commit_bytes([("write", path, json.dumps(state), b"")])
    return True


def build(ws, project, include_all: bool = False) -> dict:
    log = os.path.join(ws.pbz_dir, "push-log.jsonl")
    state = os.path.join(ws.pbz_dir, "handoff.json")
    cursor = _load_state(state)
    current = cursor["projects"].get(project.name, {})
    if not isinstance(current, dict):
        current = {}
    since = "" if include_all else current.get("accepted", "")
    entries = []
    if os.path.lexists(log):
        try:
            with open(log, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            raise PbzError("REFUSED", "push log is unreadable")
        for line in lines:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                raise PbzError("REFUSED", "push log is malformed")
            if not isinstance(e, dict):
                raise PbzError("REFUSED", "push log is malformed")
            if e.get("project") == project.name and e.get("stamp", "") > since:
                entries.append(e)
    if not entries:
        out = {"verb": "handoff", "project": project.name, "result": "NOTHING_NEW"}
        if os.path.exists(os.path.join(ws.mirror, "HANDOFF.md")):
            out["file"] = "pbz-workspace/HANDOFF.md"
            if current.get("packet"):
                out["packet"] = current["packet"]
        return out
    model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
    usage = field_usage(project, model)
    pages: dict[str, set] = {}
    model_items = []
    for e in entries:
        for obj in e.get("objects", []):
            m = re.match(r"^[+~-] (measure|column) (.+)$", obj)
            if m:
                kind, name = m.groups()
                model_items.append(obj)
                for (k, _t, n), where in usage.items():
                    if k == kind and n == name:
                        for w in where:
                            page, _, rest = w.partition(" | ")
                            pages.setdefault(page, set()).add(f"{rest or 'page'}: uses {kind} {name}")
                continue
            m = re.search(r"/pages/([^/]+)/(?:visuals/([^/]+)/)?", obj)
            if m:
                pid, vid = m.groups()
                pjson = os.path.join(project.mirror_report, "definition", "pages", pid, "page.json")
                pname = pid
                try:
                    with open(pjson, encoding="utf-8-sig") as f:
                        pname = json.load(f).get("displayName", pid)
                except (OSError, ValueError):
                    pass
                pages.setdefault(pname, set()).add(f"{vid}: layout/format/binding changed" if vid else "page settings changed")
            elif obj.strip():
                model_items.append(obj)
    lines = [f"# Visual review checklist: {project.name}", "",
             f"Push range: `{min(e['stamp'] for e in entries)}` to `{max(e['stamp'] for e in entries)}`.", "",
             "Open the real report in Power BI Desktop (refresh if needed) and tick each item.", ""]
    for page in sorted(pages):
        lines.append(f"## {page}")
        for item in sorted(pages[page]):
            lines.append(f"- [ ] {item}: renders, no error icon, values look plausible, filters and slicers respond")
        lines.append("")
    if model_items:
        lines += ["## Model changes (no direct visual)", *[f"- [ ] {x}" for x in sorted(set(model_items))], ""]
    lines += ["Tell the agent what looks wrong in words (blank, doubled, too high, wrong period).",
              "Do not paste numbers or names from the report."]
    text = "\n".join(lines) + "\n"
    packet, ops = _packet_plan(ws, "HANDOFF.md", text, "handoffs")
    from . import evidence
    revision = {"mirror": evidence.mirror_rev(ws, project), "real": evidence.real_rev(ws, project)}
    current.update(generated=max(e["stamp"] for e in entries), packet=packet, revision=revision)
    cursor["projects"][project.name] = current
    ops.append(("write", state, json.dumps(cursor), b""))
    commit_bytes(ops)

    return {"verb": "handoff", "project": project.name, "file": "pbz-workspace/HANDOFF.md",
            "packet": packet, "review_required": True, "checklist": text}
