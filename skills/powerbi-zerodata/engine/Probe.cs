using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>probe: fixed shape facts for one table (booleans in both tiers, buckets in shape tier).</summary>
public static class Probe
{
    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        string tableName = J.ReqStr(req.Obj, "table");
        var colNames = req.StrList("columns");
        using var conn = Conn.Open(ctx, port);
        var t = conn.Meta.FindTable(tableName) ?? throw new EngineException(ErrorClass.UNKNOWN_OBJECT, "probe table not found");
        List<ColumnMeta> cols;
        if (colNames == null || colNames.Count == 0) cols = t.Columns.ToList();
        else
        {
            cols = new List<ColumnMeta>();
            foreach (var n in colNames)
            {
                var parsed = Dax.ParseColRef(n);
                if (parsed != null && !string.Equals(parsed.Value.Table, t.Name, StringComparison.OrdinalIgnoreCase))
                    throw new EngineException(ErrorClass.UNKNOWN_OBJECT, "probe column not found");
                string colName = parsed?.Column ?? n;
                var c = conn.Meta.FindColumn(t.Name, colName) ?? throw new EngineException(ErrorClass.UNKNOWN_OBJECT, "probe column not found");
                if (!cols.Contains(c)) cols.Add(c);
            }
        }
        var (rows, stats) = ColumnStats.Compute(conn, t, cols);
        var outCols = new JsonArray();
        foreach (var c in cols)
        {
            var o = new JsonObject { ["column"] = c.Name, ["type"] = c.TypeName };
            if (stats.TryGetValue(c, out var s))
            {
                o["has_blanks"] = s.Blanks > 0;
                if (c.IsNumeric) o["has_negatives"] = s.Negatives;
                if (c.IsDate) o["has_future_dates"] = s.Future;
                if (!ctx.Strict)
                {
                    o["blank_bucket"] = Buckets.Share(s.Blanks, rows);
                    o["card_bucket"] = Buckets.Count(s.Distinct);
                }
            }
            outCols.Add(o);
        }
        var resp = new JsonObject { ["table"] = t.Name };
        if (!ctx.Strict) resp["rows_bucket"] = Buckets.Count(rows);
        resp["columns"] = outCols;
        return Ctx.Ok("probe", resp);
    }
}
