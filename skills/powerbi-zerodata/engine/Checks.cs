using System.Globalization;
using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>
/// check: data-blind assertions. Values are read and compared inside the engine; the response carries only
/// PASS/FAIL/EMPTY/UNSUPPORTED/ERROR, mismatch classes, coarse buckets (shape tier), and group keys that are
/// revealed only for an allowlisted date, indexed category, or policy reveal column. Every other key is HMAC'd.
/// </summary>
public static class Checks
{
    const double DefaultTol = 1e-9;
    const int MaxGroups = 10;

    static readonly HashSet<string> Kinds = new(StringComparer.Ordinal)
    {
        "executes", "nonblank", "nonempty", "equal", "additive", "unique", "no_orphans", "no_blank_keys", "sign", "subset", "rls",
    };

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        var checks = req.Arr("checks") ?? throw new BadRequestException("checks is required");
        var reveal = new HashSet<string>(req.StrList("reveal") ?? new List<string>(), StringComparer.Ordinal);
        var allow = PolicyFile.RevealColumns(ctx.RequirePrivateDir());
        var vault = Vault.Open(ctx.RequirePrivateDir());
        string privateDir = ctx.RequirePrivateDir();
        using var conn = Conn.Open(ctx, port);
        var results = new JsonArray();
        foreach (var node in checks)
        {
            if (node is not JsonObject chk) throw new BadRequestException("checks must be objects");
            string id = J.Id(chk);
            results.Add(RunOne(id, chk, conn, vault, reveal, allow, privateDir, ctx));
        }
        return Ctx.Ok("check", new JsonObject { ["checks"] = results });
    }

    static JsonObject RunOne(string id, JsonObject chk, Conn conn, Vault vault, HashSet<string> reveal,
        HashSet<string> allow, string privateDir, Ctx ctx)
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
                    Equal(chk, where, res, conn, vault, reveal, allow, privateDir, ctx);
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
                case "rls":
                    Rls(chk, res, conn, ctx);
                    break;
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

    readonly record struct RlsTarget(string Text, bool FullQuery);

    /// <summary>
    /// Row counts are compared here and never returned. The role, the query and the user are not returned.
    /// An expected access error for OLS_INACCESSIBLE is handled in this method so the outer catch does not
    /// turn it into a generic ERROR.
    /// </summary>
    static void Rls(JsonObject chk, JsonObject res, Conn conn, Ctx ctx)
    {
        string expect = J.ReqStr(chk, "expect");
        if (expect is not ("EMPTY" or "NON_EMPTY" or "LESS" or "EQUAL" or "OLS_INACCESSIBLE"))
            throw new BadRequestException("expect is not an rls verdict");
        string? role = BlankToNull(J.Str(chk, "role"));
        string? user = BlankToNull(J.Str(chk, "user"));
        if (role == null && user == null) throw new BadRequestException("rls needs a role or a user");
        Conn.RejectIdentity(role, user);
        var target = RlsTargetOf(chk, conn);

        if (expect == "OLS_INACCESSIBLE")
        {
            RunCount(conn, target);
            Conn restricted;
            try
            {
                restricted = Conn.Open(ctx, conn.Port, role, user);
            }
            catch (Exception ex)
            {
                var openClass = Errors.Classify(ex);
                throw new EngineException(
                    openClass is ErrorClass.TIMEOUT or ErrorClass.BAD_REQUEST ? openClass : ErrorClass.ENGINE_UNAVAILABLE,
                    "rls role connection failed");
            }
            try
            {
                using (restricted)
                {
                    RunCount(restricted, target);
                    res["status"] = "FAIL";
                }
            }
            catch (Exception ex) when (ex is not EngineException)
            {
                var cls = Errors.Classify(ex);
                if (cls != ErrorClass.UNKNOWN_OBJECT)
                    throw new EngineException(
                        cls is ErrorClass.TIMEOUT or ErrorClass.BAD_REQUEST or ErrorClass.ENGINE_UNAVAILABLE
                            ? cls : ErrorClass.OTHER,
                        "rls role query failed");
                ctx.LogText("rls role query failed");
                res["status"] = "PASS";
            }
            return;
        }

        long openRows = RunCount(conn, target);
        long roleRows;
        try
        {
            using var restricted = Conn.Open(ctx, conn.Port, role, user);
            roleRows = RunCount(restricted, target);
        }
        catch (Exception ex)
        {
            var cls = Errors.Classify(ex);
            if (cls is ErrorClass.TIMEOUT or ErrorClass.BAD_REQUEST) throw;
            throw new EngineException(cls, "rls role query failed");
        }
        bool pass = expect switch
        {
            "EMPTY" => roleRows == 0,
            "NON_EMPTY" => roleRows > 0,
            "LESS" => roleRows < openRows,
            "EQUAL" => roleRows == openRows,
            _ => false,
        };
        res["status"] = pass ? "PASS" : "FAIL";
    }

    static string? BlankToNull(string? s) => string.IsNullOrWhiteSpace(s) ? null : s;

    static RlsTarget RlsTargetOf(JsonObject chk, Conn conn)
    {
        string? query = J.Str(chk, "query");
        if (!string.IsNullOrWhiteSpace(query))
        {
            string? first = Dax.BareWords(query).FirstOrDefault();
            bool full = first != null && (first.Equals("EVALUATE", StringComparison.OrdinalIgnoreCase)
                || first.Equals("DEFINE", StringComparison.OrdinalIgnoreCase));
            if (full) Dax.GuardQuery(query, "query");
            else Dax.Guard(query, "query");
            return new RlsTarget(query, full);
        }
        string? column = J.Str(chk, "column");
        if (!string.IsNullOrWhiteSpace(column))
        {
            var c = conn.Meta.ResolveColumn(column);
            return new RlsTarget("VALUES(" + c.Ref + ")", false);
        }
        string? table = J.Str(chk, "table");
        if (!string.IsNullOrWhiteSpace(table))
        {
            var t = conn.Meta.FindTable(table) ?? throw new EngineException(ErrorClass.UNKNOWN_OBJECT, "rls table not found");
            return new RlsTarget(Dax.QTable(t.Name), false);
        }
        throw new BadRequestException("rls needs a query, column or table");
    }

    static long RunCount(Conn conn, RlsTarget target)
    {
        if (target.FullQuery) return conn.CountRows(target.Text);
        return ColumnStats.L(conn.Row1("EVALUATE ROW(\"n\", COUNTROWS(" + target.Text + "))")[0]);
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

    static void Equal(JsonObject chk, List<string> where, JsonObject res, Conn conn, Vault vault, HashSet<string> reveal,
        HashSet<string> allow, string privateDir, Ctx ctx)
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
            if (!ctx.Strict) res["mismatch"] = Buckets.Mismatch(0);
            return;
        }
        var bad = rows.Where(r => !ValuesEqual(r[cols.Count], r[cols.Count + 1], tol)).ToList();
        res["status"] = bad.Count == 0 ? "PASS" : "FAIL";
        if (!ctx.Strict) res["mismatch"] = Buckets.Mismatch(bad.Count);
        if (!ctx.Strict && bad.Count > 0) res["groups"] = Groups(bad, by, cols, vault, reveal, allow, privateDir);
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

    static bool IndexedCategory(string privateDir, ColumnMeta col)
    {
        if (IndexFile.TryLoad(privateDir)?["columns"] is not JsonObject cols) return false;
        if (cols[col.Key] is not JsonObject entry) return false;
        string? cls = J.Str(entry, "class");
        return cls == "category";
    }

    static bool PolicyAllows(HashSet<string> allow, string by, ColumnMeta col)
    {
        if (allow.Count == 0) return false;
        if (allow.Contains(by) || allow.Contains(col.Key)) return true;
        string quoted = "'" + col.Table.Name.Replace("'", "''") + "'[" + col.Name.Replace("]", "]]") + "]";
        return allow.Contains(quoted);
    }

    /// <summary>
    /// Raw keys only when the exact by string is on the request list and the column is a date,
    /// an indexed category, or a policy reveal column. A direct caller cannot widen that set.
    /// </summary>
    static JsonArray Groups(List<object?[]> bad, List<string> by, List<ColumnMeta> cols, Vault vault, HashSet<string> reveal,
        HashSet<string> allow, string privateDir)
    {
        var groups = new List<JsonObject>();
        foreach (var r in bad)
        {
            var g = new JsonObject();
            for (int k = 0; k < by.Count; k++)
            {
                object? v = r[k];
                JsonObject key;
                bool listed = reveal.Contains(by[k]);
                bool permitted = cols[k].IsDate || IndexedCategory(privateDir, cols[k]) || PolicyAllows(allow, by[k], cols[k]);
                if (v == null) key = new JsonObject { ["blank"] = true };
                else if (listed && permitted) key = new JsonObject { ["reveal"] = Reveal(v) };
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
