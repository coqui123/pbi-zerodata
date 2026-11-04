using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.AnalysisServices.Tabular;
using Microsoft.AnalysisServices.Tabular.Tmdl;

namespace PbzEngine;

/// <summary>
/// tmdl-validate: TmdlSerializer.DeserializeDatabaseFromFolder plus a ref/file inventory. No connection.
/// Never echoes line text; messages are rebuilt without the Document/Line parts and scrubbed.
/// </summary>
public static class TmdlValidate
{
    static readonly Dictionary<string, string> RefDirs = new(StringComparer.Ordinal)
    {
        ["table"] = "tables",
        ["role"] = "roles",
        ["perspective"] = "perspectives",
        ["cultureInfo"] = "cultures",
    };

    static readonly Regex RefRx = new(@"^ref\s+(?<kind>[A-Za-z]+)\s+(?<name>.+?)\s*$", RegexOptions.Compiled);
    static readonly Regex DeclRx = new(
        @"^\s*(?:table|column|measure|hierarchy|level|partition|role|perspective|cultureInfo|expression|calculationItem|tablePermission)\s+(?<name>'(?:[^']|'')+'|[^\s=]+)",
        RegexOptions.Compiled);

    public static JsonObject Run(Req req, Ctx ctx)
    {
        string folder = J.ReqStr(req.Obj, "folder");
        if (!Path.IsPathFullyQualified(folder)) throw new BadRequestException("folder must be an absolute path");
        folder = Path.GetFullPath(folder);
        if (!Directory.Exists(folder)) throw new BadRequestException("folder not found");

        var scrub = new Scrubber(CollectNames(folder));
        var errors = new JsonArray();
        try
        {
            ctx.Guard(() => TmdlSerializer.DeserializeDatabaseFromFolder(folder));
        }
        catch (TmdlFormatException fe)
        {
            ctx.Log(fe, "tmdl");
            errors.Add(Entry(fe.HasDocumentSourceInfo ? fe.Document : null, fe.HasDocumentSourceInfo ? fe.Line : 0,
                ErrorClass.SYNTAX, scrub.Scrub(CleanMessage(fe.Message, fe.HasDocumentSourceInfo ? fe.LineText : null))));
        }
        catch (TmdlDeserializationWithReferenceErrorsException re)
        {
            ctx.Log(re, "tmdl");
            int n = 0;
            foreach (var e in re.ResolutionErrors)
            {
                if (++n > 50) break;
                errors.Add(Entry(FileOfObjectPath(e.SourceObject?.ToString()), 0, ErrorClass.UNKNOWN_OBJECT, scrub.Scrub(e.Error)));
            }
            if (n == 0) errors.Add(Entry(null, 0, ErrorClass.UNKNOWN_OBJECT, scrub.Scrub(CleanMessage(re.Message, null))));
        }
        catch (TmdlSerializationException se)
        {
            ctx.Log(se, "tmdl");
            var cls = Errors.ClassifyMessage(se.Message);
            if (cls == ErrorClass.OTHER) cls = ErrorClass.SYNTAX;
            errors.Add(Entry(se.HasDocumentSourceInfo ? se.Document : null, se.HasDocumentSourceInfo ? se.Line : 0, cls,
                scrub.Scrub(CleanMessage(se.Message, null))));
        }
        catch (Exception ex) when (ex is not TimeoutException)
        {
            ctx.Log(ex, "tmdl");
            errors.Add(Entry(null, 0, Errors.Classify(ex), scrub.Scrub(CleanMessage(ex.Message, null))));
        }

        var (missing, orphans) = Inventory(folder);
        var missingArr = new JsonArray();
        foreach (var m in missing) missingArr.Add(m);
        var orphanArr = new JsonArray();
        foreach (var o in orphans) orphanArr.Add(o);
        return Ctx.Ok("tmdl-validate", new JsonObject
        {
            ["valid"] = errors.Count == 0 && missing.Count == 0,
            ["errors"] = errors,
            ["missing_refs"] = missingArr,
            ["orphan_files"] = orphanArr,
        });
    }

    static JsonObject Entry(string? document, int line, ErrorClass cls, string message)
    {
        var o = new JsonObject();
        o["file"] = DocToFile(document);
        if (line > 0) o["line"] = line;
        o["class"] = cls.ToString();
        o["message"] = message;
        return o;
    }

    /// <summary>"./tables/Person" -> "tables/Person.tmdl".</summary>
    static string DocToFile(string? doc)
    {
        if (string.IsNullOrWhiteSpace(doc)) return "";
        string d = doc.Replace('\\', '/').Trim();
        while (d.StartsWith("./", StringComparison.Ordinal)) d = d.Substring(2);
        d = d.TrimStart('/');
        if (!d.EndsWith(".tmdl", StringComparison.OrdinalIgnoreCase)) d += ".tmdl";
        return Dax.Cap(d, 260);
    }

