"""Checklist for the user's real-data visual review. Names only."""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import tmdl
from .common import PbzError, write_text
from .describe import field_usage


START = "<!-- pbz:generated:start -->"
END = "<!-- pbz:generated:end -->"


def write_packet(ws, alias: str, text: str, folder: str) -> str:
    """Immutable generated packet plus an alias preserving all text outside its managed block."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    rel = f"{folder}/{digest}.md"
    packet = os.path.join(ws.mirror, folder, digest + ".md")
    if os.path.exists(packet):
        with open(packet, encoding="utf-8") as f:
            if f.read() != text:
                raise PbzError("PACKET_CONFLICT", "immutable packet contents differ")
    else:
        write_text(packet, text)
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
            write_text(archive, old)
        new = managed + "\n\n## Preserved previous checklist and notes\n\n" + old
    else:
        new = managed + "\n\n## Reviewer notes\n\n"
    if new != old:
        write_text(target, new)
    return "pbz-workspace/" + rel


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    # Legacy `last` meant generated, never accepted. Do not consume those outstanding items.
    if not isinstance(state, dict):
        state = {}
    return {"version": 2, "projects": state.get("projects", {}) if isinstance(state.get("projects"), dict) else {}}


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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
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
    try:
        with open(log, encoding="utf-8") as f:
            for line in f:
                e = json.loads(line)
                if e.get("project") == project.name and e.get("stamp", "") > since:
                    entries.append(e)
    except FileNotFoundError:
        pass
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
    lines += ["Tell the agent what looks wrong in words (blank, doubled, too high, wrong period) or share",
              "the specific numbers only if you choose to."]
    text = "\n".join(lines) + "\n"
    packet = write_packet(ws, "HANDOFF.md", text, "handoffs")
    os.makedirs(ws.pbz_dir, exist_ok=True)
    from . import evidence
    revision = {"mirror": evidence.mirror_rev(ws, project), "real": evidence.real_rev(ws, project)}
    current.update(generated=max(e["stamp"] for e in entries), packet=packet, revision=revision)
    cursor["projects"][project.name] = current
    with open(state, "w", encoding="utf-8") as f:
        json.dump(cursor, f)

    return {"verb": "handoff", "project": project.name, "file": "pbz-workspace/HANDOFF.md",
            "packet": packet, "review_required": True, "checklist": text}
