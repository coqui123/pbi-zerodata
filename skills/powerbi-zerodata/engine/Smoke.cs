using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>smoke: runs one caller-built query per visual; reports OK (rows), EMPTY, UNSUPPORTED (no query) or ERROR class.</summary>
public static class Smoke
{
    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        var visuals = req.Arr("visuals") ?? throw new BadRequestException("visuals is required");
        using var conn = Conn.Open(ctx, port);
        var outArr = new JsonArray();
        foreach (var n in visuals)
        {
            if (n is not JsonObject v) throw new BadRequestException("visuals must be objects");
            string id = J.Id(v);
            var o = new JsonObject { ["id"] = id };
            string? dax = J.Str(v, "dax");
            if (string.IsNullOrWhiteSpace(dax))
            {
                o["status"] = "UNSUPPORTED";
                outArr.Add(o);
                continue;
            }
            try
            {
                Dax.GuardQuery(dax, "dax");
                var rows = conn.Rows(dax, 1);
                o["status"] = rows.Count > 0 ? "OK" : "EMPTY";
            }
            catch (Exception ex)
            {
                var cls = Errors.Classify(ex, new[] { dax });
                o["status"] = "ERROR";
                o["class"] = cls.ToString();
                var logId = ctx.Log(ex, id);
                if (logId != null) o["log_id"] = logId;
                if (cls == ErrorClass.TIMEOUT) { try { conn.Reset(); } catch { } }
            }
            outArr.Add(o);
        }
        return Ctx.Ok("smoke", new JsonObject { ["visuals"] = outArr });
    }
}
