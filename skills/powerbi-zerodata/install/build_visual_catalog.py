"""Dev tool: compact Power BI's core visual catalog into pbz/rules/visuals.json (data roles, kinds,
mapping conditions) and pbz/rules/visual-objects.json (formatting objects and properties with value
types, container objects, instance states).

Source: Microsoft's @microsoft/powerbi-core-visual-schema (MIT): data/capabilities.json,
data/vco-capabilities.json and data/instance-selectors.json, installed in tools/node_modules by
`npm install`. pbz reads only the compact files at runtime, so it stays self-contained. Rerun after
the package updates:

    python install/build_visual_catalog.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
PKG = os.path.join(SKILL, "tools", "node_modules", "@microsoft", "powerbi-core-visual-schema")
OUT = os.path.join(SKILL, "pbz", "rules", "visuals.json")
OUT_OBJECTS = os.path.join(SKILL, "pbz", "rules", "visual-objects.json")
sys.path.insert(0, SKILL)

from pbz.style import compact_objects  # noqa: E402
from pbz.visuals import compact_capabilities  # noqa: E402  (same compaction as custom visuals)


def _load(name: str):
    with open(os.path.join(PKG, "data", name), encoding="utf-8") as f:
        return json.load(f)


def _lines(d: dict) -> str:
    """One entry per line: small diffs when Microsoft's catalog changes."""
    return "{\n" + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v, separators=(',', ':'), ensure_ascii=False)}"
                              for k, v in d.items()) + "\n}"


def main() -> int:
    with open(os.path.join(PKG, "package.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]
    caps = _load("capabilities.json")
    names = [n for n in sorted(caps) if not n.startswith("_") and n != "debugVisual"]
    visuals = {name: compact_capabilities(caps[name]) for name in names}
    out = {"_source": f"@microsoft/powerbi-core-visual-schema {version} (MIT), data/capabilities.json",
           "visuals": visuals}
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=1, sort_keys=False)
        f.write("\n")
    container = compact_objects(_load("vco-capabilities.json"))
    objects = {name: compact_objects(caps[name].get("objects") or {}) for name in names}
    states = {k: v for k, v in sorted(_load("instance-selectors.json").items()) if not k.startswith("_")}
    src = (f"@microsoft/powerbi-core-visual-schema {version} (MIT): capabilities.json objects, "
           "vco-capabilities.json, instance-selectors.json")
    with open(OUT_OBJECTS, "w", encoding="utf-8", newline="\n") as f:
        f.write("{\n" + f' "_source": {json.dumps(src)},\n "container": {_lines(container)},\n'
                f' "states": {_lines(states)},\n "visuals": {_lines(objects)}\n}}\n')
    with open(OUT_OBJECTS, encoding="utf-8") as f:
        json.load(f)  # still valid JSON
    print(json.dumps({"written": [os.path.relpath(p, SKILL).replace("\\", "/") for p in (OUT, OUT_OBJECTS)],
                      "visuals": len(visuals), "container_objects": len(container), "source_version": version}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
