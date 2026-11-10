using System.Text;
using System.Text.RegularExpressions;

namespace PbzEngine;

/// <summary>DAX quoting, column-reference parsing and the expression guard.</summary>
public static class Dax
{
    public static string QTable(string table) => "'" + table.Replace("'", "''") + "'";

    public static string QCol(string table, string column) => QTable(table) + "[" + column.Replace("]", "]]") + "]";

    public static string QStr(string s) => "\"" + s.Replace("\"", "\"\"") + "\"";

    static readonly Regex ColRefRx = new(
        @"^\s*(?:'(?<qt>(?:[^']|'')+)'|(?<t>[^\[\]']*?))\s*\[(?<c>(?:[^\]]|\]\])+)\]\s*$",
        RegexOptions.Compiled);

    /// <summary>Parses "'Table'[Column]" or "Table[Column]". Returns null when the text is not a column reference.</summary>
    public static (string Table, string Column)? ParseColRef(string text)
    {
        var m = ColRefRx.Match(text);
        if (!m.Success) return null;
        string table = m.Groups["qt"].Success ? m.Groups["qt"].Value.Replace("''", "'") : m.Groups["t"].Value.Trim();
        string column = m.Groups["c"].Value.Replace("]]", "]");
        if (table.Length == 0 || column.Length == 0) return null;
        return (table, column);
    }

    /// <summary>
    /// Expressions are spliced into engine-built queries, so they may not contain the query-level keywords
    /// EVALUATE or DEFINE outside strings, quoted names and comments (that would let a caller run arbitrary
    /// statements inside a bind-only check).
    /// </summary>
    public static void Guard(string expr, string field)
    {
        RequireClosed(expr, field);
        foreach (var word in BareWords(expr))
        {
            if (word.Equals("EVALUATE", StringComparison.OrdinalIgnoreCase) || word.Equals("DEFINE", StringComparison.OrdinalIgnoreCase))
                throw new BadRequestException(field + " contains a query keyword");
            GuardWord(word, field);
        }
    }

    /// <summary>
    /// Caller-written queries (snapshot, smoke) must be DAX: the first token is DEFINE or EVALUATE, so the text can
    /// never be TMSL, MDX or a $SYSTEM DMV statement. INFO.* functions (DMV equivalents, including storage DMVs that
    /// expose counts) and EVALUATEANDLOG are refused everywhere.
    /// </summary>
    public static void GuardQuery(string query, string field)
    {
        RequireClosed(query, field);
        string? first = BareWords(query).FirstOrDefault();
        if (first == null || !(first.Equals("EVALUATE", StringComparison.OrdinalIgnoreCase) || first.Equals("DEFINE", StringComparison.OrdinalIgnoreCase)))
            throw new BadRequestException(field + " must be a DAX query (DEFINE or EVALUATE first)");
        int i = 0;
        while (i < query.Length && (char.IsWhiteSpace(query[i]) || query[i] == '﻿')) i++;
        if (i < query.Length && (query[i] == '{' || query[i] == '<' || query[i] == '$'))
            throw new BadRequestException(field + " must be a DAX query");
        foreach (var word in BareWords(query)) GuardWord(word, field);
    }

    static void GuardWord(string word, string field)
    {
        if (word.StartsWith("INFO.", StringComparison.OrdinalIgnoreCase) || word.Equals("EVALUATEANDLOG", StringComparison.OrdinalIgnoreCase))
            throw new BadRequestException(field + " uses a function that is not allowed");
    }

