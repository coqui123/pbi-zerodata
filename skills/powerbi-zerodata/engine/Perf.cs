using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using TOM = Microsoft.AnalysisServices.Tabular;

namespace PbzEngine;

/// <summary>
/// perf: cold-run a guarded DAX query, discard every result row, and release only closed bands.
/// Durations, plan text and storage-engine text stay in this process. A trace that cannot start
/// still returns a valid response with trace UNAVAILABLE. A guard refusal is an error.
/// ExecutionMetrics is service-only and is not required.
/// </summary>
public static class Perf
{
    const double Ratio = 1.25;

    static readonly string[] Events =
    {
        "QueryEnd", "VertiPaqSEQueryEnd", "VertiPaqSEQueryCacheMatch",
        "DirectQueryEnd", "AggregateTableRewriteQuery", "DAXQueryPlan",
    };

    static readonly Regex BaselineName = new("^[A-Za-z0-9._\\-]{1,64}$", RegexOptions.Compiled | RegexOptions.CultureInvariant);
    static readonly Regex RecordsRx = new(@"#Records\s*=\s*(\d+)", RegexOptions.IgnoreCase | RegexOptions.Compiled | RegexOptions.CultureInvariant);

    sealed class Sample
    {
        public bool HasEnd;
        public double Ms;
        public double SeMs;
        public int Se;
        public int Dq;
        public int Cache;
        public bool Callback;
        public string Agg = "NONE";
        public bool HasPlan;
        public int CallbackOps;
        public int SpoolOps;
        public int CrossOps;
        public int LargeOps;
    }

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        string dax = req.Str("dax") ?? throw new BadRequestException("dax is required");
        if (string.IsNullOrWhiteSpace(dax)) throw new BadRequestException("dax is required");
        Dax.GuardQuery(dax, "dax");
        int runs = req.Int("runs") ?? throw new BadRequestException("runs is required");
        if (runs < 1 || runs > 20) throw new BadRequestException("runs must be from 1 to 20");
        string? action = req.Str("action");
        if (action != null && action is not ("save" or "compare"))
            throw new BadRequestException("action must be save or compare");
        string? baseline = req.Str("baseline");
        if (action != null && (baseline == null || !BaselineName.IsMatch(baseline)))
            throw new BadRequestException("baseline name is not allowed");

