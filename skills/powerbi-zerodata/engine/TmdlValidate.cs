using System.Collections.Concurrent;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.AnalysisServices.Tabular;
using Microsoft.AnalysisServices.Tabular.Tmdl;

namespace PbzEngine;

/// <summary>
/// tmdl-validate: TmdlSerializer.DeserializeDatabaseFromFolder plus a ref/file inventory of a private snapshot copy. No connection.
/// Never echoes line text; messages are rebuilt without the Document/Line parts and scrubbed.
/// </summary>
public static class TmdlValidate
{
    internal static Action? BeforeInventoryForTests;
    internal static Action<string>? AfterCopyFileForTests;
    internal static Action<string>? AfterSnapshotCleanupForTests;

    static readonly ConcurrentDictionary<string, byte> ActiveSnapshots = new(StringComparer.OrdinalIgnoreCase);

    static readonly Dictionary<string, string> RefDirs = new(StringComparer.Ordinal)
    {
        ["table"] = "tables",
        ["role"] = "roles",
        ["perspective"] = "perspectives",
        ["cultureInfo"] = "cultures",
    };

    static readonly Regex RefRx = new(
        @"^ref\s+(?<kind>[A-Za-z]+)\s+(?<name>'(?:[^']|'')+'|[^\s]+)\s*$",
        RegexOptions.Compiled);
    static readonly Regex DeclRx = new(
        @"^\s*(?:table|column|measure|hierarchy|level|partition|role|perspective|cultureInfo|expression|calculationItem|tablePermission)\s+(?<name>'(?:[^']|'')+'|[^\s=]+)",
        RegexOptions.Compiled);