    /// <summary>Bare identifiers/keywords of a DAX text (skips "strings", 'names', [names] and comments).</summary>
    public static IEnumerable<string> BareWords(string s)
    {
        int i = 0;
        while (i < s.Length)
        {
            char c = s[i];
            if (c == '"') { i = EndOrClose(SkipDelimited(s, i, '"', '"'), s.Length); continue; }
            if (c == '\'') { i = EndOrClose(SkipDelimited(s, i, '\'', '\''), s.Length); continue; }
            if (c == '[') { i = EndOrClose(SkipDelimited(s, i, '[', ']'), s.Length); continue; }
            if (c == '/' && i + 1 < s.Length && s[i + 1] == '/') { i = SkipLine(s, i); continue; }
            if (c == '-' && i + 1 < s.Length && s[i + 1] == '-') { i = SkipLine(s, i); continue; }
            if (c == '/' && i + 1 < s.Length && s[i + 1] == '*')
            {
                int end = s.IndexOf("*/", i + 2, StringComparison.Ordinal);
                i = end < 0 ? s.Length : end + 2;
                continue;
            }
            if (char.IsLetter(c) || c == '_')
            {
                int j = i;
                while (j < s.Length && (char.IsLetterOrDigit(s[j]) || s[j] == '_' || s[j] == '.')) j++;
                yield return s.Substring(i, j - i);
                i = j;
                continue;
            }
            i++;
        }
    }

    static int SkipDelimited(string s, int start, char open, char close)
    {
        int j = start + 1;
        while (j < s.Length)
        {
            if (s[j] == close)
            {
                if (j + 1 < s.Length && s[j + 1] == close) { j += 2; continue; }
                return j + 1;
            }
            j++;
        }
        return -1;
    }

    static int EndOrClose(int end, int length) => end < 0 ? length : end;

    public static void RequireClosed(string s, string field)
    {
        int i = 0;
        while (i < s.Length)
        {
            char c = s[i];
            if (c == '"' || c == '\'')
            {
                int j = SkipDelimited(s, i, c, c);
                if (j < 0) throw new BadRequestException(field + " contains an unclosed delimiter");
                i = j;
                continue;
            }
            if (c == '[')
            {
                int j = SkipDelimited(s, i, '[', ']');
                if (j < 0) throw new BadRequestException(field + " contains an unclosed delimiter");
                i = j;
                continue;
            }
            if (c == '/' && i + 1 < s.Length && s[i + 1] == '*')
            {
                int end = s.IndexOf("*/", i + 2, StringComparison.Ordinal);
                if (end < 0) throw new BadRequestException(field + " contains an unclosed delimiter");
                i = end + 2;
                continue;
            }
            if ((c == '/' || c == '-') && i + 1 < s.Length && s[i + 1] == c)
            {
                i = SkipLine(s, i);
                continue;
            }
            i++;
        }
    }

    static int SkipLine(string s, int start)
    {
        int i = start;
        while (i < s.Length && s[i] != '\n' && s[i] != '\r') i++;
        if (i < s.Length && s[i] == '\r') i++;
        if (i < s.Length && s[i] == '\n') i++;
        return i;
    }

    /// <summary>Wraps a scalar expression with CALCULATE over the 'where' predicates.</summary>
    public static string Calc(string expr, IReadOnlyList<string> where)
        => where.Count == 0 ? expr : "CALCULATE(" + expr + ", " + string.Join(", ", where) + ")";

    /// <summary>Wraps a table expression with CALCULATETABLE over the 'where' predicates.</summary>
    public static string CalcTable(string tableExpr, IReadOnlyList<string> where)
        => where.Count == 0 ? tableExpr : "CALCULATETABLE(" + tableExpr + ", " + string.Join(", ", where) + ")";

    /// <summary>Normalizes a result-set column name ("'T'[C]" -> "T[C]", "[m]" -> "m").</summary>
    public static string ResultName(string raw)
    {
        string s = raw.Trim();
        var cr = ParseColRef(s);
        if (cr != null) return cr.Value.Table + "[" + cr.Value.Column + "]";
        if (s.StartsWith('[') && s.EndsWith(']') && s.Length > 2) return s.Substring(1, s.Length - 2).Replace("]]", "]");
        return s;
    }

    public static string Cap(string s, int max = 128)
    {
        var sb = new StringBuilder();
        foreach (char ch in s)
        {
            if (char.IsControl(ch)) continue;
            sb.Append(ch);
            if (sb.Length >= max) break;
        }
        return sb.ToString();
    }
}
