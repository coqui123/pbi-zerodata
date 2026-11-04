using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using TOM = Microsoft.AnalysisServices.Tabular;

namespace PbzEngine;

/// <summary>
/// refresh (v1.1): the engine never refreshes data partitions. Power BI Desktop refuses external refreshes of M
/// partitions (its Power Query session is not available to external clients and it raises a modal error), so data
/// refresh is the user's job via Desktop's Refresh button.
///  - no "tables": TMSL refresh type "calculate" on the database (recalc of calculated tables/columns, only where needed);
///  - "tables": every listed table must consist of calculated (DAX) or calculation-group partitions only; they get a
///    forced recalculation (TMSL "full" on calculated partitions, which is DAX-only). If any listed table has an M or
///    other data-source partition the response is status UNSUPPORTED and nothing is attempted.
/// Engine messages are logged only.
/// </summary>
public static class Refresh
{
    const int SourceCalculated = 2, SourceCalculationGroup = 7;

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        var tables = req.StrList("tables");
        string dbName;
        var names = new List<string>();
        using (var conn = Conn.Open(ctx, port))
        {
            dbName = conn.Meta.DatabaseName;
            if (tables != null)
            {
                if (tables.Count == 0) throw new BadRequestException("tables must not be empty");
                foreach (var t in tables)
                {
                    var tm = conn.Meta.FindTable(t) ?? throw new EngineException(ErrorClass.UNKNOWN_OBJECT, "refresh table not found");
                    if (tm.Partitions.Count == 0 || tm.Partitions.Any(p => p.SourceType is not (SourceCalculated or SourceCalculationGroup)))
                        return Ctx.Ok("refresh", new JsonObject { ["status"] = "UNSUPPORTED" });
                    if (!names.Contains(tm.Name)) names.Add(tm.Name);
                }
            }
        }

        string tmsl = BuildTmsl(dbName, names);
        var server = new TOM.Server();
        try
        {
            ctx.Guard(() =>
            {
                server.Connect("Data Source=localhost:" + port.ToString(CultureInfo.InvariantCulture) + ";");
                return 0;
            }, Math.Min(ctx.TimeoutSec, 60));
            var results = ctx.Guard(() => server.Execute(tmsl));
            if (results != null && results.ContainsErrors)
            {
                var sb = new StringBuilder("refresh returned errors:");
                foreach (Microsoft.AnalysisServices.XmlaResult r in results)
                    foreach (Microsoft.AnalysisServices.XmlaMessage msg in r.Messages)
                        sb.Append('\n').Append(msg.Description);
                string text = sb.ToString();
                var o = new JsonObject { ["ok"] = false, ["verb"] = "refresh", ["class"] = Errors.ClassifyMessage(text).ToString() };
                var id = ctx.LogText(text);
                if (id != null) o["log_id"] = id;
                return o;
            }
        }
        finally
        {
            Bounded.Run(() =>
            {
                if (server.Connected) server.Disconnect();
                server.Dispose();
            });
        }
        return Ctx.Ok("refresh", new JsonObject { ["status"] = "PASS" });
    }

    static string BuildTmsl(string db, List<string> tables)
    {
        var objs = new JsonArray();
        if (tables.Count == 0) objs.Add(new JsonObject { ["database"] = db });
        else foreach (var t in tables) objs.Add(new JsonObject { ["database"] = db, ["table"] = t });
        return new JsonObject
        {
            ["refresh"] = new JsonObject { ["type"] = tables.Count == 0 ? "calculate" : "full", ["objects"] = objs },
        }.ToJsonString();
    }
}

/// <summary>Cleanup that can never hang the response (a stuck session must not block the JSON on stdout).</summary>
public static class Bounded
{
    public static void Run(Action action, int milliseconds = 3000)
    {
        try
        {
            var t = Task.Run(() => { try { action(); } catch { } });
            t.Wait(milliseconds);
        }
        catch
        {
        }
    }
}
