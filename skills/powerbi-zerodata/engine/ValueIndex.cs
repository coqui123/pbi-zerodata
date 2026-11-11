using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PbzEngine;

/// <summary>Per-column aggregate facts computed inside the engine; only booleans and buckets leave the process.</summary>
public sealed class ColStats
{
    public long Blanks;
    public long Distinct;
    public bool Negatives;
    public bool Future;
    public int? YearMin;
    public int? YearMax;
}

public static class ColumnStats
{
    /// <summary>One ROW query per table: row count plus blank/distinct/negative/future/year facts per column.</summary>
    public static (long Rows, Dictionary<ColumnMeta, ColStats> Stats) Compute(Conn conn, TableMeta t, IReadOnlyList<ColumnMeta> cols)
    {
        var parts = new List<string> { "\"r\"", "COUNTROWS(" + Dax.QTable(t.Name) + ")" };
        var usable = cols.Where(c => !c.IsBinary).ToList();
        for (int i = 0; i < usable.Count; i++)
        {
            var c = usable[i];
            string idx = i.ToString(CultureInfo.InvariantCulture);
            parts.Add("\"b" + idx + "\""); parts.Add("CALCULATE(COUNTROWS(" + Dax.QTable(t.Name) + "), " + c.BlankPredicate + ")");
            parts.Add("\"d" + idx + "\""); parts.Add("DISTINCTCOUNTNOBLANK(" + c.Ref + ")");
            if (c.IsNumeric) { parts.Add("\"n" + idx + "\""); parts.Add("IF(MIN(" + c.Ref + ") < 0, 1, 0)"); }
            if (c.IsDate)
            {
                parts.Add("\"f" + idx + "\""); parts.Add("IF(MAX(" + c.Ref + ") > NOW(), 1, 0)");
                parts.Add("\"y" + idx + "\""); parts.Add("YEAR(MIN(" + c.Ref + "))");
                parts.Add("\"z" + idx + "\""); parts.Add("YEAR(MAX(" + c.Ref + "))");
            }
        }
        var (names, rows) = conn.Table("EVALUATE ROW(" + string.Join(", ", parts) + ")", 1);
        if (rows.Count == 0) throw new EngineException(ErrorClass.OTHER, "stats query returned no row");
        var row = rows[0];
        var at = new Dictionary<string, object?>(StringComparer.Ordinal);
        for (int i = 0; i < names.Count; i++) at[Dax.ResultName(names[i])] = row[i];
        long rowCount = L(at.GetValueOrDefault("r"));
        var stats = new Dictionary<ColumnMeta, ColStats>();
        for (int i = 0; i < usable.Count; i++)
        {
            var c = usable[i];
            string idx = i.ToString(CultureInfo.InvariantCulture);
            var s = new ColStats
            {
                Blanks = L(at.GetValueOrDefault("b" + idx)),
                Distinct = L(at.GetValueOrDefault("d" + idx)),
                Negatives = c.IsNumeric && L(at.GetValueOrDefault("n" + idx)) == 1,
                Future = c.IsDate && L(at.GetValueOrDefault("f" + idx)) == 1,
            };
            if (c.IsDate)
            {
                var y0 = at.GetValueOrDefault("y" + idx);
                var y1 = at.GetValueOrDefault("z" + idx);
                if (y0 != null) s.YearMin = (int)L(y0);
                if (y1 != null) s.YearMax = (int)L(y1);
            }
            stats[c] = s;
        }
        return (rowCount, stats);
    }

    public static long L(object? v) => v == null ? 0 : Norm.IsNumeric(v) ? (long)Math.Round(Norm.ToDouble(v)) : v is bool b ? (b ? 1 : 0) : 0;
}