    /// <summary>"{ Relationship: 'x' }" -> relationships.tmdl; "{ Table: 'T', ... }" -> tables/T.tmdl.</summary>
    static string FileOfObjectPath(string? path)
    {
        if (string.IsNullOrEmpty(path)) return "";
        var m = Regex.Match(path, @"Table:\s*'((?:[^']|'')*)'");
        if (m.Success) return "tables/" + EncodeName(m.Groups[1].Value.Replace("''", "'")) + ".tmdl";
        if (path.Contains("Relationship", StringComparison.Ordinal)) return "relationships.tmdl";
        m = Regex.Match(path, @"Role:\s*'((?:[^']|'')*)'");
        if (m.Success) return "roles/" + EncodeName(m.Groups[1].Value.Replace("''", "'")) + ".tmdl";
        m = Regex.Match(path, @"Perspective:\s*'((?:[^']|'')*)'");
        if (m.Success) return "perspectives/" + EncodeName(m.Groups[1].Value.Replace("''", "'")) + ".tmdl";
        if (path.Contains("Expression", StringComparison.Ordinal)) return "expressions.tmdl";
        if (path.Contains("DataSource", StringComparison.Ordinal)) return "dataSources.tmdl";
        return "model.tmdl";
    }

    /// <summary>Drops the Document / Line Number / Line parts (the line text may hold literals).</summary>
    static string CleanMessage(string message, string? lineText)
    {
        var kept = new List<string>();
        foreach (var raw in message.Split('\n'))
        {
            string l = raw.Trim();
            if (l.StartsWith("Document -", StringComparison.OrdinalIgnoreCase)) continue;
            if (l.StartsWith("Line Number -", StringComparison.OrdinalIgnoreCase)) continue;
            if (l.StartsWith("Line -", StringComparison.OrdinalIgnoreCase)) continue;
            kept.Add(l);
        }
        string s = string.Join(" ", kept);
        if (!string.IsNullOrEmpty(lineText)) s = s.Replace(lineText, " ");
        return s;
    }

    /// <summary>Object names declared in the TMDL (role members excluded: they are data).</summary>
    static IEnumerable<string> CollectNames(string folder)
    {
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var f in Directory.EnumerateFiles(folder, "*.tmdl", SearchOption.AllDirectories))
        {
            foreach (var line in File.ReadLines(f))
            {
                var m = DeclRx.Match(line);
                if (m.Success) names.Add(Unquote(m.Groups["name"].Value));
            }
        }
        return names;
    }

    static string Unquote(string n)
    {
        n = n.Trim();
        if (n.Length >= 2 && n[0] == '\'' && n[^1] == '\'') return n.Substring(1, n.Length - 2).Replace("''", "'");
        return n;
    }

    static (List<string> Missing, List<string> Orphans) Inventory(string folder)
    {
        var refs = new Dictionary<string, HashSet<string>>(StringComparer.Ordinal);
        foreach (var k in RefDirs.Keys) refs[k] = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var missing = new List<string>();
        foreach (var top in new[] { "model.tmdl", "database.tmdl" })
        {
            string p = Path.Combine(folder, top);
            if (!File.Exists(p)) continue;
            foreach (var line in File.ReadLines(p))
            {
                var m = RefRx.Match(line);
                if (!m.Success) continue;
                string kind = m.Groups["kind"].Value;
                string name = Unquote(m.Groups["name"].Value);
                if (kind == "model")
                {
                    if (!File.Exists(Path.Combine(folder, "model.tmdl"))) missing.Add("model.tmdl");
                    continue;
                }
                if (!RefDirs.TryGetValue(kind, out var dir)) continue;
                refs[kind].Add(name);
                if (FindFile(folder, dir, name) == null) missing.Add(dir + "/" + EncodeName(name) + ".tmdl");
            }
        }
        var orphans = new List<string>();
        foreach (var (kind, dir) in RefDirs)
        {
            string d = Path.Combine(folder, dir);
            if (!Directory.Exists(d)) continue;
            foreach (var f in Directory.EnumerateFiles(d, "*.tmdl").OrderBy(x => x, StringComparer.Ordinal))
            {
                string objName = DecodeName(Path.GetFileNameWithoutExtension(f));
                if (!refs[kind].Contains(objName)) orphans.Add(dir + "/" + Path.GetFileName(f));
            }
        }
        return (missing.Distinct().ToList(), orphans);
    }

    static string? FindFile(string folder, string dir, string name)
    {
        string d = Path.Combine(folder, dir);
        if (!Directory.Exists(d)) return null;
        foreach (var f in Directory.EnumerateFiles(d, "*.tmdl"))
            if (string.Equals(DecodeName(Path.GetFileNameWithoutExtension(f)), name, StringComparison.OrdinalIgnoreCase)) return f;
        return null;
    }

    const string Reserved = "\\/:*?\"<>|%";

    /// <summary>TMDL file naming: characters invalid in file names (and '%') become %XX.</summary>
    public static string EncodeName(string name)
    {
        var sb = new StringBuilder();
        foreach (char c in name)
        {
            if (c < 0x20 || Reserved.IndexOf(c) >= 0) sb.Append('%').Append(((int)c).ToString("X2", CultureInfo.InvariantCulture));
            else sb.Append(c);
        }
        return sb.ToString();
    }

    public static string DecodeName(string fileName)
    {
        var sb = new StringBuilder();
        for (int i = 0; i < fileName.Length; i++)
        {
            if (fileName[i] == '%' && i + 2 < fileName.Length &&
                int.TryParse(fileName.AsSpan(i + 1, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out int code))
            {
                sb.Append((char)code);
                i += 2;
            }
            else sb.Append(fileName[i]);
        }
        return sb.ToString();
    }
}
