using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PbzEngine;

/// <summary>
/// snapshot-save / snapshot-compare. A snapshot stores, per query, keyed row-tuple HMACs (or a multiset of row
/// HMACs when there are no keys) plus the latest partition RefreshedTime. Only HMACs and column names are stored;
/// only SAME/DIFF/NEW/MISSING/STALE_DATA, column names and mismatch classes are printed.
/// </summary>
public static class Snapshots
{
    static readonly Regex NameRx = new(@"^[A-Za-z0-9._\-]{1,64}$", RegexOptions.Compiled);

    sealed class Result
    {
        public List<string> Columns = new();      // display names, in result order
        public List<int> KeyIdx = new();
        public Dictionary<string, List<List<string>>> Rows = new(StringComparer.Ordinal); // key hmac -> rows of value-cell hmacs
        public Dictionary<string, int> Multiset = new(StringComparer.Ordinal);
        public bool Keyed => KeyIdx.Count > 0;
        public List<string> ValueColumns => Columns.Where((_, i) => !KeyIdx.Contains(i)).ToList();
    }

    static string FilePath(string pd, string name) => Path.Combine(pd, "snapshots", name + ".json");

    static string Name(Req req)
    {
        string name = J.ReqStr(req.Obj, "name");
        if (!NameRx.IsMatch(name)) throw new BadRequestException("snapshot name must match [A-Za-z0-9._-]{1,64}");
        return name;
    }

