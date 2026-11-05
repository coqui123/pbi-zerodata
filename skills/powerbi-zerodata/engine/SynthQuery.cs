using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>
/// synth-query: runs a caller DAX query and returns its rows, but only against a pbz synthetic model
/// (pbz synth build: fake data generated from the sanitized mirror). Two independent proofs are required
/// before anything is evaluated:
/// 1. every partition of every user table is calculated (DAX) or a calculation group, so no data source can
///    feed the model;
/// 2. the model annotation PBZ_SYNTHETIC equals HMAC(key, "pbz-synthetic:" + sorted non-private table
///    lineage tags) with the workspace key in private_dir. Lineage tags are regenerated for every synthetic
///    build, so the marker cannot be copied onto a real model, and pbz push refuses the annotation anyway.
/// Any other model gets BAD_REQUEST and no rows. Rows are capped at 500.
/// </summary>
public static class SynthQuery
{
    const string Marker = "PBZ_SYNTHETIC";
    const int ModelObjectType = 1;                                   // TOM ObjectType.Model
    static readonly HashSet<int> CalculatedSources = new() { 2, 7 };  // Calculated, CalculationGroup

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        string dax = req.Str("dax") ?? throw new BadRequestException("dax is required");
        int max = Math.Clamp(req.Int("max_rows") ?? 500, 1, 500);
        Dax.GuardQuery(dax, "dax");
        var vault = Vault.Open(ctx.RequirePrivateDir());
        using var conn = Conn.Open(ctx, port);

        foreach (var t in conn.Meta.UserTables)
            if (t.Partitions.Count == 0 || t.Partitions.Any(p => !CalculatedSources.Contains(p.SourceType)))
                throw new BadRequestException("not a synthetic model: a table has a data-source partition");

        string? marker = null;
        foreach (var r in conn.Dmv("TMSCHEMA_ANNOTATIONS", new[] { "ObjectType", "Name", "Value" }))
        {
            int type = r[0] == null ? -1 : Convert.ToInt32(r[0], CultureInfo.InvariantCulture);
            if (type == ModelObjectType && string.Equals(r[1] as string, Marker, StringComparison.Ordinal))
                marker = (r[2] as string ?? "").Trim().ToLowerInvariant();
        }
        if (string.IsNullOrEmpty(marker)) throw new BadRequestException("not a synthetic model: no marker");

        var tags = new List<string>();
        foreach (var r in conn.Dmv("TMSCHEMA_TABLES", new[] { "Name", "LineageTag", "IsPrivate", "ShowAsVariationsOnly" }))
        {
            string name = r[0] as string ?? "";
            bool priv = (r[2] is bool p2 && p2) || (r[3] is bool p3 && p3)
                        || name.StartsWith("LocalDateTable_", StringComparison.Ordinal)
                        || name.StartsWith("DateTableTemplate_", StringComparison.Ordinal);
            string tag = (r[1] as string ?? "").Trim().ToLowerInvariant();
            if (!priv && tag.Length > 0) tags.Add(tag);
        }
        tags.Sort(StringComparer.Ordinal);
        string expected = vault.HmacText("pbz-synthetic:" + string.Join(",", tags), alreadyNormalized: false);
        if (!CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(expected), Encoding.UTF8.GetBytes(marker)))
            throw new BadRequestException("not a synthetic model: marker does not verify");

        var (cols, rows) = conn.Table(dax, max + 1);
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
