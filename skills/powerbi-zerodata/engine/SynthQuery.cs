using System.Globalization;
using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>Return buffered rows only after signed live-definition and catalog verification
/// on both sides of execution. Missing verification is always a refusal.</summary>
public static class SynthQuery
{
    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        string dax = req.Str("dax") ?? throw new BadRequestException("dax is required");
        int max = Math.Clamp(req.Int("max_rows") ?? 500, 1, 500);
        Dax.GuardQuery(dax, "dax");
        using var conn = Conn.Open(ctx, port);
        var (cols, rows) = SyntheticProvenance.Buffered(
            () => SyntheticProvenance.Capture(req, ctx, conn),
            snapshot => SyntheticProvenance.Verify(ctx, snapshot),
            () => conn.Table(dax, max + 1));

        var colArr = new JsonArray();
        foreach (var c in cols) colArr.Add(c);
        var rowArr = new JsonArray();
        foreach (var row in rows.Take(max))
        {
            var a = new JsonArray();
            foreach (var v in row) a.Add(ToNode(v));
            rowArr.Add(a);
        }
        return Ctx.Ok("synth-query", new JsonObject
        {
            ["verified"] = true,
            ["columns"] = colArr,
            ["rows"] = rowArr,
            ["truncated"] = rows.Count > max,
        });
    }

    static JsonNode? ToNode(object? v) => v switch
    {
        null => null,
        bool b => JsonValue.Create(b),
        string s => JsonValue.Create(s),
        DateTime d => JsonValue.Create(d.ToString("yyyy-MM-dd'T'HH:mm:ss", CultureInfo.InvariantCulture)),
        double d when double.IsNaN(d) || double.IsInfinity(d) => JsonValue.Create(d.ToString(CultureInfo.InvariantCulture)),
        double d => JsonValue.Create(d),
        float f => JsonValue.Create((double)f),
        decimal m => JsonValue.Create((double)m),
        long l => JsonValue.Create(l),
        int i => JsonValue.Create(i),
        short s16 => JsonValue.Create((int)s16),
        byte b8 => JsonValue.Create((int)b8),
        ulong u => JsonValue.Create((double)u),
        _ => JsonValue.Create(Convert.ToString(v, CultureInfo.InvariantCulture) ?? ""),
    };
}
