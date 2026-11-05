"""Built-in replacements for external tools: three-way merge without git, PBIR integrity checks without
Microsoft's npm validator, and no runtime dependency on third-party binaries.

Run: python tests/test_selfcontained.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

from pbz import pbircheck  # noqa: E402
from pbz.mirror import merge3  # noqa: E402


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # ---------------------------------------------------------------- merge3 (replaces git merge-file)
    base = "a\nb\nc\nd\ne\n"
    check(merge3(base, "A\nb\nc\nd\ne\n", "a\nb\nc\nd\nE\n") == ("A\nb\nc\nd\nE\n", True), "merge: both sides clean")
    check(merge3(base, "a\nB\nc\nd\ne\n", "a\nB\nc\nd\ne\n") == ("a\nB\nc\nd\ne\n", True), "merge: same change")
    m, clean = merge3(base, "a\nX\nc\nd\ne\n", "a\nY\nc\nd\ne\n")
    check(not clean and m == "a\n<<<<<<< mine\nX\n=======\nY\n>>>>>>> theirs\nc\nd\ne\n", f"merge: conflict {m!r}")
    m, clean = merge3(base, base + "m\n", base + "t\n")
    check(not clean and "<<<<<<< mine\nm\n=======\nt\n>>>>>>> theirs\n" in m, "merge: insertions at the same point")
    check(merge3(base, base, "a\nc\nd\ne\n") == ("a\nc\nd\ne\n", True), "merge: their deletion")
    m, clean = merge3("x\r\ny\r\n", "x\r\nY1\r\n", "x\r\nY2\r\n")
    check(not clean and "<<<<<<< mine\r\n" in m and m.endswith(">>>>>>> theirs\r\n"), "merge: CRLF markers")
    m, clean = merge3("x\ny", "x\nP", "x\nQ")
    check(not clean and m == "x\n<<<<<<< mine\nP\n=======\nQ\n>>>>>>> theirs\n", f"merge: no final newline {m!r}")

    # ---------------------------------------------------------------- PBIR integrity (replaces the npm validator)
    import test_parity
    tmp = tempfile.mkdtemp(prefix="pbzself-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    rdir = os.path.join(root, "Parity.Report")
    clean_findings = [f for f in pbircheck.run(rdir) if f["severity"] == "error"]
    check(not clean_findings, f"clean report flagged: {clean_findings}")
    d = os.path.join(rdir, "definition")

    def edit(rel, fn):
        p = os.path.join(d, *rel.split("/"))
        data = json.load(open(p, encoding="utf-8"))
        fn(data)
        json.dump(data, open(p, "w", encoding="utf-8"), indent=2)

    edit("pages/pages.json", lambda j: j["pageOrder"].append("ghost"))
    edit("pages/p1/visuals/v_card/visual.json", lambda j: j.update(name="renamed"))
    edit("pages/p1/visuals/v_bar/visual.json", lambda j: j["position"].pop("width"))
    edit("pages/p1/visuals/v_slicer/visual.json", lambda j: j.update(parentGroupName="noGroup"))
    edit("pages/p1/page.json", lambda j: j.update(visualInteractions=[{"source": "v_bar", "target": "gone", "type": "NoFilter"}]))
    edit("bookmarks/bookmarks.json", lambda j: j["items"].append({"name": "BmGone"}))
    edit("bookmarks/BmUsed.bookmark.json", lambda j: j["explorationState"].update(activeSection="p9"))
    edit("reportExtensions.json", lambda j: j["entities"][0]["measures"].append(dict(j["entities"][0]["measures"][0])))
    os.makedirs(os.path.join(d, "pages", "p4"))
    codes = {f["code"] for f in pbircheck.run(rdir) if f["severity"] == "error"}
    for want in ("PAGE_ORDER_MISSING_PAGE", "VISUAL_NAME_MISMATCH", "VISUAL_POSITION", "GROUP_MISSING",
                 "INTERACTION_MISSING_VISUAL", "BOOKMARK_MISSING_FILE", "BOOKMARK_MISSING_PAGE",
                 "DUPLICATE_REPORT_MEASURE", "PAGE_JSON_MISSING"):
        check(want in codes, f"PBIR check missed {want}: {sorted(codes)}")
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- no third-party binaries at runtime
    src = ""
    for f in sorted(os.listdir(os.path.join(SKILL, "pbz"))):
        if f.endswith(".py") and f != "guard.py":   # the guard names tools only to block them
            src += open(os.path.join(SKILL, "pbz", f), encoding="utf-8").read()
    calls = re.findall(r"subprocess\.run\(\s*\[([^\],]+)", src)
    allowed = {"exe", "str(exe)", "BRIDGE", "sys.executable"}  # PowerShell, pbz-engine, optional Desktop bridge
    odd = [c.strip() for c in calls if c.strip() not in allowed and not c.strip().startswith(("REPORT_AUTHOR", "args"))]
    check(not odd, f"unexpected external executables: {odd}")
    check(not re.search(r"(?i)measure ?killer\.exe|tabulareditor\.exe|daxstudio|bravo\.exe|['\"]git['\"]", src),
          "a third-party binary is invoked")

    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
