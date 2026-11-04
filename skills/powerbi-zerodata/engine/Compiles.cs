using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>
/// compiles: bind-check DAX scalar expressions without evaluating them.
/// Spike result: DEFINE MEASURE + EVALUATE ROW("x", IF(FALSE(), [m])) surfaces syntax and bind errors
/// (unknown column/measure/table/function, naked column) but never evaluates the expression (ERROR() and
/// conversion failures stay silent), and the only cell returned is BLANK. TOPN(0, ...) does evaluate, so it is
/// not used.
/// </summary>
public static class Compiles
{
    public const string ProbeMeasure = "__pbz_c";

    public static string Query(string table, string expr)
        => "DEFINE MEASURE " + Dax.QTable(table) + "[" + ProbeMeasure + "] = " + expr + "\n" +
           "EVALUATE ROW(\"x\", IF(FALSE(), [" + ProbeMeasure + "]))";

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        var items = req.Arr("items") ?? throw new BadRequestException("items is required");
        using var conn = Conn.Open(ctx, port);
        var scrub = new Scrubber(conn.Meta.KnownNames());
        var outItems = new JsonArray();
        foreach (var node in items)
        {
            if (node is not JsonObject item) throw new BadRequestException("items must be objects");
            string id = J.Id(item);
            var res = new JsonObject { ["id"] = id };
            string? query = null;
            string? expr = null;
            try
            {
                string table = J.ReqStr(item, "table");
                expr = J.ReqStr(item, "expression");
                Dax.Guard(expr, "expression");
                query = Query(table, expr);
                conn.Rows(query, 1);
                res["status"] = "PASS";
            }
            catch (Exception ex)
            {
                var echoes = new List<string>();
                if (query != null) echoes.Add(query);
                if (expr != null) echoes.Add(expr);
                var cls = Errors.Classify(ex, echoes);
                res["status"] = "ERROR";
                res["class"] = cls.ToString();
                // Messages only for engine bind errors (metadata level); local request errors carry the class alone.
                if (ex is not BadRequestException and not EngineException and not TimeoutException)
                    res["message"] = scrub.Scrub(Errors.Chain(ex), echoes);
                var logId = ctx.Log(ex, id);
                if (logId != null) res["log_id"] = logId;
                if (cls == ErrorClass.TIMEOUT) conn.Reset();
            }
            outItems.Add(res);
        }
        return Ctx.Ok("compiles", new JsonObject { ["items"] = outItems });
    }
}
