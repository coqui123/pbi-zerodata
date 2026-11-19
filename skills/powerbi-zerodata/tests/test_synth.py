"""`pbz synth`: synthetic copy of the model with fake data, the marker that unlocks `synth-query`, and the
push-time refusal of that marker in real models.

Run: python tests/test_synth.py           (offline: build, TOM validation, keys, marker, refusals)
     python tests/test_synth.py --live    (also opens the synthetic model in Desktop and queries it)
     python tests/test_synth.py --provenance (Windows C# manifest and query-buffer acceptance fixtures)
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
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, desktop, dpapi, mirror, synth, tmdl, tokens, validate  # noqa: E402
from pbz.common import PbzError, engine_call  # noqa: E402

FIXTURE = os.path.join(SKILL, "tests", "fixture")


def _rows(text: str, table_col_names: list[str]) -> list[list[str]]:
    """Rows of the DATATABLE in a synthetic table file, as raw cell texts."""
    body = text[text.index("DATATABLE("):]
    rows = re.findall(r"^\t+ +\{(.*)\},?$", body, re.M)
    out = []
    for r in rows:
        cells = re.findall(r'"(?:[^"]|"")*"|[^,\s][^,]*', r)
        out.append([c.strip() for c in cells])
    return out


def offline(fails: list) -> None:
    def check(cond, msg):
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp(prefix="pbzsynth-")
    os.environ["PBZ_SYNTH_ROOT"] = os.path.join(tmp, "synth")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    real_texts = "\n".join(open(os.path.join(cur, f), encoding="utf-8", errors="replace").read()
                           for base in (project.model_dir, project.report_dir)
                           for cur, _d, fs in os.walk(base) for f in fs)
    res = synth.build(ws, project, rows=40, seed=3)
    check(res["valid"] is True, f"synthetic TMDL is not TOM-valid: {res}")
    check(set(res["tables_faked"]) == {"Sales", "Customer", "Orphan"}, f"faked tables: {res['tables_faked']}")
    sp = synth.synth_project(ws, project)
    sdef = os.path.join(sp.model_dir, "definition")
    smodel = tmdl.load_model(sdef)
    kinds = {t.name: sorted({k for _n, k, _e in t.partitions}) for t in smodel.tables.values()}
    check(all(set(k) <= {"calculated", "calculationgroup"} for k in kinds.values()), f"non-calculated partition: {kinds}")
    for c in smodel.tables["Sales"].columns.values():
        if not c.calculated:
            check(c.source_column == f"[{c.name}]", f"sourceColumn not rewritten: Sales[{c.name}] {c.source_column}")
    mtags = {n.props.get("lineageTag", "").strip() for d in tmdl.load_model(os.path.join(project.mirror_model, "definition")).docs.values()
             for n in d.nodes if n.props.get("lineageTag")}
    stags = {n.props.get("lineageTag", "").strip() for d in smodel.docs.values() for n in d.nodes if n.props.get("lineageTag")}
    check(stags and not (stags & mtags), "lineage tags were not regenerated")
    mt = open(os.path.join(sdef, "model.tmdl"), encoding="utf-8").read()
    m = re.search(r"(?m)^annotation PBZ_SYNTHETIC = (\w+)", mt)
    tags = [t.node.props["lineageTag"].strip() for t in smodel.tables.values() if not t.private and t.node is not None]
    check(m and m.group(1) == synth.marker_value(dpapi.load_key(ws.private), tags), "marker HMAC does not match")
    check(not os.path.exists(os.path.join(sdef, "expressions.tmdl")), "shared M expressions kept")
    check(json.load(open(os.path.join(sp.ws.root, ".pbz", "policy.json")))["synthetic"] is True, "synthetic policy missing")
    # keys: fact customer keys come from the customer dimension, with deliberate orphans at the end
    sales = open(os.path.join(sdef, "tables", "Sales.tmdl"), encoding="utf-8").read()
    cust = open(os.path.join(sdef, "tables", "Customer.tmdl"), encoding="utf-8").read()
    sales_cols = [c.name for c in smodel.tables["Sales"].columns.values() if not c.calculated]
    cust_cols = [c.name for c in smodel.tables["Customer"].columns.values() if not c.calculated]
    pk = {r[cust_cols.index("CustomerKey")] for r in _rows(cust, cust_cols)}
    fk = [r[sales_cols.index("CustomerKey")] for r in _rows(sales, sales_cols)]
    check(len(fk) == 40 and sum(v in pk for v in fk) == 38 and len(pk) == len(_rows(cust, cust_cols)),
          f"key integrity: {sum(v in pk for v in fk)}/{len(fk)} match, {len(pk)} keys")
    # literals and tokens the DAX compares a column with appear in its fake values
    tok = re.search(r"Customer\[Name\] = \"(«[^»]+»)\"", open(os.path.join(project.mirror_model, "definition", "tables",
                                                                    "Sales.tmdl"), encoding="utf-8").read())
    check(tok and tok.group(1) in cust, "the token a measure filters on is missing from the fake data")
    # nothing from the real files: their secrets never reach the synthetic copy
    blob = "\n".join(open(os.path.join(cur, f), encoding="utf-8", errors="replace").read()
                     for cur, _d, fs in os.walk(sp.ws.root) for f in fs)
    check(not any(s in blob for s in test_parity.SECRETS), "a real secret reached the synthetic copy")
    check(any(s in real_texts for s in test_parity.SECRETS), "sanity: secrets exist in the real files")
    # status / query refusals without Desktop
    check(synth.status(ws, project)["built"] is True, "status")
    try:
        synth.query(ws, project, "EVALUATE ROW(\"x\", 1)")
        check(False, "query without the synthetic model open must fail")
    except PbzError as e:
        check(e.code == "ENGINE_UNAVAILABLE", f"query refusal: {e.code}")
    # the marker can never be pushed into a real model
    mp = os.path.join(project.mirror_model, "definition", "model.tmdl")
    mtext = open(mp, encoding="utf-8", newline="").read()
    open(mp, "w", encoding="utf-8", newline="").write(mtext.replace("ref table Sales", f"annotation PBZ_SYNTHETIC = {m.group(1)}\n\nref table Sales", 1))
    check(any(f["code"] == "SYNTHETIC_MARKER_IN_REAL_MODEL" for f in validate.run_mirror(ws, project)), "validate misses the marker")
    stage = os.path.join(tmp, "stage")
    shutil.copytree(project.model_dir, os.path.join(stage, os.path.basename(project.model_dir)))
    smp = os.path.join(stage, os.path.basename(project.model_dir), "definition", "model.tmdl")
    open(smp, "w", encoding="utf-8", newline="").write(open(smp, encoding="utf-8").read() + f"\nannotation PBZ_SYNTHETIC = {m.group(1)}\n")
    validate.report_author = lambda *a, **k: []
    found = validate.run_stage(ws, project, stage, tokens.TokenMap(ws.private))
    check(any(f["code"] == "SYNTHETIC_MARKER_IN_REAL_MODEL" and f["severity"] == "error" for f in found), "push stage misses the marker")
    open(mp, "w", encoding="utf-8", newline="").write(mtext)
    # remove: only a marked synthetic folder
    check(synth.remove(ws, project)["result"] == "REMOVED" and not os.path.exists(sp.ws.root), "remove")
    os.makedirs(os.path.join(sp.ws.root, "something"))
    try:
        synth.remove(ws, project)
        check(False, "remove must refuse an unmarked folder")
    except PbzError as e:
        check(e.code == "UNSAFE", f"remove refusal: {e.code}")
    shutil.rmtree(tmp, ignore_errors=True)


def _kill(path_fragment: str) -> None:
    """Test harness only: end Desktop instances whose command line holds this temp path (and their engines)."""
    frag = path_fragment.replace("'", "''")
    script = ("$procs = Get-CimInstance Win32_Process -Filter \"Name='PBIDesktop.exe'\" | Where-Object { $_.CommandLine -like '*"
              + frag + "*' }; foreach ($p in $procs) { Get-CimInstance Win32_Process -Filter \"Name='msmdsrv.exe'\" | "
              "Where-Object { $_.ParentProcessId -eq $p.ProcessId } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
              "Stop-Process -Id $p.ProcessId -Force }")
    for exe in ("pwsh", "powershell"):
        try:
            subprocess.run([exe, "-NoProfile", "-Command", script], capture_output=True, timeout=120)
            break
        except FileNotFoundError:
            continue
    time.sleep(3)


def live(fails: list) -> None:
    def check(cond, msg):
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp(prefix="pbzsynthlive-")
    os.environ["PBZ_SYNTH_ROOT"] = os.path.join(tmp, "synth")
    ws_dir = os.path.join(tmp, "ws")
    shutil.copytree(FIXTURE, ws_dir, ignore=shutil.ignore_patterns(".pbi", "pbz-workspace", ".pbz", "CANARIES.md"))
    ws = common.Workspace(ws_dir)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    res = synth.build(ws, project, rows=60, seed=11)
    check(res["valid"], f"canary synthetic build invalid: {res}")
    try:
        op = synth.open_(ws, project, wait=480)
        check(op.get("open") and op.get("engine") == "READY", f"synth open: {op}")
        q = synth.query(ws, project, "EVALUATE ROW(\"n\", COUNTROWS('Hours'))")
        check(q["rows"] == [[60]], f"row count query: {q}")
        q = synth.query(ws, project, "EVALUATE SUMMARIZECOLUMNS('Project'[Status], \"h\", [Total Hours])")
        check(len(q["rows"]) >= 2 and all(r[1] is not None for r in q["rows"]), f"measure by category: {q}")
        q = synth.query(ws, project, "EVALUATE ROW(\"x\", [Canary Person Hours])")
        check(q["rows"] and q["rows"][0][0] is not None, f"token-filtered measure returns a value on fake data: {q}")
        # the marker binds the key: another workspace key cannot unlock the same model
        sp = synth.synth_project(ws, project)
        port = desktop.engine_port(int(desktop.find_instance(sp)["pid"]))
        other = os.path.join(tmp, "other-private")
        r = engine_call("synth-query", {"private_dir": other, "port": port, "dax": "EVALUATE ROW(\"a\", 1)"})
        check(not r.get("ok") and r.get("class") == "BAD_REQUEST", f"marker with a different key: {r}")
        # a non-synthetic model (the fixture copy, with an M partition and no marker) is refused
        op = desktop.open_(project, 480)
        rport = desktop.require_port(project)
        r = engine_call("synth-query", {"private_dir": ws.private, "port": rport, "dax": "EVALUATE ROW(\"a\", 1)"})
        check(op.get("open") and not r.get("ok") and r.get("class") == "BAD_REQUEST" and "rows" not in r,
              f"non-synthetic model not refused: {r}")
    finally:
        for proj in (synth.synth_project(ws, project), project):
            if desktop.find_instance(proj):
                desktop.close(proj, wait=30)
            if desktop.find_instance(proj):
                _kill(proj.ws.root)
    check(not desktop.find_instance(synth.synth_project(ws, project)) and not desktop.find_instance(project),
          "a Desktop instance stayed open")
    shutil.rmtree(tmp, ignore_errors=True)


# These fixtures compile and execute the production C# manifest and buffering code on Windows.
# They are deliberately opt-in and fail if the SDK is unavailable; Linux cannot run DPAPI/TOM.
PROVENANCE_FIXTURES = r'''
using System;
using System.IO;
using System.Text.Json.Nodes;
using Microsoft.AnalysisServices.Tabular;
using PbzEngine;
using P = PbzEngine.SyntheticProvenance;

class Fixtures
{
    static Model Build()
    {
        var database = new Database { Name = "Fixture", CompatibilityLevel = 1600 };
        var model = new Model();
        database.Model = model;
        foreach (var name in new[] { "T", "Other", "Private", "LocalDateTable_existing" })
        {
            var table = new Table { Name = name, LineageTag = "tag-" + name };
            if (name == "Private") { table.IsPrivate = true; table.LineageTag = null; }
            table.Columns.Add(new CalculatedTableColumn { Name = "X", SourceColumn = "[X]", DataType = DataType.Int64 });
            table.Partitions.Add(new Partition { Name = "P", Source = new CalculatedPartitionSource {
                Expression = "DATATABLE(\"X\", INTEGER, {{1}})" } });
            model.Tables.Add(table);
        }
        model.Tables["T"].Measures.Add(new Measure { Name = "M", Expression = "SUM('T'[X])", FormatString = "0" });
        model.Tables["T"].Columns.Add(new CalculatedColumn { Name = "C", Expression = "[X] + 1", DataType = DataType.Int64 });
        var group = new Table { Name = "CG", CalculationGroup = new CalculationGroup() };
        group.Partitions.Add(new Partition { Name = "CG", Source = new CalculationGroupSource() });
        group.CalculationGroup.CalculationItems.Add(new CalculationItem { Name = "Item", Expression = "SELECTEDMEASURE()" });
        model.Tables.Add(group);
        return model;
    }

    static void Main(string[] args)
    {
        var ctx = new Ctx("synth-query");
        ctx.Init(Req.Parse(new JsonObject { ["private_dir"] = args[0] }.ToJsonString()));
        var marker = new string('a', 64);
        var definition = P.Definition(Build());
        P.Save(ctx, marker, definition, "catalog-A");
        var good = new P.Snapshot(marker, "catalog-A", definition, "v1");
        int passed = 0;
        void Check(string name, Action action, bool refused)
        {
            try { action(); if (refused) throw new Exception(name + ": accepted"); }
            catch (BadRequestException) { if (!refused) throw; }
            passed++;
            Console.WriteLine("PASS " + name);
        }
        Check("passing manifest", () => P.Verify(ctx, good), false);
        void Mutation(string name, Action<Model> mutate)
        {
            var model = Build(); mutate(model);
            Check(name, () => P.Buffered<int>(
                () => good with { DefinitionValue = P.Definition(model) }, s => P.Verify(ctx, s),
                () => throw new Exception("query ran on changed model")), true);
        }
        Mutation("changed measure", m => m.Tables["T"].Measures["M"].Expression = "42");
        Mutation("changed calculated table", m => ((CalculatedPartitionSource)m.Tables["T"].Partitions[0].Source).Expression = "ROW(\"X\", 1)");
        Mutation("edited DATATABLE row", m => ((CalculatedPartitionSource)m.Tables["T"].Partitions[0].Source).Expression = "DATATABLE(\"X\", INTEGER, {{123456}})");
        Mutation("added column", m => m.Tables["T"].Columns.Add(new CalculatedColumn { Name = "Added", Expression = "1", DataType = DataType.Int64 }));
        Table Extra(string name, bool data = false) {
            var t = new Table { Name = name };
            t.Partitions.Add(new Partition { Name = "P", Source = data ? new MPartitionSource { Expression = "secret" } : new CalculatedPartitionSource { Expression = "ROW(\"X\", 1)" } });
            return t;
        }
        Mutation("added untagged table", m => m.Tables.Add(Extra("U")));
        Mutation("private-prefixed table", m => m.Tables.Add(Extra("LocalDateTable_secret")));
        Mutation("private table with data partition", m => m.Tables["Private"].Partitions[0].Source = new MPartitionSource { Expression = "secret" });
        // Verify private and untagged tables present at signing remain covered, not just additions.
        var privateModel = Build(); var hidden = Extra("LocalDateTable_signed"); hidden.IsPrivate = true;
        privateModel.Tables.Add(hidden);
        var privateDefinition = P.Definition(privateModel);
        if (!privateDefinition.Contains("LocalDateTable_signed")) throw new Exception("private table omitted");
        Mutation("removed table", m => m.Tables.Remove("Other"));
        Mutation("calculated column expression", m => ((CalculatedColumn)m.Tables["T"].Columns["C"]).Expression = "42");
        Mutation("calculation item expression", m => m.Tables["CG"].CalculationGroup.CalculationItems["Item"].Expression = "42");
        Mutation("private table expression", m => ((CalculatedPartitionSource)m.Tables["Private"].Partitions[0].Source).Expression = "ROW(\"X\", 42)");
        Mutation("private-prefixed table expression", m => ((CalculatedPartitionSource)m.Tables["LocalDateTable_existing"].Partitions[0].Source).Expression = "ROW(\"X\", 42)");
        Mutation("format string", m => m.Tables["T"].Measures["M"].FormatString = "secret");
        Check("marker replay different catalog", () => P.Verify(ctx, good with { Catalog = "catalog-B" }), true);
        Check("missing marker", () => P.Verify(ctx, good with { MarkerValue = "" }), true);
        var manifestPath = Path.Combine(args[0], "synth-manifest-" + marker + ".bin");
        var signed = JsonNode.Parse(System.Text.Encoding.UTF8.GetString(Dpapi.Unprotect(File.ReadAllBytes(manifestPath))))!.AsObject();
        signed["definition"] = "tampered";
        Vault.WriteProtected(manifestPath, signed.ToJsonString());
        Check("invalid manifest signature", () => P.Verify(ctx, good), true);
        P.Save(ctx, marker, definition, "catalog-A");
        int queries = 0;
        Check("verification unavailable", () => P.Buffered(
            () => good with { MarkerValue = new string('b', 64) }, s => P.Verify(ctx, s), () => ++queries), true);
        if (queries != 0) throw new Exception("query ran without verification");
        Check("live verification unavailable before query", () => P.Buffered<int>(
            () => throw new BadRequestException("SYNTH_VERIFICATION_UNAVAILABLE: live definition unavailable"),
            s => P.Verify(ctx, s), () => ++queries), true);
        if (queries != 0) throw new Exception("query ran without live verification");
        int captures = 0;
        Check("edit between verification and execution", () => P.Buffered(
            () => ++captures == 1 ? good : good with { DefinitionValue = "edited" },
            s => P.Verify(ctx, s), () => ++queries), true);
        if (queries != 1 || captures != 2) throw new Exception("race fixture did not execute both checks");
        captures = 0;
        Check("version changed while definition restored", () => P.Buffered(
            () => ++captures == 1 ? good : good with { Version = "v2" },
            s => P.Verify(ctx, s), () => ++queries), true);
        captures = 0;
        Check("live verification unavailable after query", () => P.Buffered(
            () => ++captures == 1 ? good : throw new BadRequestException("SYNTH_VERIFICATION_UNAVAILABLE: live definition unavailable"),
            s => P.Verify(ctx, s), () => ++queries), true);
        Check("verified rows released", () => {
            if (P.Buffered(() => good, s => P.Verify(ctx, s), () => 7) != 7) throw new Exception("lost rows");
        }, false);
        Console.WriteLine($"{passed} passed, 0 failed");
    }
}
'''


def provenance_fixtures() -> int:
    if os.name != "nt" or not shutil.which("dotnet"):
        print("FAIL provenance fixtures require Windows DPAPI, TOM and the .NET 10 SDK")
        return 2
    from xml.sax.saxutils import escape
    with tempfile.TemporaryDirectory(prefix="pbz-provenance-") as tmp:
        with open(os.path.join(tmp, "Fixtures.csproj"), "w", encoding="utf-8") as f:
            f.write('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                    '<TargetFramework>net10.0</TargetFramework><RuntimeIdentifier>win-x64</RuntimeIdentifier>'
                    '</PropertyGroup><ItemGroup><ProjectReference Include="'
                    + escape(os.path.join(SKILL, "engine", "PbzEngine.csproj"))
                    + '" /></ItemGroup></Project>')
        with open(os.path.join(tmp, "Program.cs"), "w", encoding="utf-8") as f:
            f.write(PROVENANCE_FIXTURES)
        return subprocess.run(["dotnet", "run", "--project", os.path.join(tmp, "Fixtures.csproj"),
                               "--", os.path.join(tmp, "private")], timeout=300).returncode


def require_live() -> None:
    from pbz.engine import ENGINE_EXE
    if os.name != "nt" or not ENGINE_EXE.is_file():
        raise RuntimeError("live tests require Windows, a built broker, and Power BI Desktop; refusing to skip")


def main() -> int:
    if "--provenance" in sys.argv:
        return provenance_fixtures()
    if "--live" in sys.argv:
        require_live()
    fails: list[str] = []
    offline(fails)
    if "--live" in sys.argv:
        live(fails)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