        using var conn = Conn.Open(ctx, port);
        string databaseId = conn.Meta.DatabaseId;
        var server = new TOM.Server();
        EngineTrace? trace = null;
        try
        {
            ctx.Guard(() =>
            {
                server.Connect("Data Source=localhost:" + port.ToString(CultureInfo.InvariantCulture) + ";");
                return 0;
            }, Math.Min(ctx.TimeoutSec, 60));
            trace = EngineTrace.TryStart(server, Events, ctx);

            var samples = new List<Sample>(runs);
            for (int i = 0; i < runs; i++)
            {
                ctx.Guard(() => { ClearCache(server, databaseId); return 0; });
                trace?.Arm();
                conn.Drain(dax);
                List<EngineTrace.Hit> hits;
                try
                {
                    hits = trace == null
                        ? new List<EngineTrace.Hit>()
                        : trace.Collect(1500, h => h.Class == "QueryEnd");
                }
                catch (Exception ex)
                {
                    ctx.LogText("trace collect failed " + Errors.Classify(ex));
                    hits = new List<EngineTrace.Hit>();
                }
                samples.Add(Interpret(hits, dax));
                foreach (var h in hits) h.Text = "";
            }
            return Finish(ctx, samples, trace != null && trace.Started, action, baseline);
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

    /// <summary>Same rule as pbz/perf.py (RATIO 1.25). The three numbers never leave this process.</summary>
    public static string Verdict(double oldMs, double newMs, double spread)
    {
        if (!(oldMs > 0) || !double.IsFinite(newMs)) return "NOISY";
        double ratio = newMs / oldMs;
        double delta = Math.Abs(newMs - oldMs);
        if (ratio > Ratio || ratio < 1.0 / Ratio)
        {
            if (delta <= spread) return "NOISY";
            return ratio > Ratio ? "SLOWER" : "FASTER";
        }
        return "SAME";
    }

    static JsonObject Finish(Ctx ctx, List<Sample> samples, bool captured, string? action, string? baseline)
    {
        var timed = new List<double>();
        foreach (var s in samples)
            if (s.HasEnd && double.IsFinite(s.Ms) && s.Ms >= 0) timed.Add(s.Ms);
        timed.Sort();

        bool banded = timed.Count > 0;
        double median = 0;
        double spread = 0;
        Sample? rep = null;
        if (banded)
        {
            median = timed.Count % 2 == 1
                ? timed[timed.Count / 2]
                : (timed[timed.Count / 2 - 1] + timed[timed.Count / 2]) / 2.0;
            spread = timed[timed.Count - 1] - timed[0];
            if (spread < 0 || !double.IsFinite(spread)) spread = 0;
            double best = double.MaxValue;
            foreach (var s in samples)
            {
                if (!s.HasEnd) continue;
                double dist = Math.Abs(s.Ms - median);
                if (rep == null || dist < best)
                {
                    rep = s;
                    best = dist;
                }
            }
        }

        var fields = new JsonObject
        {
            ["timing"] = banded ? "BANDED" : "UNAVAILABLE",
            ["trace"] = captured ? "CAPTURED" : "UNAVAILABLE",
        };
        if (banded)
            fields["duration_band"] = TraceBands.Duration(median);
        if (rep != null)
        {
            fields["fe_share"] = Share(rep);
            fields["se_queries"] = TraceBands.Events(rep.Se);
            fields["dq_events"] = TraceBands.Events(rep.Dq);
            fields["cache_hit"] = rep.Cache <= 0 ? "NONE" : rep.Cache > rep.Se ? "MOST" : "SOME";
            fields["callback"] = rep.Callback;
            fields["agg_rewrite"] = rep.Agg;
            if (rep.HasPlan)
            {
                fields["plan"] = new JsonObject
                {
                    ["CallbackDataID"] = TraceBands.Plan(rep.CallbackOps),
                    ["Spool_Iterator"] = TraceBands.Plan(rep.SpoolOps),
                    ["CrossApply"] = TraceBands.Plan(rep.CrossOps),
                    ["LargeMaterialization"] = TraceBands.Plan(rep.LargeOps),
                };
            }
        }

        if (action == "save")
        {
            if (banded && baseline != null)
            {
                SaveBaseline(ctx.RequirePrivateDir(), baseline, median, spread);
                fields["baseline"] = "SAVED";
            }
        }
        else if (action == "compare" && baseline != null)
        {
            if (!TryReadBaseline(ctx.RequirePrivateDir(), baseline, out double old, out double oldSpread))
                fields["baseline"] = "MISSING";
            else if (banded)
                fields["verdict"] = Verdict(old, median, Math.Max(oldSpread, spread));
        }
        return Ctx.Ok("perf", fields);
    }

    static string Share(Sample s)
    {
        double fe = s.Ms - s.SeMs;
        if (fe < 0 || !double.IsFinite(fe)) fe = 0;
        double share = s.Ms <= 0 ? 0 : fe / s.Ms;
        if (share < 0.25) return "LOW";
        if (share > 0.75) return "HIGH";
        return "MID";
    }

    static Sample Interpret(List<EngineTrace.Hit> hits, string dax)
    {
        var sample = new Sample();
        string needle = Compact(dax);
        EngineTrace.Hit? end = null;
        EngineTrace.Hit? longest = null;
        foreach (var h in hits)
        {
            if (h.Class != "QueryEnd" || h.Duration == EngineTrace.Hit.MissingDuration) continue;
            if (longest == null || h.Duration > longest.Duration) longest = h;
            if (QueryTextMatches(h.Text, needle) && (end == null || h.Duration > end.Duration)) end = h;
        }
        // The window was armed around this query. If the trace omitted or truncated the text, keep the longest QueryEnd.
        end ??= longest;
        if (end == null)
        {
            foreach (var h in hits) h.Text = "";
            return sample;
        }
        sample.HasEnd = true;
        sample.Ms = end.Duration;
        string session = end.Session;
        foreach (var h in hits)
        {
            if (!SameSession(h.Session, session)) continue;
            if (h.Subclass.Contains("ScanInternal", StringComparison.OrdinalIgnoreCase)) continue;
            switch (h.Class)
            {
                case "VertiPaqSEQueryEnd":
                    sample.Se++;
                    if (h.Duration != EngineTrace.Hit.MissingDuration)
                        sample.SeMs += h.Duration;
                    if (HasCallback(h.Text)) sample.Callback = true;
                    break;
                case "VertiPaqSEQueryCacheMatch":
                    sample.Cache++;
                    if (HasCallback(h.Text)) sample.Callback = true;
                    break;
                case "DirectQueryEnd":
                    sample.Dq++;
                    break;
                case "AggregateTableRewriteQuery":
                    if (sample.Agg != "HIT")
                        sample.Agg = h.Text.Contains("matchFound", StringComparison.OrdinalIgnoreCase) ? "HIT" : "MISS";
                    break;
            }
        }
        string? physical = null;
        string? any = null;
        foreach (var h in hits)
        {
            if (h.Class != "DAXQueryPlan" || h.Text.Length == 0 || !SameSession(h.Session, session)) continue;
            if (any == null || h.Text.Length > any.Length) any = h.Text;
            if (h.Subclass.Contains("Physical", StringComparison.OrdinalIgnoreCase)
                && (physical == null || h.Text.Length > physical.Length))
                physical = h.Text;
        }
        string? plan = physical ?? any;
        if (plan != null)
        {
            sample.HasPlan = true;
            sample.CallbackOps = CountToken(plan, "CallbackDataID");
            sample.SpoolOps = CountToken(plan, "Spool_Iterator");
            sample.CrossOps = CountToken(plan, "CrossApply");
            sample.LargeOps = LargeSpools(plan);
        }
        foreach (var h in hits) h.Text = "";
        return sample;
    }

    static bool SameSession(string eventSession, string querySession)
    {
        if (querySession.Length == 0 || eventSession.Length == 0) return true;
        return string.Equals(NormalizeSession(eventSession), NormalizeSession(querySession), StringComparison.OrdinalIgnoreCase);
    }

    static string NormalizeSession(string s) => s.Trim().Trim('{', '}');

    static bool QueryTextMatches(string text, string needle)
    {
        if (needle.Length == 0) return false;
        string got = Compact(text);
        if (got.Contains(needle, StringComparison.Ordinal)) return true;
        int take = Math.Min(needle.Length, 240);
        return take >= 24 && got.Contains(needle.Substring(0, take), StringComparison.Ordinal);
    }

    static bool HasCallback(string text)
        => text.Contains("CallbackDataID", StringComparison.OrdinalIgnoreCase)
           || text.Contains("EncodeCallback", StringComparison.OrdinalIgnoreCase);

    static int CountToken(string text, string token)
        => Regex.Matches(text, Regex.Escape(token), RegexOptions.IgnoreCase | RegexOptions.CultureInvariant).Count;

    /// <summary>A spool is large when <see cref="Buckets.Count"/> of its record count is 1k-9k or 10k+. The count is not emitted.</summary>
    static int LargeSpools(string text)
    {
        int n = 0;
        foreach (var line in text.Split('\n'))
        {
            if (line.IndexOf("Spool", StringComparison.OrdinalIgnoreCase) < 0) continue;
            bool large = false;
            foreach (Match m in RecordsRx.Matches(line))
            {
                if (!long.TryParse(m.Groups[1].Value, NumberStyles.None, CultureInfo.InvariantCulture, out long records)) continue;
                string band = Buckets.Count(records);
                if (band is "1k-9k" or "10k+") large = true;
            }
            if (large) n++;
        }
        return n;
    }

    static string Compact(string s)
    {
        var sb = new StringBuilder(s.Length);
        foreach (char c in s)
            if (!char.IsWhiteSpace(c)) sb.Append(c);
        return sb.ToString();
    }

    static void ClearCache(TOM.Server server, string databaseId)
    {
        string id = System.Security.SecurityElement.Escape(databaseId) ?? "";
        string xmla = "<ClearCache xmlns=\"http://schemas.microsoft.com/analysisservices/2003/engine\"><Object><DatabaseID>"
            + id + "</DatabaseID></Object></ClearCache>";
        try
        {
            var results = server.Execute(xmla);
            if (results != null && results.ContainsErrors)
                throw new EngineException(ErrorClass.OTHER, "clear cache failed");
        }
        catch (EngineException) { throw; }
        catch (Exception ex)
        {
            var cls = Errors.Classify(ex);
            throw new EngineException(cls == ErrorClass.TIMEOUT ? ErrorClass.TIMEOUT : ErrorClass.OTHER, "clear cache failed");
        }
    }

    static string BaselinePath(string privateDir, string name)
    {
        string dir = Path.Combine(privateDir, "perf");
        Directory.CreateDirectory(dir);
        string path = Path.GetFullPath(Path.Combine(dir, name + ".bin"));
        string root = Path.GetFullPath(dir + Path.DirectorySeparatorChar);
        if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            throw new BadRequestException("baseline name is not allowed");
        return path;
    }

    static void SaveBaseline(string privateDir, string name, double median, double spread)
    {
        var body = new JsonObject { ["median"] = median, ["spread"] = spread };
        Vault.WriteProtected(BaselinePath(privateDir, name), body.ToJsonString());
    }

    static bool TryReadBaseline(string privateDir, string name, out double median, out double spread)
    {
        median = 0;
        spread = 0;
        try
        {
            string path = BaselinePath(privateDir, name);
            if (!File.Exists(path)) return false;
            string text = Encoding.UTF8.GetString(Dpapi.Unprotect(File.ReadAllBytes(path)));
            if (JsonNode.Parse(text) is not JsonObject o) return false;
            if (!TryDouble(o["median"], out median)) return false;
            if (!TryDouble(o["spread"], out spread) || spread < 0 || !double.IsFinite(spread)) spread = 0;
            return true;
        }
        catch
        {
            return false;
        }
    }

    static bool TryDouble(JsonNode? n, out double d)
    {
        d = 0;
        if (n is not JsonValue v) return false;
        if (v.TryGetValue<double>(out d)) return double.IsFinite(d);
        if (v.TryGetValue<long>(out long l)) { d = l; return true; }
        if (v.TryGetValue<int>(out int i)) { d = i; return true; }
        return false;
    }
}