    public static JsonObject Run(Req req, Ctx ctx)
    {
        string folder = J.ReqStr(req.Obj, "folder");
        if (!Path.IsPathFullyQualified(folder)) throw new BadRequestException("folder must be an absolute path");
        folder = Path.GetFullPath(folder);
        LinkGuard.Refuse(folder);
        if (!Directory.Exists(folder)) throw new BadRequestException("folder not found");

        string privateDir = ctx.RequirePrivateDir();
        string snapshot = Path.Combine(privateDir, "tmdl-validate-" + Guid.NewGuid().ToString("N"));
        if (IsInsideFolder(snapshot, folder))
            throw new BadRequestException("validation scratch directory must be outside the candidate folder");
        LinkGuard.Refuse(snapshot);
        SweepSnapshots(privateDir, folder);
        // A fresh GUID collision is unsafe: CreateNew must fail rather than overwrite
        // or remove an existing owner's marker. Publish ownership before copying.
        var lease = new FileStream(snapshot + ".pbz-owner", FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None);
        ActiveSnapshots.TryAdd(snapshot, 0);
        var cancellation = new CancellationTokenSource();
        Task? copy = null;
        try
        {
            copy = Task.Run(() => CopyTree(folder, snapshot, cancellation.Token));
            // Match Guard's deadline and exception semantics, but retain the worker for cleanup.
            try
            {
                if (!copy.Wait(TimeSpan.FromSeconds(ctx.TimeoutSec + 10)))
                    throw new TimeoutException("engine operation exceeded its deadline");
            }
            catch (AggregateException ae) when (ae.InnerException != null)
            {
                System.Runtime.ExceptionServices.ExceptionDispatchInfo.Capture(ae.InnerException).Throw();
            }
            return RunSnapshot(req, ctx, snapshot);
        }
        finally
        {
            cancellation.Cancel();
            if (copy == null || copy.IsCompleted)
            {
                _ = copy?.Exception;
                CleanupSnapshot(snapshot, lease);
                cancellation.Dispose();
            }
            else
            {
                // A timed-out worker retains ownership until it has stopped writing. If the
                // process exits first, a later sweep recovers only marked snapshots whose
                // exclusive cross-process lease can be acquired.
                _ = copy.ContinueWith(completed =>
                {
                    _ = completed.Exception; // Observe any failure after the caller timed out.
                    CleanupSnapshot(snapshot, lease);
                    cancellation.Dispose();
                }, CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
            }
        }
    }

    static void SweepSnapshots(string privateDir, string folder)
    {
        try
        {
            foreach (var stale in Directory.EnumerateDirectories(privateDir, "tmdl-validate-*"))
            {
                if (string.Equals(Path.TrimEndingDirectorySeparator(stale), Path.TrimEndingDirectorySeparator(folder),
                        StringComparison.OrdinalIgnoreCase)
                    || IsInsideFolder(stale, folder) || IsInsideFolder(folder, stale)) continue;
                try
                {
                    // Missing markers are unowned; an exclusive-open failure leaves a
                    // live owner's tree alone, including owners in another process.
                    using (new FileStream(stale + ".pbz-owner", FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { }
                }
                catch (IOException) { continue; }
                catch (UnauthorizedAccessException) { continue; }
                // Claim stale paths too, so concurrent sweeps cannot traverse the same tree.
                if (ActiveSnapshots.TryAdd(stale, 0)) CleanupSnapshot(stale);
            }
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    static void CleanupSnapshot(string snapshot, FileStream? lease = null)
    {
        // Best effort: a leftover scratch directory must not fail a completed validation.
        try
        {
            if (!LinkGuard.IsLink(snapshot) && Directory.Exists(snapshot))
            {
                ClearReadOnly(snapshot);
                Directory.Delete(snapshot, recursive: true);
            }
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
        finally
        {
            // Hold ownership through the directory cleanup, then release before removing
            // the sibling marker (Windows disallows deleting an exclusively held file).
            lease?.Dispose();
            try
            {
                // Retain proof of ownership when best-effort tree deletion failed.
                if (!Directory.Exists(snapshot)) File.Delete(snapshot + ".pbz-owner");
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            ActiveSnapshots.TryRemove(snapshot, out _);
        }
        AfterSnapshotCleanupForTests?.Invoke(snapshot);
    }

    internal static bool IsInsideFolder(string candidate, string folder)
    {
        candidate = Path.GetFullPath(candidate);
        folder = Path.TrimEndingDirectorySeparator(Path.GetFullPath(folder));
        string prefix = Path.EndsInDirectorySeparator(folder) ? folder : folder + Path.DirectorySeparatorChar;
        return !string.Equals(Path.TrimEndingDirectorySeparator(candidate), folder, StringComparison.OrdinalIgnoreCase)
            && candidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);
    }

    static void ClearReadOnly(string path)
    {
        var attributes = File.GetAttributes(path);
        File.SetAttributes(path, attributes & ~FileAttributes.ReadOnly);
        if (attributes.HasFlag(FileAttributes.Directory))
            foreach (var entry in Directory.EnumerateFileSystemEntries(path)) ClearReadOnly(entry);
    }

    // Copy failures propagate: no validation or signing may fall back to the live folder.
    static void CopyTree(string source, string destination, CancellationToken cancellation)
    {
        cancellation.ThrowIfCancellationRequested();
        LinkGuard.Refuse(source);
        Directory.CreateDirectory(destination);
        foreach (var entry in Directory.EnumerateFileSystemEntries(source))
        {
            cancellation.ThrowIfCancellationRequested();
            LinkGuard.Refuse(entry);
            string target = Path.Combine(destination, Path.GetFileName(entry));
            if (File.GetAttributes(entry).HasFlag(FileAttributes.Directory))
                CopyTree(entry, target, cancellation);
            else if (string.Equals(Path.GetExtension(entry), ".tmdl", StringComparison.OrdinalIgnoreCase))
            {
                cancellation.ThrowIfCancellationRequested();
                File.Copy(entry, target);
                AfterCopyFileForTests?.Invoke(target);
            }
        }
    }

    static JsonObject RunSnapshot(Req req, Ctx ctx, string folder)
    {
        var scrub = new Scrubber(CollectNames(folder));
        var errors = new JsonArray();
        Database? db = null;
        try
        {
            db = ctx.Guard(() => TmdlSerializer.DeserializeDatabaseFromFolder(folder));
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

        BeforeInventoryForTests?.Invoke();
        var (missing, orphans) = Inventory(folder);
        var missingArr = new JsonArray();
        foreach (var m in missing) missingArr.Add(m);
        var orphanArr = new JsonArray();
        foreach (var o in orphans) orphanArr.Add(o);
        if (errors.Count == 0 && missing.Count == 0 && req.Str("synthetic_marker") is string marker)
        {
            if (db!.Model.Annotations.Find("PBZ_SYNTHETIC")?.Value != marker)
                throw new BadRequestException("SYNTH_MARKER_MISMATCH: candidate marker differs");
            SyntheticProvenance.Save(ctx, marker, SyntheticProvenance.Definition(db.Model));
        }
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
        foreach (var f in TmdlFiles(folder))
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
        var indexes = new Dictionary<string, TmdlDirIndex>(StringComparer.Ordinal);
        TmdlDirIndex IndexFor(string dir)
        {
            if (!indexes.TryGetValue(dir, out var index))
            {
                index = TmdlDirIndex.Build(Path.Combine(folder, dir),
                    p => Directory.EnumerateFiles(p, "*.tmdl"), LinkGuard.Refuse, Directory.Exists);
                indexes.Add(dir, index);
            }
            return index;
        }
        var missing = new List<string>();
        foreach (var top in new[] { "model.tmdl", "database.tmdl" })
        {
            string p = Path.Combine(folder, top);
            LinkGuard.Refuse(p);
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
                if (IndexFor(dir).Find(name) == null) missing.Add(dir + "/" + EncodeName(name) + ".tmdl");
            }
        }
        var orphans = new List<string>();
        foreach (var (kind, dir) in RefDirs)
        {
            foreach (var f in IndexFor(dir).Entries.OrderBy(x => x, StringComparer.Ordinal))
            {
                string objName = DecodeName(Path.GetFileNameWithoutExtension(f));
                if (!refs[kind].Contains(objName)) orphans.Add(dir + "/" + Path.GetFileName(f));
            }
        }
        return (missing.Distinct().ToList(), orphans);
    }

    static IEnumerable<string> TmdlFiles(string folder)
    {
        LinkGuard.Refuse(folder);
        foreach (var entry in Directory.EnumerateFileSystemEntries(folder))
        {
            LinkGuard.Refuse(entry);
            if (File.GetAttributes(entry).HasFlag(FileAttributes.Directory))
            {
                foreach (var child in TmdlFiles(entry)) yield return child;
            }
            else if (entry.EndsWith(".tmdl", StringComparison.OrdinalIgnoreCase))
            {
                yield return entry;
            }
        }
    }

    /// <summary>TMDL file naming: characters invalid in file names (and '%') become %XX.</summary>
    public static string EncodeName(string name) => TmdlNameCodec.EncodeName(name);

    public static string DecodeName(string fileName) => TmdlNameCodec.DecodeName(fileName);
}
