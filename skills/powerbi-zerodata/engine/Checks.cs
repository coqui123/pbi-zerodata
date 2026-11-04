using System.Globalization;
using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>
/// check: data-blind assertions. Values are read and compared inside the engine; the response carries only
/// PASS/FAIL/EMPTY/UNSUPPORTED/ERROR, mismatch classes, coarse buckets (shape tier), and group keys that are
/// revealed (only when the exact `by` string is listed in `reveal`) or HMAC'd.
/// </summary>
public static class Checks
{
    const double DefaultTol = 1e-9;
    const int MaxGroups = 10;

    static readonly HashSet<string> Kinds = new(StringComparer.Ordinal)
    {
        "executes", "nonblank", "nonempty", "equal", "additive", "unique", "no_orphans", "no_blank_keys", "sign", "subset",
    };

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        var checks = req.Arr("checks") ?? throw new BadRequestException("checks is required");
        var reveal = new HashSet<string>(req.StrList("reveal") ?? new List<string>(), StringComparer.Ordinal);
        var vault = Vault.Open(ctx.RequirePrivateDir());
        using var conn = Conn.Open(ctx, port);
        var results = new JsonArray();
        foreach (var node in checks)
        {
            if (node is not JsonObject chk) throw new BadRequestException("checks must be objects");
            string id = J.Id(chk);
            results.Add(RunOne(id, chk, conn, vault, reveal, ctx));
        }
        return Ctx.Ok("check", new JsonObject { ["checks"] = results });
    }

    static JsonObject RunOne(string id, JsonObject chk, Conn conn, Vault vault, HashSet<string> reveal, Ctx ctx)
    {
        var res = new JsonObject { ["id"] = id };
        try
        {
            string? kind = J.Str(chk, "kind");
            if (kind == null || !Kinds.Contains(kind))
            {
                res["status"] = "UNSUPPORTED";
                return res;
            }
            var where = Where(chk);
            switch (kind)
            {
                case "executes":
                {
                    conn.Row1("EVALUATE ROW(\"v\", " + Dax.Calc(Expr(chk, "expr"), where) + ")");
                    res["status"] = "PASS";
                    break;
                }
                case "nonblank":
                {
                    var row = conn.Row1("EVALUATE ROW(\"v\", NOT ISBLANK(" + Dax.Calc(Expr(chk, "expr"), where) + "))");
                    res["status"] = row[0] is true ? "PASS" : "FAIL";
                    break;
                }
                case "nonempty":
                {
                    var row = conn.Row1("EVALUATE ROW(\"v\", NOT ISEMPTY(" + Dax.CalcTable(Expr(chk, "expr"), where) + "))");
                    res["status"] = row[0] is true ? "PASS" : "FAIL";
                    break;
                }
                case "equal":
                    Equal(chk, where, res, conn, vault, reveal, ctx);
                    break;
                case "additive":
                    Additive(chk, where, res, conn);
                    break;
                case "unique":
                {
                    var c = Col(chk, "column", conn);
                    string t = Dax.QTable(c.Table.Name);
                    var row = conn.Row1("EVALUATE ROW(\"n\", " + Dax.Calc("COUNTROWS(" + t + ")", where) +
                                        ", \"o\", " + Dax.Calc("COUNTROWS(FILTER(DISTINCT(" + c.Ref + "), CALCULATE(COUNTROWS(" + t + ")) > 1))", where) + ")");
                    Offenders(res, ColumnStats.L(row[0]), ColumnStats.L(row[1]), ctx);
                    break;
                }
                case "no_orphans":
                case "subset":
                {
                    var a = Col(chk, kind == "subset" ? "column" : "from", conn);
                    var b = Col(chk, kind == "subset" ? "of" : "to", conn);
                    string fa = "FILTER(DISTINCT(" + a.Ref + "), " + a.NonBlankPredicate + ")";
                    var row = conn.Row1("EVALUATE ROW(\"n\", " + Dax.Calc("COUNTROWS(" + fa + ")", where) +
                                        ", \"o\", " + Dax.Calc("COUNTROWS(EXCEPT(" + fa + ", DISTINCT(" + b.Ref + ")))", where) + ")");
                    Offenders(res, ColumnStats.L(row[0]), ColumnStats.L(row[1]), ctx);
                    break;
                }
                case "no_blank_keys":
                {
                    var c = Col(chk, "column", conn);
                    string t = Dax.QTable(c.Table.Name);
                    var row = conn.Row1("EVALUATE ROW(\"n\", " + Dax.Calc("COUNTROWS(" + t + ")", where) +
                                        ", \"o\", " + Dax.Calc("CALCULATE(COUNTROWS(" + t + "), " + c.BlankPredicate + ")", where) + ")");
                    Offenders(res, ColumnStats.L(row[0]), ColumnStats.L(row[1]), ctx);
                    break;
                }
                case "sign":
                {
                    var v = conn.Row1("EVALUATE ROW(\"v\", " + Dax.Calc(Expr(chk, "expr"), where) + ")")[0];
                    string sign;
                    if (v == null) sign = "BLANK";
                    else if (!Norm.IsNumeric(v)) throw new EngineException(ErrorClass.TYPE_MISMATCH, "sign needs a numeric value");
                    else
                    {
                        double d = Norm.ToDouble(v);
                        if (double.IsNaN(d)) throw new EngineException(ErrorClass.OTHER, "sign of NaN");
                        sign = d < 0 ? "NEG" : d == 0 ? "ZERO" : "POS";
                    }
                    var expect = J.StrList(chk, "expect");
                    if (expect != null && expect.Any(e => e is not ("NEG" or "ZERO" or "POS" or "BLANK")))
                        throw new BadRequestException("expect must be NEG, ZERO, POS or BLANK");
                    res["status"] = expect == null || expect.Count == 0 || expect.Contains(sign) ? "PASS" : "FAIL";
                    res["sign"] = sign;
                    break;
                }
            }
        }
        catch (Exception ex)
        {
            var cls = Errors.Classify(ex);
            res.Clear();
            res["id"] = id;
            res["status"] = "ERROR";
            res["class"] = cls.ToString();
            var logId = ctx.Log(ex, id);
            if (logId != null) res["log_id"] = logId;
            if (cls == ErrorClass.TIMEOUT)
            {
                try { conn.Reset(); } catch { }
            }
        }
        return res;
    }

    static void Offenders(JsonObject res, long rows, long offenders, Ctx ctx)
    {
        if (rows <= 0)
        {
            res["status"] = "EMPTY";
            return;
        }
        res["status"] = offenders == 0 ? "PASS" : "FAIL";
        if (!ctx.Strict) res["offenders_bucket"] = Buckets.Count(offenders);
    }

    static void Equal(JsonObject chk, List<string> where, JsonObject res, Conn conn, Vault vault, HashSet<string> reveal, Ctx ctx)
    {
        string left = Expr(chk, "left");
        string right = Expr(chk, "right");
        double tol = Tol(chk);
        var by = J.StrList(chk, "by") ?? new List<string>();
        if (by.Count == 0)
        {
            var row = conn.Row1("EVALUATE ROW(\"l\", " + Dax.Calc(left, where) + ", \"r\", " + Dax.Calc(right, where) + ")");
            res["status"] = ValuesEqual(row[0], row[1], tol) ? "PASS" : "FAIL";
            return;
        }
        var cols = by.Select(b => conn.Meta.ResolveColumn(b)).ToList();
        string q = "EVALUATE " + Dax.CalcTable(
            "SUMMARIZECOLUMNS(" + string.Join(", ", cols.Select(c => c.Ref)) + ", \"__pbz_l\", " + left + ", \"__pbz_r\", " + right + ")",
            where);
        var rows = conn.Rows(q, int.MaxValue);
        if (rows.Count == 0)
        {
            res["status"] = "EMPTY";
            res["mismatch"] = Buckets.Mismatch(0);
            return;
        }
        var bad = rows.Where(r => !ValuesEqual(r[cols.Count], r[cols.Count + 1], tol)).ToList();
        res["status"] = bad.Count == 0 ? "PASS" : "FAIL";
        res["mismatch"] = Buckets.Mismatch(bad.Count);
        if (!ctx.Strict && bad.Count > 0) res["groups"] = Groups(bad, by, cols, vault, reveal);
    }

    static void Additive(JsonObject chk, List<string> where, JsonObject res, Conn conn)
    {
        string expr = Expr(chk, "expr");
        double tol = Tol(chk);
        var by = J.StrList(chk, "by") ?? new List<string>();
        if (by.Count == 0) throw new BadRequestException("additive needs by");
        var cols = by.Select(b => conn.Meta.ResolveColumn(b)).ToList();
        var total = conn.Row1("EVALUATE ROW(\"t\", " + Dax.Calc(expr, where) + ")")[0];
        var rows = conn.Rows("EVALUATE " + Dax.CalcTable(
            "SUMMARIZECOLUMNS(" + string.Join(", ", cols.Select(c => c.Ref)) + ", \"__pbz_v\", " + expr + ")", where), int.MaxValue);
        double sum = 0;
        bool any = false;
        foreach (var r in rows)
        {
            var v = r[cols.Count];
            if (v == null) continue;
            if (!Norm.IsNumeric(v)) throw new EngineException(ErrorClass.TYPE_MISMATCH, "additive needs numeric values");
            sum += Norm.ToDouble(v);
            any = true;
        }
        if (total == null && !any)
        {
            res["status"] = "EMPTY";
            return;
        }
        if (total != null && !Norm.IsNumeric(total)) throw new EngineException(ErrorClass.TYPE_MISMATCH, "additive needs a numeric total");
        double t = total == null ? 0 : Norm.ToDouble(total);
        res["status"] = NumEqual(t, sum, tol) ? "PASS" : "FAIL";
    }

    static JsonArray Groups(List<object?[]> bad, List<string> by, List<ColumnMeta> cols, Vault vault, HashSet<string> reveal)
    {
        var groups = new List<JsonObject>();
        foreach (var r in bad)
        {
            var g = new JsonObject();
            for (int k = 0; k < by.Count; k++)
            {
                object? v = r[k];
                JsonObject key;
                if (v == null) key = new JsonObject { ["blank"] = true };
                else if (reveal.Contains(by[k])) key = new JsonObject { ["reveal"] = Reveal(v) };
                else key = new JsonObject { ["hmac"] = vault.Hmac(v) };
                g[by[k]] = key;
            }
            groups.Add(g);
        }
        var arr = new JsonArray();
        foreach (var g in groups.OrderBy(g => g.ToJsonString(), StringComparer.Ordinal).Take(MaxGroups)) arr.Add(g);
        return arr;
    }

    static string Reveal(object v) => v switch
    {
        string s => Dax.Cap(s, 256),
        DateTime d => d.TimeOfDay == TimeSpan.Zero
            ? d.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture)
            : d.ToString("yyyy-MM-dd'T'HH:mm:ss", CultureInfo.InvariantCulture),
        bool b => b ? "true" : "false",
        _ when Norm.IsNumeric(v) => FormatNumber(Norm.ToDouble(v)),
        _ => Dax.Cap(Convert.ToString(v, CultureInfo.InvariantCulture) ?? "", 256),
    };

    static string FormatNumber(double d)
    {
        if (Math.Abs(d) < 1e15 && d == Math.Floor(d)) return ((long)d).ToString(CultureInfo.InvariantCulture);
        return d.ToString("R", CultureInfo.InvariantCulture);
    }

    /// <summary>BLANK equals BLANK only; numbers within relative tolerance; others by value.</summary>
    public static bool ValuesEqual(object? a, object? b, double tol)
    {
        if (a == null || b == null) return a == null && b == null;
        if (Norm.IsNumeric(a) && Norm.IsNumeric(b)) return NumEqual(Norm.ToDouble(a), Norm.ToDouble(b), tol);
        if (a is DateTime da && b is DateTime db) return da == db;
        if (a is string sa && b is string sb) return string.Equals(sa, sb, StringComparison.Ordinal);
        if (a is bool ba && b is bool bb) return ba == bb;
        return string.Equals(Norm.Value(a), Norm.Value(b), StringComparison.Ordinal);
    }

    static bool NumEqual(double a, double b, double tol)
    {
        if (a == b) return true;
        if (double.IsNaN(a) || double.IsNaN(b)) return false;
        return Math.Abs(a - b) <= tol * Math.Max(Math.Abs(a), Math.Abs(b));
    }

    static double Tol(JsonObject chk)
    {
        double tol = J.Num(chk, "tol") ?? DefaultTol;
        if (double.IsNaN(tol) || tol < 0 || tol > 1) throw new BadRequestException("tol must be between 0 and 1");
        return tol;
    }

    static string Expr(JsonObject chk, string field)
    {
        string e = J.ReqStr(chk, field);
        Dax.Guard(e, field);
        return e;
    }

    static List<string> Where(JsonObject chk)
    {
        var list = J.StrList(chk, "where") ?? new List<string>();
        foreach (var w in list)
        {
            if (string.IsNullOrWhiteSpace(w)) throw new BadRequestException("empty where item");
            Dax.Guard(w, "where");
        }
        return list;
    }

    static ColumnMeta Col(JsonObject chk, string field, Conn conn) => conn.Meta.ResolveColumn(J.ReqStr(chk, field));
}