/// <summary>
/// index: column classes for the mirror. Writes index.json (HMACs only) and categories.bin (DPAPI) in private_dir.
/// v1.1: name-like text columns also get word_hmacs (every word of every distinct value).
/// </summary>
public static class ValueIndex
{
    static readonly Regex NameLikeColumn = new(
        @"(?i)(name|owner|manager|\bpm\b|leader|lead|supervisor|resource|person|employee|staff|member|assignee|requester|approver|sponsor|contact|user)",
        RegexOptions.Compiled);

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        int? requested = req.Int("category_max");
        if (requested is int asked && (asked < 0 || asked > 100000))
            throw new BadRequestException("category_max out of range");
        // Policy owns the class boundary. A lower request value must not move it.
        int categoryMax = PolicyFile.CategoryMax(ctx.RequirePrivateDir());
        int valueCap = req.Int("value_cap") ?? 50000;
        if (valueCap < 1 || valueCap > 5000000) throw new BadRequestException("value_cap out of range");
        string pd = ctx.RequirePrivateDir();
        var vault = Vault.Open(pd);
        using var conn = Conn.Open(ctx, port);

        var indexCols = new JsonObject();
        var categories = new SortedDictionary<string, List<string>>(StringComparer.Ordinal);
        var outTables = new JsonArray();
        foreach (var t in conn.Meta.UserTables)
        {
            var cols = t.Columns.ToList();
            var (rows, stats) = ColumnStats.Compute(conn, t, cols);
            var outCols = new JsonArray();
            foreach (var c in cols)
            {
                var entry = new JsonObject { ["type"] = c.TypeName };
                var o = new JsonObject { ["column"] = c.Name, ["type"] = c.TypeName };
                stats.TryGetValue(c, out var s);
                if (c.IsText && s != null)
                {
                    string cls = s.Distinct == 0 ? "empty" : s.Distinct <= categoryMax ? "category" : "high";
                    entry["class"] = cls;
                    o["class"] = cls;
                    List<string>? values = null;
                    if (cls == "category")
                    {
                        values = DistinctText(conn, c, categoryMax);
                        categories[c.Key] = values;
                    }
                    else if (cls == "high")
                    {
                        values = DistinctText(conn, c, valueCap);
                        var hmacs = values.Select(v => vault.Hmac(v)).Distinct().OrderBy(h => h, StringComparer.Ordinal).ToList();
                        entry["hmacs"] = ToArr(hmacs);
                        if (s.Distinct > valueCap) entry["capped"] = true;
                    }
                    if (values != null && NameLikeColumn.IsMatch(c.Name))
                    {
                        var words = new HashSet<string>(StringComparer.Ordinal);
                        bool wordCapped = false;
                        foreach (var v in values)
                        {
                            foreach (var w in Words(v))
                            {
                                if (words.Count >= valueCap)
                                {
                                    wordCapped = true;
                                    break;
                                }
                                words.Add(vault.Hmac(w));
                            }
                            if (wordCapped) break;
                        }
                        entry["word_hmacs"] = ToArr(words.OrderBy(h => h, StringComparer.Ordinal));
                        if (wordCapped) entry["word_capped"] = true;
                    }
                }
                if (!ctx.Strict && s != null)
                {
                    if (c.IsText) o["card_bucket"] = Buckets.Count(s.Distinct);
                    o["blank_bucket"] = Buckets.Share(s.Blanks, rows);
                }
                indexCols[c.Key] = entry;
                outCols.Add(o);
            }
            var to = new JsonObject { ["table"] = t.Name };
            if (!ctx.Strict) to["rows_bucket"] = Buckets.Count(rows);
            to["columns"] = outCols;
            outTables.Add(to);
        }