    static List<(string Id, string Dax, List<string> Keys)> Queries(Req req)
    {
        var arr = req.Arr("queries") ?? throw new BadRequestException("queries is required");
        var list = new List<(string, string, List<string>)>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var n in arr)
        {
            if (n is not JsonObject q) throw new BadRequestException("queries must be objects");
            string id = J.Id(q);
            if (!seen.Add(id)) throw new BadRequestException("duplicate query id");
            string dax = J.ReqStr(q, "dax");
            Dax.GuardQuery(dax, "dax");
            list.Add((id, dax, J.StrList(q, "keys") ?? new List<string>()));
        }
        return list;
    }

    static Result RunQuery(Conn conn, Vault vault, string dax, List<string> keys)
    {
        var (cols, rows) = conn.Table(dax);
        var res = new Result { Columns = cols.Select(c => Dax.Cap(Dax.ResultName(c))).ToList() };
        foreach (var k in keys)
        {
            string want = Dax.ResultName(k);
            int idx = res.Columns.FindIndex(c => string.Equals(c, want, StringComparison.OrdinalIgnoreCase));
            if (idx < 0) throw new BadRequestException("snapshot key column not in the query result");
            if (!res.KeyIdx.Contains(idx)) res.KeyIdx.Add(idx);
        }
        foreach (var r in rows)
        {
            if (res.Keyed)
            {
                string kh = vault.HmacTuple(res.KeyIdx.Select(i => r[i]));
                var cells = new List<string>();
                for (int i = 0; i < r.Length; i++) if (!res.KeyIdx.Contains(i)) cells.Add(vault.Hmac(r[i]));
                if (!res.Rows.TryGetValue(kh, out var list)) res.Rows[kh] = list = new List<List<string>>();
                list.Add(cells);
            }
            else
            {
                string rh = vault.HmacTuple(r);
                res.Multiset[rh] = res.Multiset.GetValueOrDefault(rh) + 1;
            }
        }
        foreach (var l in res.Rows.Values) l.Sort((a, b) => string.CompareOrdinal(string.Join(",", a), string.Join(",", b)));
        return res;
    }

    static JsonObject ToJson(Result r)
    {
        var o = new JsonObject
        {
            ["columns"] = new JsonArray(r.Columns.Select(c => (JsonNode)c).ToArray()),
            ["key_columns"] = new JsonArray(r.KeyIdx.Select(i => (JsonNode)i).ToArray()),
        };
        if (r.Keyed)
        {
            var rows = new JsonObject();
            foreach (var kv in r.Rows.OrderBy(k => k.Key, StringComparer.Ordinal))
                rows[kv.Key] = new JsonArray(kv.Value.Select(cells => (JsonNode)new JsonArray(cells.Select(c => (JsonNode)c).ToArray())).ToArray());
            o["rows"] = rows;
        }
        else
        {
            var ms = new JsonObject();
            foreach (var kv in r.Multiset.OrderBy(k => k.Key, StringComparer.Ordinal)) ms[kv.Key] = kv.Value;
            o["multiset"] = ms;
        }
        return o;
    }

    static Result FromJson(JsonObject o)
    {
        var r = new Result();
        foreach (var c in o["columns"]!.AsArray()) r.Columns.Add(c!.GetValue<string>());
        foreach (var k in o["key_columns"]!.AsArray()) r.KeyIdx.Add(k!.GetValue<int>());
        if (o["rows"] is JsonObject rows)
            foreach (var kv in rows)
                r.Rows[kv.Key] = kv.Value!.AsArray().Select(cells => cells!.AsArray().Select(c => c!.GetValue<string>()).ToList()).ToList();
        if (o["multiset"] is JsonObject ms)
            foreach (var kv in ms) r.Multiset[kv.Key] = kv.Value!.GetValue<int>();
        return r;
    }

    public static JsonObject Save(Req req, Ctx ctx)
    {
        int port = req.Port();
        string name = Name(req);
        var queries = Queries(req);
        string pd = ctx.RequirePrivateDir();
        var vault = Vault.Open(pd);
        using var conn = Conn.Open(ctx, port);
        var stored = new JsonObject();
        var outArr = new JsonArray();
        foreach (var (id, dax, keys) in queries)
        {
            var o = new JsonObject { ["id"] = id };
            try
            {
                stored[id] = ToJson(RunQuery(conn, vault, dax, keys));
                o["result"] = "SAVED";
            }
            catch (Exception ex)
            {
                var cls = Errors.Classify(ex, new[] { dax });
                o["result"] = "ERROR";
                o["class"] = cls.ToString();
                var logId = ctx.Log(ex, id);
                if (logId != null) o["log_id"] = logId;
                if (cls == ErrorClass.TIMEOUT) { try { conn.Reset(); } catch { } }
            }
            outArr.Add(o);
        }
        var doc = new JsonObject
        {
            ["version"] = 1,
            ["name"] = name,
            ["refreshed"] = conn.LatestRefresh(),
            ["queries"] = stored,
        };
        Vault.WriteAtomic(FilePath(pd, name), doc.ToJsonString());
        return Ctx.Ok("snapshot-save", new JsonObject { ["name"] = name, ["queries"] = outArr });
    }

    public static JsonObject Compare(Req req, Ctx ctx)
    {
        int port = req.Port();
        string name = Name(req);
        var queries = Queries(req);
        string pd = ctx.RequirePrivateDir();
        var vault = Vault.Open(pd);
        using var conn = Conn.Open(ctx, port);
        JsonObject? saved = null;
        string path = FilePath(pd, name);
        if (File.Exists(path)) saved = JsonNode.Parse(File.ReadAllText(path, Encoding.UTF8)) as JsonObject;
        var savedQueries = saved?["queries"] as JsonObject;
        bool stale = saved != null && !string.Equals(saved["refreshed"]?.GetValue<string>(), conn.LatestRefresh(), StringComparison.Ordinal);
        var outArr = new JsonArray();
        var requested = new HashSet<string>(StringComparer.Ordinal);
        foreach (var (id, dax, keys) in queries)
        {
            requested.Add(id);
            var o = new JsonObject { ["id"] = id };
            if (savedQueries == null || savedQueries[id] is not JsonObject sq)
            {
                o["result"] = "NEW";
                outArr.Add(o);
                continue;
            }
            try
            {
                var before = FromJson(sq);
                var now = RunQuery(conn, vault, dax, keys);
                var (same, columns, groups) = Diff(before, now);
                if (same) o["result"] = "SAME";
                else if (stale) o["result"] = "STALE_DATA";
                else
                {
                    o["result"] = "DIFF";
                    o["columns"] = new JsonArray(columns.Select(c => (JsonNode)c).ToArray());
                    o["mismatch"] = Buckets.Mismatch(groups);
                }
            }
            catch (Exception ex)
            {
                var cls = Errors.Classify(ex, new[] { dax });
                o.Clear();
                o["id"] = id;
                o["result"] = "ERROR";
                o["class"] = cls.ToString();
                var logId = ctx.Log(ex, id);
                if (logId != null) o["log_id"] = logId;
                if (cls == ErrorClass.TIMEOUT) { try { conn.Reset(); } catch { } }
            }
            outArr.Add(o);
        }
        if (savedQueries != null)
            foreach (var kv in savedQueries.OrderBy(k => k.Key, StringComparer.Ordinal))
                if (!requested.Contains(kv.Key) && J.IsSafeId(kv.Key))
                    outArr.Add(new JsonObject { ["id"] = kv.Key, ["result"] = "MISSING" });
        return Ctx.Ok("snapshot-compare", new JsonObject { ["name"] = name, ["queries"] = outArr });
    }

    /// <summary>Returns (same, differing column names, number of differing groups/rows).</summary>
    static (bool Same, List<string> Columns, long Groups) Diff(Result a, Result b)
    {
        var aVals = a.ValueColumns;
        var bVals = b.ValueColumns;
        var diffCols = new SortedSet<string>(StringComparer.Ordinal);
        foreach (var c in aVals.Except(bVals, StringComparer.OrdinalIgnoreCase)) diffCols.Add(c);
        foreach (var c in bVals.Except(aVals, StringComparer.OrdinalIgnoreCase)) diffCols.Add(c);
        bool columnSetDiffers = diffCols.Count > 0;
        var aKeys = a.KeyIdx.Select(i => a.Columns[i]).ToList();
        var bKeys = b.KeyIdx.Select(i => b.Columns[i]).ToList();
        bool keyShapeSame = a.Keyed == b.Keyed && aKeys.SequenceEqual(bKeys, StringComparer.OrdinalIgnoreCase);

        if (!keyShapeSame)
        {
            foreach (var c in aVals.Concat(bVals)) diffCols.Add(c);
            foreach (var c in aKeys.Concat(bKeys)) diffCols.Add(c);
            long n = Math.Max(a.Keyed ? a.Rows.Count : a.Multiset.Values.Sum(), b.Keyed ? b.Rows.Count : b.Multiset.Values.Sum());
            return (false, diffCols.ToList(), Math.Max(1, n));
        }

        if (!a.Keyed)
        {
            long delta = 0;
            foreach (var h in a.Multiset.Keys.Union(b.Multiset.Keys))
                delta += Math.Abs(a.Multiset.GetValueOrDefault(h) - b.Multiset.GetValueOrDefault(h));
            bool same = delta == 0 && diffCols.Count == 0;
            if (!same) foreach (var c in b.Columns) diffCols.Add(c);
            return (same, diffCols.ToList(), delta);
        }

        // keyed: compare per key, attributing differences to value columns by name
        var aPos = aVals.Select((c, i) => (c, i)).ToDictionary(x => x.c, x => x.i, StringComparer.OrdinalIgnoreCase);
        var bPos = bVals.Select((c, i) => (c, i)).ToDictionary(x => x.c, x => x.i, StringComparer.OrdinalIgnoreCase);
        var common = aVals.Where(c => bPos.ContainsKey(c)).ToList();
        long groups = 0;
        foreach (var key in a.Rows.Keys.Union(b.Rows.Keys))
        {
            bool inA = a.Rows.TryGetValue(key, out var ra);
            bool inB = b.Rows.TryGetValue(key, out var rb);
            if (!inA || !inB)
            {
                groups++;
                foreach (var c in aVals.Concat(bVals)) diffCols.Add(c);
                continue;
            }
            bool groupDiff = columnSetDiffers;
            if (ra!.Count != rb!.Count)
            {
                groupDiff = true;
                foreach (var c in common) diffCols.Add(c);
            }
            else
            {
                for (int r = 0; r < ra.Count; r++)
                    foreach (var c in common)
                        if (!string.Equals(ra[r][aPos[c]], rb[r][bPos[c]], StringComparison.Ordinal))
                        {
                            groupDiff = true;
                            diffCols.Add(c);
                        }
            }
            if (groupDiff) groups++;
        }
        return (groups == 0 && diffCols.Count == 0, diffCols.ToList(), groups);
    }
}
