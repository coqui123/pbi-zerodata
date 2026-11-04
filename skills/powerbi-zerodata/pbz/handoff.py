"""Checklist for the user's real-data visual review. Names only."""
from __future__ import annotations

import json
import os
import re

from . import tmdl
from .common import write_text
from .describe import field_usage


def build(ws, project, include_all: bool = False) -> dict:
    log = os.path.join(ws.pbz_dir, "push-log.jsonl")
    state = os.path.join(ws.pbz_dir, "handoff.json")
    since = ""
    if not include_all:
        try:
            with open(state, encoding="utf-8") as f:
                since = json.load(f).get("last", "")
        except (OSError, ValueError):
            since = ""
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
        return {"verb": "handoff", "project": project.name, "result": "NOTHING_NEW"}
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
    write_text(os.path.join(ws.mirror, "HANDOFF.md"), text)
    os.makedirs(ws.pbz_dir, exist_ok=True)
    with open(state, "w", encoding="utf-8") as f:
        json.dump({"last": max(e["stamp"] for e in entries)}, f)
    return {"verb": "handoff", "project": project.name, "file": "pbz-workspace/HANDOFF.md", "checklist": text}
