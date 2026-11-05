using System.Globalization;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PbzEngine;

/// <summary>
/// deps: the engine's own object dependency list, for cross-checking pbz's metadata parser.
/// Reads $SYSTEM.DISCOVER_CALC_DEPENDENCY with a fixed column list: object identity only. The EXPRESSION,
/// REFERENCED_EXPRESSION and QUERY columns (formula text, which can hold literals) are never selected.
/// Every name must equal a TMSCHEMA object name (table, column, measure, hierarchy, partition,
/// relationship), else it is replaced by "?"; object types must match ^[A-Z_]{1,40}$, else "?".
/// </summary>
public static class Deps
{
    const string Statement =
        "SELECT [OBJECT_TYPE], [TABLE], [OBJECT], [REFERENCED_OBJECT_TYPE], [REFERENCED_TABLE], [REFERENCED_OBJECT] " +
        "FROM $SYSTEM.DISCOVER_CALC_DEPENDENCY";

    static readonly Regex TypeRe = new("^[A-Z_]{1,40}$", RegexOptions.CultureInvariant);

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        using var conn = Conn.Open(ctx, port);
        var meta = conn.Meta;
        var known = new HashSet<string>(meta.KnownNames(), StringComparer.Ordinal);
        foreach (var t in meta.Tables)
            foreach (var p in t.Partitions) known.Add(p.Name);
        foreach (var r in meta.Relationships) known.Add(r.Name);

        string Name(object? v)
        {
            string s = v == null ? "" : Convert.ToString(v, CultureInfo.InvariantCulture) ?? "";
            return s.Length == 0 || known.Contains(s) ? s : "?";
        }
        string Type(object? v)
        {
            string s = v == null ? "" : Convert.ToString(v, CultureInfo.InvariantCulture) ?? "";
            return TypeRe.IsMatch(s) ? s : "?";
        }

        var edges = new JsonArray();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var r in conn.Rows(Statement, 200000))
        {
            var row = new[] { Type(r[0]), Name(r[1]), Name(r[2]), Type(r[3]), Name(r[4]), Name(r[5]) };
            if (!seen.Add(string.Join("\u0001", row))) continue;
            var a = new JsonArray();
            foreach (var cell in row) a.Add(cell);
            edges.Add(a);
        }
        return Ctx.Ok("deps", new JsonObject { ["edges"] = edges });
    }
}
