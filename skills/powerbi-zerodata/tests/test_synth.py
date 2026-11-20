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
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Text.Json.Nodes;
using Microsoft.AnalysisServices.Tabular;
using PbzEngine;
using P = PbzEngine.SyntheticProvenance;

class Fixtures
{
    // TOM creates RowNumber columns internally and the pinned AMO 19.118.3 exposes no
    // public RowNumberColumn constructor, so the fixture builds one through the
    // non-public constructor to exercise Provenance.Verify's RowNumber counting.
    static RowNumberColumn MakeRowNumber(string name)
    {
        try
        {
            var column = (RowNumberColumn)Activator.CreateInstance(typeof(RowNumberColumn),
                BindingFlags.Instance | BindingFlags.NonPublic, null, null, null)!;
            column.Name = name;
            return column;
        }
        catch (Exception ex)
        {
            throw new Exception("MakeRowNumber: RowNumberColumn has no usable non-public constructor in the pinned AMO 19.118.3", ex);
        }
    }

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
        model.Tables["T"].Columns.Add(MakeRowNumber("RowNumber"));
        model.Tables["T"].Measures.Add(new Measure { Name = "M", Expression = "SUM('T'[X])", FormatString = "0" });
        model.Tables["T"].Columns.Add(new CalculatedColumn { Name = "C", Expression = "[X] + 1", DataType = DataType.Int64 });
        var group = new Table { Name = "CG", CalculationGroup = new CalculationGroup() };
        group.Partitions.Add(new Partition { Name = "CG", Source = new CalculationGroupSource() });
        group.CalculationGroup.CalculationItems.Add(new CalculationItem { Name = "Item", Expression = "SELECTEDMEASURE()" });
        model.Tables.Add(group);
        return model;
    }

    static int ChildValidate(string[] args)
    {
        var resultFile = args[7];
        try
        {
            var privateDir = args[2];
            var folder = args[3];
            var timeoutSec = int.Parse(args[4]);
            var gate = args[5];
            using var paused = EventWaitHandle.OpenExisting(args[6] + "-paused");
            using var resume = EventWaitHandle.OpenExisting(args[6] + "-resume");
            int calls = 0;
            void Pause() {
                if (Interlocked.Increment(ref calls) != 1) return;
                paused.Set();
                if (!resume.WaitOne(TimeSpan.FromSeconds(600)))
                    throw new Exception("child validation was not released");
            }
            if (gate == "copy")
                typeof(TmdlValidate).GetField("AfterCopyFileForTests", BindingFlags.Static | BindingFlags.NonPublic)!
                    .SetValue(null, (Action<string>)(_ => Pause()));
            else if (gate == "inventory")
                typeof(TmdlValidate).GetField("BeforeInventoryForTests", BindingFlags.Static | BindingFlags.NonPublic)!
                    .SetValue(null, (Action)Pause);
            else if (gate != "none") throw new Exception("unknown child gate: " + gate);
            var ctx = new Ctx("tmdl-validate");
            var request = Req.Parse(new JsonObject { ["private_dir"] = privateDir,
                ["folder"] = folder, ["timeout_s"] = timeoutSec }.ToJsonString());
            ctx.Init(request);
            var result = TmdlValidate.Run(request, ctx);
            File.WriteAllText(resultFile, new JsonObject { ["valid"] = result["valid"]!.GetValue<bool>() }.ToJsonString());
            return 0;
        }
        catch (Exception ex)
        {
            File.WriteAllText(resultFile, new JsonObject { ["error"] = ex.GetType().Name + ": " + ex.Message }.ToJsonString());
            return 2;
        }
    }

    static int Main(string[] args)
    {
        if (args.Length > 1 && args[1] == "child-validate")
            Environment.Exit(ChildValidate(args));
        var ctx = new Ctx("synth-query");
        ctx.Init(Req.Parse(new JsonObject { ["private_dir"] = args[0] }.ToJsonString()));
        var marker = new string('a', 64);
        var definition = P.Definition(Build());
        P.Save(ctx, marker, definition, "catalog-A");
        var good = new P.Snapshot(marker, "catalog-A", definition, "v1");
        int passed = 0;
        int failed = 0;
        void Check(string name, Action action, string? expectedReason)
        {
            Exception? caught = null;
            try { action(); }
            catch (Exception ex) { caught = ex; }
            string? failure = null;
            if (expectedReason == null)
            {
                if (caught != null)
                    failure = "unexpected " + caught.GetType().Name + ": " + caught.Message;
            }
            else if (caught == null)
            {
                failure = "accepted, expected refusal " + expectedReason;
            }
            else if (caught is not BadRequestException)
            {
                failure = "unexpected " + caught.GetType().Name + ", expected refusal " + expectedReason + ": " + caught.Message;
            }
            else if (!caught.Message.StartsWith(expectedReason + ":", StringComparison.Ordinal))
            {
                failure = "unexpected refusal reason: " + caught.Message;
            }
            if (failure == null)
            {
                passed++;
                Console.WriteLine("PASS " + name);
            }
            else
            {
                failed++;
                Console.WriteLine("FAIL " + name + ": " + failure);
            }
        }
        Check("passing manifest", () => P.Verify(ctx, good), null);
        // Counts-less definitions cover both offline and engine-maintained RowNumber shapes.
        var withoutRowNumber = Build();
        withoutRowNumber.Tables["T"].Columns.Remove("RowNumber");
        var withoutRowNumberDefinition = P.Definition(withoutRowNumber);
        Check("single RowNumber absent on live side", () => P.Verify(ctx,
            good with { DefinitionValue = withoutRowNumberDefinition }), null);
        var builtMarker = new string('c', 64);
        P.Save(ctx, builtMarker, withoutRowNumberDefinition);
        var builtLive = good with { MarkerValue = builtMarker };
        Check("single RowNumber present only on live side", () => P.Verify(ctx,
            builtLive, requireCatalog: false), null);
        Check("manifest without counts binds", () => {
            // The same verify/save sequence used by Bind, without requiring a live Desktop.
            P.Verify(ctx, builtLive, requireCatalog: false);
            P.Save(ctx, builtMarker, builtLive.DefinitionValue, builtLive.Catalog);
            P.Verify(ctx, builtLive);
        }, null);
        void Mutation(string name, Action<Model> mutate, string expectedReason = "SYNTH_DEFINITION_CHANGED")
        {
            var model = Build();
            var baseline = good with { DefinitionValue = P.Definition(model) };
            Check(name + " baseline", () => P.Verify(ctx, baseline), null);
            Check(name, () => {
                mutate(model);
                P.Buffered<int>(
                    () => baseline with { DefinitionValue = P.Definition(model) }, s => P.Verify(ctx, s),
                    () => throw new Exception("query ran on changed model"));
            }, expectedReason);
        }
        Mutation("changed measure", m => m.Tables["T"].Measures["M"].Expression = "42");
        Mutation("changed calculated table", m => ((CalculatedPartitionSource)m.Tables["T"].Partitions[0].Source).Expression = "ROW(\"X\", 1)");
        Mutation("edited DATATABLE row", m => ((CalculatedPartitionSource)m.Tables["T"].Partitions[0].Source).Expression = "DATATABLE(\"X\", INTEGER, {{123456}})");
        Mutation("added column", m => m.Tables["T"].Columns.Add(new CalculatedColumn { Name = "Added", Expression = "1", DataType = DataType.Int64 }));
        Mutation("second RowNumber column", m => m.Tables["T"].Columns.Add(MakeRowNumber("SecondRowNumber")));
        Table Extra(string name, bool data = false) {
            var t = new Table { Name = name };
            t.Partitions.Add(new Partition { Name = "P", Source = data ? new MPartitionSource { Expression = "secret" } : new CalculatedPartitionSource { Expression = "ROW(\"X\", 1)" } });
            return t;
        }
        Mutation("added untagged table", m => m.Tables.Add(Extra("U")));
        Mutation("private-prefixed table", m => m.Tables.Add(Extra("LocalDateTable_secret")));
        Mutation("private table with data partition", m => m.Tables["Private"].Partitions[0].Source = new MPartitionSource { Expression = "secret" }, "SYNTH_DATA_PARTITION");
        // Verify private and untagged tables present at signing remain covered, not just additions.
        Check("signed private table definition preserved", () => {
            var privateModel = Build(); var hidden = Extra("LocalDateTable_signed"); hidden.IsPrivate = true;
            privateModel.Tables.Add(hidden);
            var privateDefinition = P.Definition(privateModel);
            var signedPrivateTable = JsonNode.Parse(privateDefinition)!["tables"]!.AsArray()
                .SingleOrDefault(t => t?["name"]?.GetValue<string>() == "LocalDateTable_signed");
            if (signedPrivateTable == null) throw new Exception("private table omitted");
            if (signedPrivateTable["partitions"]![0]!["source"]!["expression"]!.GetValue<string>()
                != ((CalculatedPartitionSource)hidden.Partitions[0].Source).Expression)
                throw new Exception("private table partition definition omitted");
        }, null);
        Mutation("removed table", m => m.Tables.Remove("Other"));
        Mutation("calculated column expression", m => ((CalculatedColumn)m.Tables["T"].Columns["C"]).Expression = "42");
        Mutation("calculation item expression", m => m.Tables["CG"].CalculationGroup.CalculationItems["Item"].Expression = "42");
        Mutation("private table expression", m => ((CalculatedPartitionSource)m.Tables["Private"].Partitions[0].Source).Expression = "ROW(\"X\", 42)");
        Mutation("private-prefixed table expression", m => ((CalculatedPartitionSource)m.Tables["LocalDateTable_existing"].Partitions[0].Source).Expression = "ROW(\"X\", 42)");
        Mutation("format string", m => m.Tables["T"].Measures["M"].FormatString = "secret");
        Check("marker replay different catalog", () => P.Verify(ctx, good with { Catalog = "catalog-B" }), "SYNTH_CATALOG_MISMATCH");
        Check("missing marker", () => P.Verify(ctx, good with { MarkerValue = "" }), "SYNTH_NO_MARKER");
        var manifestPath = Path.Combine(args[0], "synth-manifest-" + marker + ".bin");
        var signed = JsonNode.Parse(System.Text.Encoding.UTF8.GetString(Dpapi.Unprotect(File.ReadAllBytes(manifestPath))))!.AsObject();
        signed["definition"] = "tampered";
        Vault.WriteProtected(manifestPath, signed.ToJsonString());
        Check("invalid manifest signature", () => P.Verify(ctx, good), "SYNTH_MANIFEST_SIGNATURE");
        P.Save(ctx, marker, definition, "catalog-A");
        int queries = 0;
        Check("verification unavailable", () => {
            queries = 0;
            try
            {
                P.Buffered(
                    () => good with { MarkerValue = new string('b', 64) }, s => P.Verify(ctx, s), () => ++queries);
            }
            finally
            {
                if (queries != 0) throw new Exception("query ran without verification");
            }
        }, "SYNTH_VERIFICATION_UNAVAILABLE");
        Check("live verification unavailable before query", () => {
            queries = 0;
            try
            {
                P.Buffered<int>(
                    () => throw new BadRequestException("SYNTH_VERIFICATION_UNAVAILABLE: live definition unavailable"),
                    s => P.Verify(ctx, s), () => ++queries);
            }
            finally
            {
                if (queries != 0) throw new Exception("query ran without live verification");
            }
        }, "SYNTH_VERIFICATION_UNAVAILABLE");
        int captures = 0;
        Check("edit between verification and execution", () => {
            queries = 0;
            captures = 0;
            try
            {
                P.Buffered(
                    () => ++captures == 1 ? good : good with { DefinitionValue = "edited" },
                    s => P.Verify(ctx, s), () => ++queries);
            }
            finally
            {
                if (queries != 1 || captures != 2) throw new Exception("race fixture did not execute both checks");
            }
        }, "SYNTH_DEFINITION_CHANGED");
        captures = 0;
        Check("version changed while definition restored", () => P.Buffered(
            () => ++captures == 1 ? good : good with { Version = "v2" },
            s => P.Verify(ctx, s), () => ++queries), "SYNTH_MODEL_CHANGED_DURING_QUERY");
        captures = 0;
        Check("live verification unavailable after query", () => P.Buffered(
            () => ++captures == 1 ? good : throw new BadRequestException("SYNTH_VERIFICATION_UNAVAILABLE: live definition unavailable"),
            s => P.Verify(ctx, s), () => ++queries), "SYNTH_VERIFICATION_UNAVAILABLE");
        Check("verified rows released", () => {
            if (P.Buffered(() => good, s => P.Verify(ctx, s), () => 7) != 7) throw new Exception("lost rows");
        }, null);
        var isInsideFolder = typeof(TmdlValidate).GetMethod("IsInsideFolder", BindingFlags.Static | BindingFlags.NonPublic)!
            .CreateDelegate<Func<string, string, bool>>();
        Check("drive root contains same-drive descendant", () => {
            if (!isInsideFolder(@"C:\private\tmdl-validate-fixture", @"C:\")) throw new Exception("root descendant missed");
        }, null);
        Check("ordinary folder contains descendant", () => {
            if (!isInsideFolder(@"C:\models\foo\tables", @"C:\models\foo")) throw new Exception("descendant missed");
        }, null);
        Check("folder prefix sibling is outside", () => {
            if (isInsideFolder(@"C:\models\foo2", @"C:\models\foo")) throw new Exception("sibling included");
        }, null);
        Check("folder itself is outside", () => {
            if (isInsideFolder(@"C:\models\foo", @"C:\models\foo") || isInsideFolder(@"C:\", @"C:\"))
                throw new Exception("folder itself included");
        }, null);
        var beforeInventory = typeof(TmdlValidate).GetField("BeforeInventoryForTests", BindingFlags.Static | BindingFlags.NonPublic)!;
        Check("validation copies only TMDL and cleans read-only snapshot", () => {
            var source = Path.Combine(args[0], "readonly-source");
            Microsoft.AnalysisServices.Tabular.TmdlSerializer.SerializeDatabaseToFolder((Microsoft.AnalysisServices.Tabular.Database)Build().Database, source);
            var readOnlyFile = Path.Combine(source, "tables", "T.tmdl");
            var originalAttributes = File.GetAttributes(readOnlyFile);
            File.SetAttributes(readOnlyFile, originalAttributes | FileAttributes.ReadOnly);
            File.WriteAllText(Path.Combine(source, "cache.abf"), "must not be copied");
            var snapshotPrivate = Path.Combine(args[0], "readonly-private");
            var snapshotCtx = new Ctx("tmdl-validate");
            var request = Req.Parse(new JsonObject { ["private_dir"] = snapshotPrivate,
                ["folder"] = source, ["timeout_s"] = 120 }.ToJsonString());
            snapshotCtx.Init(request);
            using var paused = new ManualResetEventSlim();
            using var resume = new ManualResetEventSlim();
            Task<JsonObject>? validation = null;
            var deadline = DateTime.UtcNow.AddSeconds(120);
            beforeInventory.SetValue(null, (Action)(() => {
                paused.Set();
                if (!resume.Wait(TimeSpan.FromSeconds(120))) throw new Exception("snapshot fixture was not released");
            }));
            try
            {
                validation = Task.Run(() => TmdlValidate.Run(request, snapshotCtx));
                if (!paused.Wait(TimeSpan.FromTicks(Math.Max(0, (deadline - DateTime.UtcNow).Ticks))))
                    throw new Exception("no snapshot observed before inventory deadline");
                var snapshot = Directory.GetDirectories(snapshotPrivate, "tmdl-validate-*").Single();
                var files = Directory.GetFiles(snapshot, "*", SearchOption.AllDirectories);
                if (files.Any(f => !string.Equals(Path.GetExtension(f), ".tmdl", StringComparison.OrdinalIgnoreCase)))
                    throw new Exception("non-TMDL sentinel copied into snapshot");
                var expectedFiles = Directory.GetFiles(source, "*", SearchOption.AllDirectories)
                    .Where(f => string.Equals(Path.GetExtension(f), ".tmdl", StringComparison.OrdinalIgnoreCase))
                    .Select(f => Path.GetRelativePath(source, f)).OrderBy(f => f).ToArray();
                if (!expectedFiles.SequenceEqual(files.Select(f => Path.GetRelativePath(snapshot, f)).OrderBy(f => f)))
                    throw new Exception("snapshot TMDL files differ from source");
                if (!File.GetAttributes(Path.Combine(snapshot, "tables", "T.tmdl")).HasFlag(FileAttributes.ReadOnly))
                    throw new Exception("fixture did not exercise read-only snapshot cleanup");
                resume.Set();
                var result = validation.GetAwaiter().GetResult();
                if (result["valid"]?.GetValue<bool>() != true) throw new Exception("read-only snapshot validation failed");
                if (Directory.GetDirectories(snapshotPrivate, "tmdl-validate-*").Length != 0)
                    throw new Exception("read-only validation snapshot was not removed");
            }
            finally
            {
                resume.Set();
                try { if (validation != null) validation.GetAwaiter().GetResult(); }
                finally
                {
                    beforeInventory.SetValue(null, null);
                    File.SetAttributes(readOnlyFile, originalAttributes);
                }
            }
        }, null);
        Check("validation inventories and signs its snapshot", () => {
            var candidate = Build();
            var raceMarker = new string('d', 64);
            candidate.Annotations.Add(new Annotation { Name = "PBZ_SYNTHETIC", Value = raceMarker });
            var source = Path.Combine(args[0], "race-source");
            Microsoft.AnalysisServices.Tabular.TmdlSerializer.SerializeDatabaseToFolder((Microsoft.AnalysisServices.Tabular.Database)candidate.Database, source);
            var expected = P.Definition(Microsoft.AnalysisServices.Tabular.TmdlSerializer
                .DeserializeDatabaseFromFolder(source).Model);
            var racePrivate = Path.Combine(args[0], "race-private");
            var raceCtx = new Ctx("tmdl-validate");
            var request = Req.Parse(new JsonObject { ["private_dir"] = racePrivate,
                ["folder"] = source, ["synthetic_marker"] = raceMarker, ["timeout_s"] = 120 }.ToJsonString());
            raceCtx.Init(request);
            using var paused = new ManualResetEventSlim();
            using var resume = new ManualResetEventSlim();
            Task<JsonObject>? validation = null;
            var deadline = DateTime.UtcNow.AddSeconds(120);
            beforeInventory.SetValue(null, (Action)(() => {
                paused.Set();
                if (!resume.Wait(TimeSpan.FromSeconds(120))) throw new Exception("race fixture was not released");
            }));
            try
            {
                validation = Task.Run(() => TmdlValidate.Run(request, raceCtx));
                if (!paused.Wait(TimeSpan.FromTicks(Math.Max(0, (deadline - DateTime.UtcNow).Ticks))))
                    throw new Exception("no snapshot observed before inventory deadline");
                File.Delete(Path.Combine(source, "tables", "T.tmdl"));
                resume.Set();
                var result = validation.GetAwaiter().GetResult();
                if (result["valid"]?.GetValue<bool>() != true || result["missing_refs"]!.AsArray().Count != 0
                    || result["orphan_files"]!.AsArray().Count != 0)
                    throw new Exception("source mutation changed snapshot validation: " + result.ToJsonString());
                if (P.Load(raceCtx, raceMarker)["definition"]!.GetValue<string>() != expected)
                    throw new Exception("validation did not sign the complete original model");
                if (Directory.EnumerateDirectories(racePrivate, "tmdl-validate-*").Any())
                    throw new Exception("validation snapshot was not cleaned up");
            }
            finally
            {
                resume.Set();
                try { if (validation != null) validation.GetAwaiter().GetResult(); }
                finally { beforeInventory.SetValue(null, null); }
            }
        }, null);
        Check("copy timeout retains ownership until worker finishes and recovers stale snapshots", () => {
            var source = Path.Combine(args[0], "timeout-source");
            Microsoft.AnalysisServices.Tabular.TmdlSerializer.SerializeDatabaseToFolder((Microsoft.AnalysisServices.Tabular.Database)Build().Database, source);
            var scratch = Path.Combine(args[0], "timeout-private");
            var timeoutCtx = new Ctx("tmdl-validate");
            var request = Req.Parse(new JsonObject { ["private_dir"] = scratch,
                ["folder"] = source, ["timeout_s"] = 1 }.ToJsonString());
            timeoutCtx.Init(request);
            var afterCopy = typeof(TmdlValidate).GetField("AfterCopyFileForTests", BindingFlags.Static | BindingFlags.NonPublic)!;
            var afterCleanup = typeof(TmdlValidate).GetField("AfterSnapshotCleanupForTests", BindingFlags.Static | BindingFlags.NonPublic)!;
            using var paused = new ManualResetEventSlim();
            using var resume = new ManualResetEventSlim();
            using var cleaned = new ManualResetEventSlim();
            using var returned = new ManualResetEventSlim();
            Task<JsonObject>? validation = null;
            string? copiedFile = null;
            string? snapshot = null;
            int copies = 0;
            try
            {
                afterCopy.SetValue(null, (Action<string>)(target => {
                    if (Interlocked.Increment(ref copies) != 1) return;
                    copiedFile = target;
                    paused.Set();
                    if (!resume.Wait(TimeSpan.FromSeconds(120))) throw new Exception("copy timeout fixture was not released");
                }));
                afterCleanup.SetValue(null, (Action<string>)(path => {
                    if (path == snapshot) cleaned.Set();
                }));
                validation = Task.Run(() => {
                    try { return TmdlValidate.Run(request, timeoutCtx); }
                    finally { returned.Set(); }
                });
                if (!paused.Wait(TimeSpan.FromSeconds(120))) throw new Exception("copier did not produce a partial snapshot");
                snapshot = Directory.GetDirectories(scratch, "tmdl-validate-*").Single();
                if (copiedFile == null || !File.Exists(copiedFile)) throw new Exception("copier paused before copying a file");
                if (!returned.Wait(TimeSpan.FromSeconds(120)))
                    throw new Exception("blocked copier prevented the deadline from surfacing");
                try
                {
                    validation.GetAwaiter().GetResult();
                    throw new Exception("blocked copy did not time out");
                }
                catch (TimeoutException) { }
                // Pins invariants 1 and 3: the deadline must return while the copier is
                // blocked, without traversing/deleting its partial output (the old race).
                if (!Directory.Exists(snapshot) || !File.Exists(copiedFile) || cleaned.IsSet)
                    throw new Exception("timeout cleanup touched a snapshot with a live copier");
                var stale = Path.Combine(scratch, "tmdl-validate-stale-fixture");
                Directory.CreateDirectory(stale);
                File.WriteAllText(Path.Combine(stale, "partial.tmdl"), "stale partial copy");
                // An abandoned engine snapshot has an ownership marker but no held lease.
                File.WriteAllText(stale + ".pbz-owner", "");
                var unowned = Path.Combine(scratch, "tmdl-validate-unowned-fixture");
                Directory.CreateDirectory(unowned);
                var unownedFile = Path.Combine(unowned, "partial.tmdl");
                var unownedBytes = new byte[] { 0, 1, 127, 255 };
                File.WriteAllBytes(unownedFile, unownedBytes);
                var recoveryCtx = new Ctx("tmdl-validate");
                var recoveryRequest = Req.Parse(new JsonObject { ["private_dir"] = scratch,
                    ["folder"] = source, ["timeout_s"] = 120 }.ToJsonString());
                recoveryCtx.Init(recoveryRequest);
                var recovered = TmdlValidate.Run(recoveryRequest, recoveryCtx);
                if (recovered["valid"]?.GetValue<bool>() != true || Directory.Exists(stale)
                    || File.Exists(stale + ".pbz-owner"))
                    throw new Exception("subsequent validation did not recover the stale snapshot");
                // Ownership invariant 1: a matching name alone never authorizes deletion.
                if (!Directory.Exists(unowned) || !File.ReadAllBytes(unownedFile).SequenceEqual(unownedBytes))
                    throw new Exception("stale sweep changed unowned matching content");
                if (!Directory.Exists(snapshot) || !File.Exists(copiedFile) || cleaned.IsSet)
                    throw new Exception("stale sweep touched a snapshot with a live copier");
                resume.Set();
                // Pins invariant 2: this signal follows actual worker completion and cleanup,
                // so the released worker can never recreate entries after the final assertion.
                if (!cleaned.Wait(TimeSpan.FromSeconds(120))) throw new Exception("released copy snapshot was not cleaned up");
                if (!Directory.EnumerateDirectories(scratch, "tmdl-validate-*").OrderBy(p => p)
                        .SequenceEqual(new[] { unowned })
                    || Directory.EnumerateFiles(scratch, "tmdl-validate-*.pbz-owner").Any())
                    throw new Exception("copy timeout left unaccounted validation snapshots");
            }
            finally
            {
                resume.Set();
                try
                {
                    if (validation != null)
                    {
                        try { validation.GetAwaiter().GetResult(); }
                        catch (TimeoutException) { }
                        if (snapshot != null && !cleaned.Wait(TimeSpan.FromSeconds(120)))
                            throw new Exception("copy cleanup did not finish before resetting hooks");
                    }
                }
                finally
                {
                    afterCopy.SetValue(null, null);
                    afterCleanup.SetValue(null, null);
                }
            }
        }, null);
        Check("validation preserves cross-process leases and matching candidate folders", () => {
            var scratch = Path.Combine(args[0], "cross-process-private");
            Directory.CreateDirectory(scratch);
            var live = Path.Combine(scratch, "tmdl-validate-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(live);
            var partial = Path.Combine(live, "partial.tmdl");
            var partialBytes = new byte[] { 112, 97, 114, 116, 105, 97, 108, 0, 255 };
            File.WriteAllBytes(partial, partialBytes);
            var liveMarker = live + ".pbz-owner";
            File.WriteAllText(liveMarker, "");
            using var lease = new FileStream(liveMarker, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
            var source = Path.Combine(scratch, "tmdl-validate-source");
            Microsoft.AnalysisServices.Tabular.TmdlSerializer.SerializeDatabaseToFolder((Microsoft.AnalysisServices.Tabular.Database)Build().Database, source);
            var sourceBytes = Directory.GetFiles(source, "*", SearchOption.AllDirectories)
                .ToDictionary(f => Path.GetRelativePath(source, f), File.ReadAllBytes);
            // Even an unleased marker must not override candidate protection (invariant 4).
            File.WriteAllText(source + ".pbz-owner", "");
            JsonObject Validate() {
                var request = Req.Parse(new JsonObject { ["private_dir"] = scratch,
                    ["folder"] = source, ["timeout_s"] = 120 }.ToJsonString());
                var validationCtx = new Ctx("tmdl-validate");
                validationCtx.Init(request);
                return TmdlValidate.Run(request, validationCtx);
            }
            using var paused = new ManualResetEventSlim();
            using var resume = new ManualResetEventSlim();
            Task<JsonObject>? validation = null;
            beforeInventory.SetValue(null, (Action)(() => {
                paused.Set();
                if (!resume.Wait(TimeSpan.FromSeconds(120))) throw new Exception("lease fixture was not released");
            }));
            try
            {
                validation = Task.Run(Validate);
                if (!paused.Wait(TimeSpan.FromSeconds(120))) throw new Exception("lease fixture did not reach inventory");
                resume.Set();
                var result = validation.GetAwaiter().GetResult();
                // Invariants 1 and 3: the fixture-held exclusive lease is indistinguishable
                // from another process's live owner; its partial tree must survive verbatim.
                if (!Directory.Exists(live) || !File.ReadAllBytes(partial).SequenceEqual(partialBytes)
                    || !File.Exists(liveMarker))
                    throw new Exception("sweep changed another process's live snapshot");
                // Invariant 4: a candidate matching the sweep pattern must survive verbatim.
                if (!Directory.Exists(source)
                    || !Directory.GetFiles(source, "*", SearchOption.AllDirectories)
                        .Select(f => Path.GetRelativePath(source, f)).OrderBy(f => f)
                        .SequenceEqual(sourceBytes.Keys.OrderBy(f => f))
                    || sourceBytes.Any(kv => !File.ReadAllBytes(Path.Combine(source, kv.Key)).SequenceEqual(kv.Value)))
                    throw new Exception("sweep changed its matching candidate folder");
                // Candidate protection must allow the validation itself to finish successfully.
                if (result["valid"]?.GetValue<bool>() != true) throw new Exception("protected candidate validation failed");
            }
            finally
            {
                resume.Set();
                try { if (validation != null) validation.GetAwaiter().GetResult(); }
                finally { beforeInventory.SetValue(null, null); }
            }
            lease.Dispose(); // Process A has now exited; its ownership marker is abandoned.
            var recovered = Validate();
            // Invariants 1, 3 and 5: only after the lease ends may recovery delete tree and marker.
            if (Directory.Exists(live) || File.Exists(liveMarker))
                throw new Exception("abandoned cross-process snapshot was not recovered");
            if (recovered["valid"]?.GetValue<bool>() != true) throw new Exception("recovery validation failed");
        }, null);
        Check("validation owned by a separate process survives a peer sweep and is recovered after the owner dies", () => {
            var privateDir = Path.Combine(args[0], "two-process-private");
            var source = Path.Combine(args[0], "two-process-source");
            Directory.CreateDirectory(privateDir);
            Microsoft.AnalysisServices.Tabular.TmdlSerializer.SerializeDatabaseToFolder((Microsoft.AnalysisServices.Tabular.Database)Build().Database, source);
            System.Collections.Generic.Dictionary<string, byte[]> SnapshotBytes(string dir) =>
                Directory.GetFiles(dir, "*", SearchOption.AllDirectories)
                    .ToDictionary(f => Path.GetRelativePath(dir, f), File.ReadAllBytes);
            string Quote(string value) {
                // Windows command-line quoting, including quotes and trailing backslashes.
                var quoted = new System.Text.StringBuilder("\"");
                int slashes = 0;
                foreach (var ch in value)
                {
                    if (ch == '\\') { slashes++; continue; }
                    quoted.Append('\\', ch == '"' ? slashes * 2 + 1 : slashes);
                    quoted.Append(ch);
                    slashes = 0;
                }
                return quoted.Append('\\', slashes * 2).Append('"').ToString();
            }
            Process StartChild(string gate, int timeoutSec, string resultFile,
                out EventWaitHandle paused, out EventWaitHandle resume) {
                var prefix = "pbz-tmdl-" + Guid.NewGuid().ToString("N");
                paused = new EventWaitHandle(false, EventResetMode.ManualReset, prefix + "-paused");
                // The caller's finally owns both handles, including launch failures.
                resume = new EventWaitHandle(false, EventResetMode.ManualReset, prefix + "-resume");
                var childArgs = string.Join(" ", new[] { args[0], "child-validate", privateDir, source,
                    timeoutSec.ToString(System.Globalization.CultureInfo.InvariantCulture), gate, prefix, resultFile }.Select(Quote));
                var executable = Environment.ProcessPath;
                var useDotnet = executable == null || Path.GetFileName(executable).StartsWith("dotnet", StringComparison.OrdinalIgnoreCase);
                var info = new ProcessStartInfo {
                    FileName = useDotnet ? "dotnet" : executable!,
                    Arguments = (useDotnet ? "exec " + Quote(typeof(Fixtures).Assembly.Location) + " " : "") + childArgs,
                    UseShellExecute = false
                };
                return Process.Start(info) ?? throw new Exception("could not start child validation");
            }
            JsonObject ReadChildResult(string resultFile) => JsonNode.Parse(File.ReadAllText(resultFile))!.AsObject();
            void FinishChild(Process process, string resultFile) {
                if (!process.WaitForExit(300000)) throw new Exception("child validation did not exit before deadline");
                var result = ReadChildResult(resultFile);
                if (process.ExitCode != 0 || result.ContainsKey("error") || result["valid"]?.GetValue<bool>() != true)
                    throw new Exception("child validation failed: " + result.ToJsonString());
            }
            void DisposeChild(Process? process, EventWaitHandle? paused, EventWaitHandle? resume) {
                try
                {
                    resume?.Set();
                    if (process != null && !process.WaitForExit(300000))
                    {
                        process.Kill();
                        if (!process.WaitForExit(300000)) throw new Exception("child did not exit after termination");
                        throw new Exception("child required termination after cleanup deadline");
                    }
                }
                finally { process?.Dispose(); paused?.Dispose(); resume?.Dispose(); }
            }
            void AssertNoSnapshots() {
                if (Directory.GetDirectories(privateDir, "tmdl-validate-*").Any()
                    || Directory.GetFiles(privateDir, "tmdl-validate-*.pbz-owner").Any())
                    throw new Exception("child validation left snapshots or ownership markers");
            }
            void LiveOwnerPhase(string gate) {
                Process? owner = null, peer = null;
                EventWaitHandle? pausedA = null, resumeA = null, pausedB = null, resumeB = null;
                var ownerResult = Path.Combine(args[0], "two-process-" + gate + "-owner.json");
                var peerResult = Path.Combine(args[0], "two-process-" + gate + "-peer.json");
                try
                {
                    owner = StartChild(gate, 300, ownerResult, out pausedA, out resumeA);
                    if (!pausedA.WaitOne(TimeSpan.FromSeconds(180))) throw new Exception("owner did not pause at " + gate);
                    var snapshot = Directory.GetDirectories(privateDir, "tmdl-validate-*").Single();
                    if (!File.Exists(snapshot + ".pbz-owner")) throw new Exception("owner did not publish a lease");
                    var before = SnapshotBytes(snapshot);
                    if (before.Count == 0) throw new Exception("owner paused without copied files");
                    peer = StartChild("none", 120, peerResult, out pausedB, out resumeB);
                    FinishChild(peer, peerResult);
                    if (!Directory.Exists(snapshot) || !File.Exists(snapshot + ".pbz-owner"))
                        throw new Exception("peer sweep removed live owner snapshot or marker at " + gate);
                    var after = SnapshotBytes(snapshot);
                    if (!after.Keys.OrderBy(f => f).SequenceEqual(before.Keys.OrderBy(f => f))
                        || before.Any(kv => !after[kv.Key].SequenceEqual(kv.Value)))
                        throw new Exception("peer sweep changed live snapshot bytes at " + gate);
                    resumeA.Set();
                    FinishChild(owner, ownerResult);
                    if (Directory.Exists(snapshot) || File.Exists(snapshot + ".pbz-owner"))
                        throw new Exception("owner did not clean up its snapshot and marker");
                    AssertNoSnapshots();
                }
                finally
                {
                    try { DisposeChild(owner, pausedA, resumeA); }
                    finally { DisposeChild(peer, pausedB, resumeB); }
                }
            }
            // Phase 1 pins Run publishing and holding its lease during copying, and
            // prevents a peer sweep from deleting a live, partially copied snapshot.
            LiveOwnerPhase("copy");
            // Phase 2 pins lease ownership after copying finishes, through validation
            // and cleanup: releasing the lease before inventory lets the peer delete it.
            LiveOwnerPhase("inventory");
            // Phase 3 pins real Run lease publication and abandoned marked recovery.
            // Without a published lease, a killed owner's unmarked tree must be kept.
            Process? abandoned = null, recovery = null;
            EventWaitHandle? pausedC = null, resumeC = null, pausedD = null, resumeD = null;
            var abandonedResult = Path.Combine(args[0], "two-process-abandoned.json");
            var recoveryResult = Path.Combine(args[0], "two-process-recovery.json");
            try
            {
                abandoned = StartChild("copy", 300, abandonedResult, out pausedC, out resumeC);
                if (!pausedC.WaitOne(TimeSpan.FromSeconds(180))) throw new Exception("abandoned owner did not pause during copy");
                var snapshot = Directory.GetDirectories(privateDir, "tmdl-validate-*").Single();
                if (!File.Exists(snapshot + ".pbz-owner")) throw new Exception("abandoned owner did not publish a lease");
                var before = SnapshotBytes(snapshot);
                if (before.Count == 0) throw new Exception("abandoned owner did not copy any files");
                abandoned.Kill();
                if (!abandoned.WaitForExit(300000)) throw new Exception("killed owner did not exit before deadline");
                if (!Directory.Exists(snapshot) || !File.Exists(snapshot + ".pbz-owner"))
                    throw new Exception("killed owner did not leave its snapshot and marker");
                recovery = StartChild("none", 120, recoveryResult, out pausedD, out resumeD);
                FinishChild(recovery, recoveryResult);
                if (Directory.Exists(snapshot) || File.Exists(snapshot + ".pbz-owner"))
                    throw new Exception("peer sweep did not recover the killed owner's snapshot and marker");
                AssertNoSnapshots();
            }
            finally
            {
                try { DisposeChild(abandoned, pausedC, resumeC); }
                finally { DisposeChild(recovery, pausedD, resumeD); }
            }
        }, null);
        Console.WriteLine($"{passed} passed, {failed} failed");
        return failed == 0 ? 0 : 1;
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
