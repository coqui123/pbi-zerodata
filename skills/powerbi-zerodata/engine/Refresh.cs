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
        var allowed = new HashSet<string>(StringComparer.Ordinal);
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
            Allow(allowed, dbName);
            if (names.Count == 0)
            {
                foreach (var t in conn.Meta.UserTables)
                {
                    Allow(allowed, t.Name);
                    foreach (var p in t.Partitions) Allow(allowed, p.Name);
                }
            }
            else
            {
                foreach (var n in names)
                {
                    Allow(allowed, n);
                    var tm = conn.Meta.FindTable(n);
                    if (tm == null) continue;
                    foreach (var p in tm.Partitions) Allow(allowed, p.Name);
                }
            }
        }

        string tmsl = BuildTmsl(dbName, names);
        var server = new TOM.Server();
        EngineTrace? trace = null;
        try
        {
            ctx.Guard(() =>
            {
                server.Connect("Data Source=localhost:" + port.ToString(CultureInfo.InvariantCulture) + ";");
                return 0;
            }, Math.Min(ctx.TimeoutSec, 60));
            // A trace that cannot start must not fail the refresh. M partitions already returned UNSUPPORTED.
            trace = EngineTrace.TryStart(server, new[] { "ProgressReportEnd" }, ctx);
            trace?.Arm();
            var results = ctx.Guard(() => server.Execute(tmsl));
            List<EngineTrace.Hit> hits = new();
            if (trace != null && trace.Started)
            {
                try { hits = trace.Collect(200, null); }
                catch (Exception ex)
                {
                    ctx.LogText("trace collect failed " + Errors.Classify(ex));
                    hits = new();
                }
            }
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
            var fields = new JsonObject { ["status"] = "PASS" };
            var objects = DurationObjects(hits, allowed);
            if (objects != null) fields["objects"] = objects;
            return Ctx.Ok("refresh", fields);
        }
        finally
        {
            trace?.Dispose();
            Bounded.Run(() =>
            {
                if (server.Connected) server.Disconnect();
                server.Dispose();
            });
        }
    }

    static void Allow(HashSet<string> allowed, string name)
    {
        if (Printable(name)) allowed.Add(name);
    }

    static bool Printable(string name)
    {
        if (name.Length is < 1 or > 512) return false;
        foreach (char c in name)
            if (c < ' ') return false;
        return true;
    }

    /// <summary>Known table, partition and database names only. Raw durations stay in this process. Null when nothing was captured.</summary>
    static JsonArray? DurationObjects(List<EngineTrace.Hit> hits, HashSet<string> allowed)
    {
        var best = new Dictionary<string, long>(StringComparer.Ordinal);
        foreach (var h in hits)
        {
            if (!string.Equals(h.Class, "ProgressReportEnd", StringComparison.Ordinal)) continue;
            string? name = MatchName(h.ObjectName, allowed);
            if (name == null) continue;
            long d = h.Duration < 0 ? 0 : h.Duration;
            if (!best.TryGetValue(name, out long cur) || d > cur) best[name] = d;
        }
        if (best.Count == 0) return null;
        var arr = new JsonArray();
        foreach (var name in best.Keys.OrderBy(n => n, StringComparer.OrdinalIgnoreCase).ThenBy(n => n, StringComparer.Ordinal))
        {
            arr.Add(new JsonObject
            {
                ["name"] = name,
                ["duration_band"] = TraceBands.Duration(best[name]),
            });
        }
        return arr;
    }

    static string? MatchName(string raw, HashSet<string> allowed)
    {
        if (allowed.Contains(raw)) return raw;
        int dot = raw.LastIndexOf('.');
        if (dot >= 0 && dot + 1 < raw.Length)
        {
            string tail = raw[(dot + 1)..];
            if (allowed.Contains(tail)) return tail;
        }
        return null;
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
