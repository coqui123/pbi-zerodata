"""Dev tool: compact the data roles of Power BI's core visuals into pbz/rules/visuals.json.

Source: Microsoft's @microsoft/powerbi-core-visual-schema (MIT), data/capabilities.json, installed in
tools/node_modules by `npm install`. pbz reads only the compact file at runtime, so it stays
self-contained. Rerun after the package updates:

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
sys.path.insert(0, SKILL)

from pbz.visuals import compact_capabilities  # noqa: E402  (same compaction as custom visuals)


def main() -> int:
    with open(os.path.join(PKG, "package.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]
    with open(os.path.join(PKG, "data", "capabilities.json"), encoding="utf-8") as f:
        caps = json.load(f)
    visuals = {name: compact_capabilities(v) for name, v in sorted(caps.items())
               if not name.startswith("_") and name != "debugVisual"}
    out = {"_source": f"@microsoft/powerbi-core-visual-schema {version} (MIT), data/capabilities.json",
           "visuals": visuals}
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=1, sort_keys=False)
        f.write("\n")
    print(json.dumps({"written": os.path.relpath(OUT, SKILL).replace("\\", "/"), "visuals": len(visuals),
                      "source_version": version}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
