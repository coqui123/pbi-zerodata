"""End-to-end canary test: every pbz verb against a copy of the synthetic fixture, opened in Power BI Desktop.

Proves that the canaries planted in tests/fixture (listed in CANARIES.md) never appear in anything an
agent can see: pbz stdout/stderr, the mirror (pbz-workspace/) and .pbz/ (policy, logs, push log, handoff).
Two kinds of canary are visible by design and exempted explicitly: values of category columns (labels
are readable in the mirror) and identifiers written into DAX (formulas are readable).

Run: python tests/test_e2e.py     (opens a Desktop instance on a temp copy; several minutes)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(SKILL, "pbz", "cli.py")
FIXTURE = os.path.join(SKILL, "tests", "fixture")
sys.path.insert(0, SKILL)

from pbz import dpapi  # noqa: E402
from pbz.common import Workspace  # noqa: E402

def blocks() -> dict[str, list[str]]:
    text = open(os.path.join(FIXTURE, "CANARIES.md"), encoding="utf-8").read()
    out = {}
    for m in re.finditer(r"```([\w-]+)\n(.*?)```", text, re.S):
        out[m.group(1)] = [l.strip() for l in m.group(2).splitlines() if l.strip()]
    return out


class Runner:
    def __init__(self, ws: str, private_root: str):
        self.ws = ws
        self.synth_root = os.path.join(os.path.dirname(private_root), "synth")  # keep `pbz synth` inside the temp dir
        self.env = {**os.environ, "PBZ_PRIVATE_ROOT": private_root, "PBZ_SYNTH_ROOT": self.synth_root, "PYTHONUTF8": "1"}
        self.outputs: list[str] = []
        self.log: list[str] = []

    def pbz(self, *args, timeout=1800) -> dict:
        p = subprocess.run([sys.executable, "-I", CLI, *args], cwd=self.ws, env=self.env,
                           capture_output=True, timeout=timeout)
        out = p.stdout.decode("utf-8", errors="replace")
        err = p.stderr.decode("utf-8", errors="replace")
        self.outputs += [out, err]  # agent-visible; the request text itself is the agent's own words
        self.log.append(f"pbz {args[0]} -> rc {p.returncode}")
        try:
            return json.loads(out)
        except ValueError:
            return {"_unparsed": True}


def ps(cmd: str) -> str:
    return subprocess.run(["pwsh", "-NoProfile", "-Command", cmd], capture_output=True, timeout=120).stdout.decode(
        "utf-8", errors="replace")


def kill_instance(ws: str) -> None:
    """Test harness only: end the Desktop instance holding THIS temp copy (and its engine child)."""
    pat = ws.replace("\\", "\\\\")
    ps("$procs = Get-CimInstance Win32_Process -Filter \"Name='PBIDesktop.exe'\" | Where-Object { $_.CommandLine -like '*"
       + ws.replace("'", "''") + "*' }; foreach ($p in $procs) { Get-CimInstance Win32_Process -Filter \"Name='msmdsrv.exe'\" | "
       "Where-Object { $_.ParentProcessId -eq $p.ProcessId } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
       "Stop-Process -Id $p.ProcessId -Force }")
    time.sleep(3)


def close_desktop(r: Runner) -> None:
    res = r.pbz("desktop", "close", timeout=300)
    if res.get("open"):
        kill_instance(r.ws)  # Desktop asked to save (it upgrades the compat level on open); fixture copy only


def visible_text(r: Runner) -> str:
    parts = list(r.outputs)
    for base in (os.path.join(r.ws, "pbz-workspace"), os.path.join(r.ws, ".pbz"), r.synth_root):  # synth is agent-readable
        for cur, _d, files in os.walk(base):
            for f in files:
                with open(os.path.join(cur, f), encoding="utf-8", errors="replace") as fh:
                    parts.append(fh.read())
    return "\n".join(parts)


def main() -> int:
    b = blocks()
    strings = b["canary-strings"] + b["canary-blob-strings"] + b["canary-request-strings"]
    numbers = b["canary-numbers"] + b["canary-derived-numbers"]
    stems = b["canary-stems"]
    # this file must not contain canary literals (the engine test scans it): pick them from CANARIES.md
    person = b["canary-strings"][0]
    email = next(s for s in b["canary-strings"] if s.endswith("@example.invalid"))
    hours_value = b["canary-numbers"][0]
    req_errfn, req_conv, req_col = b["canary-request-strings"][0:3]
    identifier_canaries = {s for s in b["canary-strings"] if "_NOCOL_" in s}  # column name in a broken measure
    tmp = tempfile.mkdtemp(prefix="pbz-e2e-")
    ws_dir = os.path.join(tmp, "ws")
    shutil.copytree(FIXTURE, ws_dir, ignore=shutil.ignore_patterns(".pbi", "pbz-workspace", ".pbz", "CANARIES.md"))
    private_root = os.path.join(tmp, "private")
    r = Runner(ws_dir, private_root)
    fails: list[str] = []

    def expect(cond, msg):
        if not cond:
            fails.append(msg)

    try:
        expect(r.pbz("init").get("verb") == "init", "init")
        res = r.pbz("desktop", "open", "--wait", "480", timeout=900)
        expect(res.get("open") and res.get("engine") == "READY", f"desktop open: {res}")
        res = r.pbz("refresh")
        expect(res.get("status") in ("PASS", "UNSUPPORTED"), f"refresh: {res}")
        res = r.pbz("index")
        expect(res.get("verb") == "index", f"index: {res}")
        res = r.pbz("pull", "--force")
        expect(res.get("classification") == "index", f"pull mode: {res}")
        expect(not res.get("lossy_files"), f"lossy: {res.get('lossy_files')}")
        ov = r.pbz("describe")
        tables = [t["table"] for t in ov.get("tables", [])]
        expect({"Person", "Project", "Hours"} <= set(tables), f"overview: {tables}")
        for t in tables:
            r.pbz("describe", t)
            if t != "Notes":
                r.pbz("probe", t)
        for m in ("[Total Hours]", "[Active Hours]", "[Canary Person Hours]", "[Broken Measure]"):
            r.pbz("describe", m)
        res = r.pbz("values", "'Project'[Status]")
        expect(set(res.get("values", [])) >= {"Active", "Closed"}, f"category labels: {res}")
        res = r.pbz("values", "'Person'[Name]")
        expect("values" not in res and res.get("class") == "sensitive", f"sensitive column listed: {res}")
        res = r.pbz("health")
        expect(any(e.get("name") == "Broken Measure" for e in res.get("errors", [])), f"health: {res}")
        spec = {"checks": [
            {"id": "nb", "kind": "nonblank", "expr": "[Total Hours]"},
            {"id": "eq", "kind": "equal", "left": "[Total Hours]", "right": "SUM('Hours'[Hours])", "by": ["'Person'[Name]"]},
            {"id": "df", "kind": "differs", "left": "[Total Hours]", "right": "[Active Hours]", "by": ["'Project'[Status]"]},
            {"id": "pk", "kind": "unique", "column": "'Project'[ProjectID]"},
            {"id": "fk", "kind": "no_orphans", "from": "'Hours'[ProjectID]", "to": "'Project'[ProjectID]"},
            {"id": "bk", "kind": "no_blank_keys", "column": "'Hours'[Person]"},
            {"id": "sg", "kind": "sign", "expr": "[Total Hours]"},
            {"id": "fi", "kind": "filter_independent", "expr": "[Total Hours]", "column": "'ExceptionType'[Label]"},
            {"id": "cmp", "kind": "compiles", "table": "Hours", "expr": f"SUM('Hours'[{req_col}])"},
            {"id": "err", "kind": "executes", "expr": f"ERROR(\"{req_errfn}\")"},
            {"id": "conv", "kind": "executes", "expr": f"VALUE(\"{req_conv}\")"},
        ]}
        res = r.pbz("check", json.dumps(spec))
        st = {x.get("id"): x.get("status") for x in res.get("results", [])}
        expect(st.get("nb") == "PASS", f"nonblank: {st}")
        expect(st.get("eq") == "PASS", f"equal by person: {st}")
        expect(st.get("df") == "PASS", f"differs: {st}")
        expect(st.get("pk") == "PASS", f"unique: {st}")
        expect(st.get("fk") == "FAIL", f"planted orphans: {st}")
        expect(st.get("bk") == "FAIL", f"planted blank key: {st}")
        expect(st.get("fi") == "PASS", f"island table filter: {st}")
        expect(st.get("cmp") == "ERROR", f"compiles: {st}")
        expect(st.get("err") == "ERROR" and st.get("conv") == "ERROR", f"forced errors: {st}")
        q = {"queries": [{"id": "by-person", "keys": ["'Person'[Name]"],
                          "dax": "EVALUATE SUMMARIZECOLUMNS('Person'[Name], \"h\", [Total Hours])"}]}
        expect(r.pbz("snapshot", "save", "base", "--spec", json.dumps(q)).get("verb") == "snapshot-save", "snapshot save")
        res = r.pbz("snapshot", "compare", "base")
        expect([x.get("result") for x in res.get("queries", [])] == ["SAME"], f"snapshot compare: {res}")
        res = r.pbz("smoke")
        expect(res.get("verb") == "smoke" and res.get("summary"), f"smoke: {res}")
        # parity verbs (metadata only): usage states, engine cross-check, lineage, cleanup, BPA, docs
        res = r.pbz("unused")
        expect(res.get("verb") == "unused" and res.get("analysis") == "complete", f"unused: {res}")
        res = r.pbz("unused", "--verify-engine")
        expect(res.get("result") in ("MATCH", "DIFF") and res.get("edges", {}).get("engine", 0) > 0
               and not res.get("engine_only"), f"verify-engine: {res}")
        for args in (("unused", "--all", "--impact"), ("unused", "--remove", "--dry-run"), ("lineage", "[Total Hours]"),
                     ("lineage", "[Canary Person Hours]", "--up"), ("suggest",), ("m-lineage",), ("bpa",),
                     ("bpa", "model", "--labels", "measure-killer"), ("similar",), ("changes",), ("changes", "--list"),
                     ("docs", "--dax"), ("docs", "--format", "html", "--dax"), ("docs", "--format", "json")):
            res = r.pbz(*args)
            expect(res.get("verb") and res.get("ok") is not False, f"{' '.join(args)}: {res}")
        for args in (("status",), ("layout",), ("layout", "--page", "Canary Page"), ("format",), ("repair", "--dry-run"),
                     ("bpa", "--fix", "HIDE_FOREIGN_KEYS", "--dry-run")):
            res = r.pbz(*args)
            expect(res.get("verb") and res.get("ok") is not False, f"{' '.join(args)}: {res}")
        # authoring verbs: their output must pass the staged validation (TOM + Microsoft's PBIR validator)
        res = r.pbz("new", "measure-table", "E2E Measures")
        expect(res.get("verb") == "new", f"new measure-table: {res}")
        res = r.pbz("move", "measure", "Active Hours", "--to", "E2E Measures")
        expect(res.get("verb") == "move", f"move measure: {res}")
        res = r.pbz("add", "page", "E2E Page")
        expect(res.get("verb") == "add", f"add page: {res}")
        made = {}
        for vargs in (("card", "--field", "[Total Hours]", "--title", "Hours"),
                      ("clusteredColumnChart", "--field", "'Project'[Status]", "--field", "[Total Hours]"),
                      ("tableEx", "--field", "'Project'[Status]", "--field", "[Active Hours]"),
                      ("slicer", "--field", "'Project'[Status]"),
                      ("kpi", "--field", "[Total Hours]", "--field", "TrendLine='Project'[Status]"),
                      ("gauge", "--field", "[Total Hours]"), ("cardVisual", "--field", "[Active Hours]"),
                      ("actionButton",)):
            res = r.pbz("add", "visual", vargs[0], "--page", "E2E Page", *vargs[1:])
            expect(res.get("verb") == "add" and res.get("placement") == "free_spot", f"add visual {vargs[0]}: {res}")
            made[vargs[0]] = res.get("id", "?")
        # images: a staged PNG on the canvas, in an image visual and on a button; push installs the file
        from test_images import png
        logo = os.path.join(tmp, "E2E Logo.png")
        with open(logo, "wb") as f:
            f.write(png(240, 80))
        res = r.pbz("image", "add", logo)
        img = res.get("item", "?")
        expect(res.get("result") == "STAGED", f"image add: {res}")
        for args in (("style", "E2E Page", "--canvas", "--set", f"background.image={img}", "--set", "background.scaling=fit",
                      "--set", "wallpaper.color=#F3F2F1"),
                     ("add", "visual", "image", "--page", "E2E Page", "--image", img, "--alt", "E2E logo"),
                     ("style", "E2E Page", made["actionButton"], "--state", "default", "--set", f"fill.image={img}"),
                     ("image", "list")):
            res = r.pbz(*args)
            expect(res.get("verb") and res.get("ok") is not False, f"{' '.join(args)}: {res}")
        # pages, filters, interactions and links: Microsoft's validator checks them on push, Desktop on reopen
        for args in (("add", "page", "E2E Tip", "--tooltip"),
                     ("add", "page", "E2E Detail", "--drillthrough", "'Project'[Status]"),
                     ("filter", "add", "'Project'[Status]", "--page", "E2E Page", "--in", "Active"),
                     ("filter", "add", "'Hours'[Hours]", "--report", "--where", ">= 0"),
                     ("filter", "add", "'Person'[Name]", "--page", "E2E Page", "--visual", made["tableEx"],
                      "--top", "3", "--by", "[Total Hours]"),
                     ("filter", "add", "[Total Hours]", "--page", "E2E Page", "--visual", made["card"], "--where", "> 0"),
                     ("filter", "list"),
                     ("interaction", "E2E Page", made["clusteredColumnChart"], made["tableEx"], "highlight"),
                     ("link", "E2E Page", made["actionButton"], "--drillthrough", "E2E Detail"),
                     ("link", "E2E Page", made["clusteredColumnChart"], "--tooltip", "E2E Tip"),
                     ("bookmark", "add", "E2E Chart Only", "--page", "E2E Page", "--hide", made["tableEx"]),
                     ("bookmark", "list"),
                     ("copy", "page", "E2E Page", "--as", "E2E Page Copy"),
                     ("copy", "visual", "E2E Page", made["card"], "--to-page", "E2E Tip"),
                     ("style", "E2E Page", made["card"], "--set", "container.general.altText=Total hours card"),
                     ("a11y", "--page", "E2E Page", "--fix-tab-order"), ("a11y",),
                     ("mobile", "E2E Page", "all"),
                     ("set", "[Total Hours]", "displayFolder=E2E", "description=Hours in total"),
                     ("set", "--table", "Hours", "--kind", "column", "--match", "ID$", "isHidden=true"),
                     ("hierarchy", "add", "DimDate", "E2E Calendar", "--level", "Year", "--level", "Month Number"),
                     ("format-expr", "[Total Hours]", "\"#,0.0\"", "--upgrade-compat"),
                     ("function", "add", "E2ETax", "--params", "(x : NUMERIC)", "--body", "x * 1.1", "--upgrade-compat"),
                     ("role", "add", "E2E Role"), ("role", "ols", "E2E Role", "'Person'[Email]", "none"),
                     ("recipe", "run", json.dumps({"recipe": "e2e", "steps": [
                         {"verb": "set", "args": ["[Active Hours]", "displayFolder=E2E"]},
                         {"verb": "style", "args": ["E2E Page", "--type", "gauge", "--set", "container.title.show=true"]}]})),
                     ("review",),
                     ("audit", "memory"), ("audit", "--coverage", "--save-baseline", "e2e"), ("audit", "--since", "e2e"),
                     ("consolidate", "families"),
                     ("consumers", "discover", ws_dir)):
            res = r.pbz(*args)
            expect(res.get("verb") and res.get("ok") is not False, f"{' '.join(args)}: {res}")
        for args in (("style", "E2E Page", "--type", "clusteredColumnChart", "--set", "labels.show=true",
                      "--set", "title.text=Hours by status", "--set", "dataPoint.fill=theme:2:-25",
                      "--set", "legend.position=Top"),
                     ("style", "E2E Page", "--all", "--set", "container.border.show=true"),
                     ("style", "E2E Page", "--type", "card", "--set", "labels.fontSize=20", "--set", "labels.color=#118DFF"),
                     ("style", "--list", "--type", "kpi")):
            res = r.pbz(*args)
            expect(res.get("verb") == "style" and res.get("ok") is not False
                   and (res.get("changed") or res.get("action") == "list"), f"{' '.join(args)}: {res}")
        for args in (("culture", "add", "fr-FR"), ("translate", "fr-FR", "Hours", "--caption", "Heures"),
                     ("translate", "fr-FR", "[Total Hours]", "--caption", "Heures totales"),
                     ("perspective", "add", "E2E View"), ("perspective", "include", "E2E View", "Project", "[Total Hours]"),
                     ("role", "filter", "E2E Role", "Project", "[Status] = \"Active\""),
                     ("refresh-policy", "show"), ("synth", "build"),
                     ("consumers", "add", "--object", "[Active Hours]", "--label", "E2E pack"), ("consumers", "list"),
                     ("visual", "list"), ("visual", "roles", "kpi")):
            res = r.pbz(*args)
            expect(res.get("verb") and res.get("ok") is not False, f"{' '.join(args)}: {res}")
        res = r.pbz("unused", "--all")
        ah = next((o for o in res.get("objects", []) if o.get("name") == "Active Hours"), {})
        expect(res.get("scope", {}).get("consumers") and ah.get("state") in ("used", "used_external"),
               f"unused with a registered consumer: {ah} {res.get('scope')}")
        res = r.pbz("validate")
        expect(not [f for f in res.get("findings", []) if f.get("severity") == "error"], f"validate after authoring: {res}")
        tdir = os.path.join(ws_dir, "pbz-workspace", "Canary.SemanticModel", "definition", "tables")
        with open(os.path.join(tdir, "Hours.tmdl"), "a", encoding="utf-8", newline="") as f:
            f.write("\n\tmeasure 'E2E Added' = [Total Hours] * 2\n\t\tformatString: 0\n")
        expect(r.pbz("push").get("result") == "DESKTOP_OPEN", "push with Desktop open not refused")
        close_desktop(r)
        res = r.pbz("push")
        expect(res.get("result") == "PUSHED", f"push: {res}")
        expect(img in (res.get("images_installed") or []), f"push did not install the image: {res.get('images_installed')}")
        # Desktop must open the authored report (new page types, filters, links, formatting, images) cleanly
        res = r.pbz("desktop", "open", "--wait", "480", timeout=900)
        expect(res.get("open") and res.get("engine") == "READY", f"desktop reopen after authoring: {res}")
        res = r.pbz("health")
        expect(res.get("verb") == "health" and res.get("ok") is not False, f"health after authoring: {res}")
        res = r.pbz("smoke", "--page", "E2E Page")
        expect(res.get("verb") == "smoke" and res.get("summary"), f"smoke on the authored page: {res}")
        close_desktop(r)
        expect(r.pbz("handoff").get("verb") == "handoff", "handoff")
        memdir = os.path.join(tmp, "memory")
        os.makedirs(memdir)
        with open(os.path.join(memdir, "note.md"), "w", encoding="utf-8") as f:
            f.write(f"Owner {person} logged {hours_value} hours; 42% blank; cost $1,234; contact {email}.\n")
        res = r.pbz("memscrub", "--dir", memdir, "--show", "note.md")
        expect("scrubbed_text" in res, f"memscrub: {res}")
    finally:
        close_desktop(r)

    # exemptions: labels of columns pbz itself treats as categories (readable by design), and
    # identifiers written into DAX. Personal / ID columns must never be exempt.
    from pbz import policy as policy_mod, tmdl as tmdl_mod
    from pbz.classify import Classifier
    ws = Workspace(ws_dir)
    ws.private = os.path.join(private_root, ws.id)
    clf = Classifier(tmdl_mod.load_model(os.path.join(ws_dir, "Canary.SemanticModel", "definition")), ws.private,
                     policy_mod.load(ws))
    raw = dpapi.read_protected(os.path.join(ws.private, "categories.bin"))
    category_values = set()
    for lab, vals in (json.loads(raw) if raw else {}).items():
        t, c = lab[:lab.index("[")], lab[lab.index("[") + 1:-1]
        if clf.column_class(t, c) == "category":
            category_values |= {v for v in vals if isinstance(v, str)}
    exempt = identifier_canaries | {s for s in strings if s in category_values}
    sensitive_cols = ["Person[Name]", "Person[Email]", "Project[Title]", "ExceptionType[Code]", "Hours[Person]"]
    expect(all(clf.column_class(*[x.rstrip("]") for x in s.split("[")]) == "sensitive" for s in sensitive_cols),
           "a personal / ID column is not classified sensitive")
    text = visible_text(r)
    scan = text
    for e in sorted(exempt, key=len, reverse=True):
        scan = scan.replace(e, "")
    leaks = [s for s in strings if s not in exempt and s.lower() in scan.lower()]
    leaks += [n for n in numbers if re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", scan)]
    leaks += [s for s in stems if s.lower() in scan.lower()]
    expect(not leaks, f"CANARY LEAK: {sorted(set(leaks))}")
    print("exempt (by design):", sorted(exempt))
    print("steps:", "; ".join(r.log))
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    if fails:
        print("workspace kept at", tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