        var indexDoc = new JsonObject { ["version"] = 1, ["columns"] = indexCols };
        Vault.WriteAtomic(Path.Combine(pd, "index.json"), indexDoc.ToJsonString(new JsonSerializerOptions { WriteIndented = false }));
        var catJson = JsonSerializer.Serialize(categories, new JsonSerializerOptions { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
        Vault.WriteProtected(Path.Combine(pd, "categories.bin"), catJson);

        return Ctx.Ok("index", new JsonObject { ["tables"] = outTables });
    }

    /// <summary>Distinct non-blank text values, ascending, at most <paramref name="cap"/> (values stay in-process).</summary>
    static List<string> DistinctText(Conn conn, ColumnMeta c, int cap)
    {
        if (cap <= 0) return new List<string>();
        string q = "EVALUATE TOPN(" + cap.ToString(CultureInfo.InvariantCulture) + ", FILTER(DISTINCT(" + c.Ref + "), NOT ISBLANK(" + c.Ref + ")), " + c.Ref + ", 1)";
        var rows = conn.Rows(q, cap);
        var list = new List<string>(rows.Count);
        foreach (var r in rows)
            if (r[0] is string s) list.Add(s);
            else if (r[0] != null) list.Add(Convert.ToString(r[0], CultureInfo.InvariantCulture) ?? "");
        list.Sort(StringComparer.Ordinal);
        return list;
    }

    /// <summary>Whitespace-separated words, non-letters stripped at both ends, kept when they hold at least 3 letters.</summary>
    public static IEnumerable<string> Words(string value)
    {
        foreach (var raw in value.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries))
        {
            int a = 0, b = raw.Length - 1;
            while (a <= b && !char.IsLetter(raw[a])) a++;
            while (b >= a && !char.IsLetter(raw[b])) b--;
            if (a > b) continue;
            string w = raw.Substring(a, b - a + 1);
            if (w.Count(char.IsLetter) >= 3) yield return w;
        }
    }

    static JsonArray ToArr(IEnumerable<string> items)
    {
        var a = new JsonArray();
        foreach (var i in items) a.Add(i);
        return a;
    }
}

/// <summary>category_max and reveal_columns from the private policy snapshot. A caller-supplied workspace is ignored.</summary>
public static class PolicyFile
{
    public const int DefaultCategoryMax = 50;

    static JsonObject? Read(string? privateDir)
    {
        if (string.IsNullOrWhiteSpace(privateDir) || !Path.IsPathFullyQualified(privateDir)) return null;
        string root;
        string path;
        try
        {
            root = Path.GetFullPath(privateDir);
            path = Path.GetFullPath(Path.Combine(root, "policy-snapshot.json"));
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException or IOException)
        {
            return null;
        }
        if (!path.StartsWith(root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            return null;
        if (!File.Exists(path)) return null;
        try
        {
            if ((File.GetAttributes(root) & FileAttributes.ReparsePoint) != 0) return null;
            if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) return null;
            string text = File.ReadAllText(path);
            if (text.Length > 1_000_000) return null;
            return JsonNode.Parse(text) as JsonObject;
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    public static int CategoryMax(string? privateDir)
    {
        var doc = Read(privateDir);
        if (doc == null) return DefaultCategoryMax;
        try
        {
            int? n = J.Int(doc, "category_max");
            if (n == null || n < 0) return DefaultCategoryMax;
            return n > 100000 ? 100000 : n.Value;
        }
        catch (BadRequestException)
        {
            return DefaultCategoryMax;
        }
    }

    public static HashSet<string> RevealColumns(string? privateDir)
    {
        var set = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var doc = Read(privateDir);
        if (doc == null) return set;
        try
        {
            var list = J.StrList(doc, "reveal_columns");
            if (list != null)
                foreach (var item in list) set.Add(item);
        }
        catch (BadRequestException)
        {
        }
        return set;
    }
}

/// <summary>Read side of index.json (used only for internal decisions).</summary>
public static class IndexFile
{
    public static JsonObject? TryLoad(string privateDir)
    {
        try
        {
            string p = Path.Combine(privateDir, "index.json");
            if (!File.Exists(p)) return null;
            return JsonNode.Parse(File.ReadAllText(p, Encoding.UTF8)) as JsonObject;
        }
        catch
        {
            return null;
        }
    }
}
