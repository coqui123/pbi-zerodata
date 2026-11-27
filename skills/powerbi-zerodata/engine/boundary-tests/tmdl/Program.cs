using System.Diagnostics;
using System.Globalization;
using PbzEngine;

// Synthetic boundary tests for the TmdlDirIndex seam and the transcribed current scan.
// No Desktop, no model. Prints PASS/FAIL per case, COUNT lines for enumeration counts,
// and TIMING lines for paired timings (reported, not asserted).

var refDirs = new Dictionary<string, string>(StringComparer.Ordinal)
{
    ["table"] = "tables",
    ["role"] = "roles",
    ["perspective"] = "perspectives",
    ["cultureInfo"] = "cultures"
};
int failures = 0;

void Check(string name, bool ok, string detail)
{
    Console.WriteLine(ok ? "PASS " + name : "FAIL " + name + " :: " + detail);
    if (!ok) failures++;
}

static string DecodePath(string path) =>
    TmdlNameCodec.DecodeName(Path.GetFileNameWithoutExtension(path));

static string? FindScan(string dir, Func<string, IEnumerable<string>> entries,
    Func<string, bool> exists, Action<string> refuse, string name)
{
    refuse(dir);
    if (!exists(dir)) return null;
    foreach (var entry in entries(dir))
    {
        refuse(entry);
        if (string.Equals(DecodePath(entry), name, StringComparison.OrdinalIgnoreCase)) return entry;
    }
    return null;
}

(List<string> Missing, List<string> Orphans) Compose(string root,
    (string Kind, string Name)[] references, Func<string, string, string?> find,
    Func<string, IEnumerable<string>> orphanEntries)
{
    var refs = refDirs.Keys.ToDictionary(kind => kind, _ => new HashSet<string>(StringComparer.OrdinalIgnoreCase));
    var missing = new List<string>();
    foreach (var (kind, name) in references)
    {
        string dir = refDirs[kind];
        refs[kind].Add(name);
        if (find(Path.Combine(root, dir), name) == null)
            missing.Add(dir + "/" + TmdlNameCodec.EncodeName(name) + ".tmdl");
    }
    var orphans = new List<string>();
    foreach (var (kind, dir) in refDirs)
    {
        foreach (var entry in orphanEntries(Path.Combine(root, dir)))
        {
            if (!refs[kind].Contains(DecodePath(entry))) orphans.Add(dir + "/" + Path.GetFileName(entry));
        }
    }
    return (missing.Distinct().ToList(), orphans);
}

(List<string> Missing, List<string> Orphans) Scan(string root,
    (string Kind, string Name)[] refs, Func<string, IEnumerable<string>> enumerate,
    Action<string> refuse, Func<string, bool> exists)
{
    IEnumerable<string> OrphanEntries(string dir)
    {
        refuse(dir);
        if (!exists(dir)) yield break;
        foreach (var entry in enumerate(dir).OrderBy(path => path, StringComparer.Ordinal))
        {
            refuse(entry);
            yield return entry;
        }
    }

    return Compose(root, refs, (dir, name) => FindScan(dir, enumerate, exists, refuse, name), OrphanEntries);
}

(List<string> Missing, List<string> Orphans) Index(string root,
    (string Kind, string Name)[] refs, Func<string, IEnumerable<string>> enumerate,
    Action<string> refuse, Func<string, bool> exists)
{
    var indexes = new Dictionary<string, TmdlDirIndex>(StringComparer.Ordinal);

    TmdlDirIndex GetIndex(string dir)
    {
        if (!indexes.TryGetValue(dir, out var index))
        {
            index = TmdlDirIndex.Build(dir, enumerate, refuse, exists);
            indexes.Add(dir, index);
        }
        return index;
    }

    return Compose(root, refs, (dir, name) => GetIndex(dir).Find(name),
        dir => GetIndex(dir).Entries.OrderBy(path => path, StringComparer.Ordinal));
}

static bool Equal((List<string> Missing, List<string> Orphans) a, (List<string> Missing, List<string> Orphans) b) =>
    a.Missing.SequenceEqual(b.Missing) && a.Orphans.SequenceEqual(b.Orphans);

static ((List<string> Missing, List<string> Orphans) Result, Exception? Error) Capture(
    Func<(List<string> Missing, List<string> Orphans)> run)
{
    try { return (run(), null); }
    catch (Exception ex) { return (default, ex); }
}

void Parity(string name, Dictionary<string, string[]> listings, (string Kind, string Name)[] refs,
    string[]? links = null, string[]? expectedMissing = null, string[]? expectedOrphans = null,
    string? firstMatch = null)
{
    var existingDirs = listings.Keys.ToHashSet(StringComparer.Ordinal);
    var linkPaths = (links ?? []).ToHashSet(StringComparer.Ordinal);
    IEnumerable<string> Enumerate(string dir) => listings[dir];
    void Refuse(string path)
    {
        if (linkPaths.Contains(path))
            throw new EngineException(ErrorClass.OTHER, "refusing to read through a link");
    }

    var scan = Capture(() => Scan("", refs, Enumerate, Refuse, existingDirs.Contains));
    var index = Capture(() => Index("", refs, Enumerate, Refuse, existingDirs.Contains));
    bool equal = scan.Error is { } scanError
        ? index.Error is { } indexError && scanError.GetType().Name == indexError.GetType().Name &&
            scanError.Message == indexError.Message
        : index.Error == null && Equal(scan.Result, index.Result);

    bool expected = links != null
        ? scan.Error is EngineException { Class: ErrorClass.OTHER, Message: "refusing to read through a link" }
        : scan.Error == null &&
            (expectedMissing == null || scan.Result.Missing.SequenceEqual(expectedMissing)) &&
            (expectedOrphans == null || scan.Result.Orphans.SequenceEqual(expectedOrphans));

    if (equal && expected && firstMatch != null)
    {
        var (kind, objectName) = refs[0];
        string dir = refDirs[kind];
        expected = FindScan(dir, Enumerate, existingDirs.Contains, Refuse, objectName) == firstMatch &&
            TmdlDirIndex.Build(dir, Enumerate, Refuse, existingDirs.Contains).Find(objectName) == firstMatch;
    }

    Check(name, equal && expected, $"parity={equal}, expected={expected}, scan error={scan.Error?.Message}, index error={index.Error?.Message}");
}

Parity("parity-small-mix", new() { ["tables"] = ["tables/Person.tmdl", "tables/Project.tmdl", "tables/Notes.tmdl"] },
    [("table", "Person"), ("table", "Ghost"), ("table", "Notes")],
    expectedMissing: ["tables/Ghost.tmdl"], expectedOrphans: ["tables/Project.tmdl"]);
Parity("parity-case-variant", new() { ["tables"] = ["tables/person.tmdl"] }, [("table", "Person")]);
Parity("parity-encoded-dup-a", new() { ["roles"] = ["roles/Canary Reader.tmdl", "roles/Canary%20Reader.tmdl"] },
    [("role", "Canary Reader")], firstMatch: "roles/Canary Reader.tmdl");
Parity("parity-encoded-dup-b", new() { ["roles"] = ["roles/Canary%20Reader.tmdl", "roles/Canary Reader.tmdl"] },
    [("role", "Canary Reader")], firstMatch: "roles/Canary%20Reader.tmdl");
Parity("parity-case-dup", new() { ["tables"] = ["tables/PERSON.tmdl", "tables/person.tmdl"] },
    [("table", "Person")], firstMatch: "tables/PERSON.tmdl");
Parity("parity-missing-dir", new(), [("perspective", "X")]);
Parity("parity-orphans-two-dirs", new() { ["tables"] = ["tables/Notes.tmdl"], ["roles"] = ["roles/Canary Reader.tmdl"] }, []);
Parity("parity-no-ref-dir", new() { ["cultures"] = ["cultures/en-US.tmdl"] }, [],
    expectedOrphans: ["cultures/en-US.tmdl"]);
Parity("parity-link-before-match", new() { ["tables"] = ["tables/ZLink.tmdl", "tables/Person.tmdl"] },
    [("table", "Person")], links: ["tables/ZLink.tmdl"]);
Parity("parity-link-after-match", new() { ["tables"] = ["tables/Person.tmdl", "tables/ZLink.tmdl"] },
    [("table", "Person")], links: ["tables/ZLink.tmdl"]);
Parity("parity-link-dir", new() { ["tables"] = ["tables/Person.tmdl"] }, [("table", "Person")], links: ["tables"]);

(string Input, string Expected)[] encodeVectors =
[
    ("A/B", "A%2FB"), ("a b", "a b"), ("100%", "100%25"), (":", "%3A"), ("*", "%2A"),
    ("?", "%3F"), ("\"", "%22"), ("<", "%3C"), (">", "%3E"), ("|", "%7C"),
    ("\\", "%5C"), ("\u0001", "%01"), ("\u001F", "%1F"), ("plain", "plain")
];
(string Input, string Expected)[] decodeVectors =
[
    ("A%2FB", "A/B"), ("Canary%20Reader", "Canary Reader"), ("100%25", "100%"), ("%41", "A"),
    ("a%2", "a%2"), ("a%ZZ", "a%ZZ"), ("%", "%"), ("plain", "plain")
];
string[] roundTrips = ["A/B", "Canary Reader", "100%", "a:b", "T0001"];
var codecFailures = new List<string>();
foreach (var (input, expected) in encodeVectors)
    if (TmdlNameCodec.EncodeName(input) != expected) codecFailures.Add("encode expected " + expected);
foreach (var (input, expected) in decodeVectors)
    if (TmdlNameCodec.DecodeName(input) != expected) codecFailures.Add("decode " + input);
foreach (var input in roundTrips)
    if (TmdlNameCodec.DecodeName(TmdlNameCodec.EncodeName(input)) != input) codecFailures.Add("round trip " + input);
Check("codec-vectors", codecFailures.Count == 0, string.Join(", ", codecFailures));

static IEnumerable<string> EnumerateFiles(string dir) => Directory.EnumerateFiles(dir, "*.tmdl");

void Count(string name, string root, (string Kind, string Name)[] refs, bool absentDir)
{
    int calls = 0, visits = 0;
    IEnumerable<string> CountEntries(string dir)
    {
        calls++;
        return VisitEntries(dir);
    }

    IEnumerable<string> VisitEntries(string dir)
    {
        foreach (var entry in EnumerateFiles(dir))
        {
            visits++;
            yield return entry;
        }
    }

    var scan = Scan(root, refs, CountEntries, LinkGuard.Refuse, Directory.Exists);
    int scanCalls = calls, scanVisits = visits;
    calls = visits = 0;
    var index = Index(root, refs, CountEntries, LinkGuard.Refuse, Directory.Exists);
    Console.WriteLine($"COUNT {name} scan_calls={scanCalls} scan_visits={scanVisits} index_calls={calls} index_visits={visits}");
    bool absentMissing = !absentDir || scan.Missing.SequenceEqual(
        refs.Select(reference => "tables/" + TmdlNameCodec.EncodeName(reference.Name) + ".tmdl"));
    Check("count-" + name, Equal(scan, index) && scanCalls == (absentDir ? 0 : refs.Length + 1) &&
        calls == (absentDir ? 0 : 1) && absentMissing, "unexpected outcomes or enumeration counts");
}

static string Format(double value) => value.ToString("F3", CultureInfo.InvariantCulture);
static string FormatList(IEnumerable<double> values) => "[" + string.Join(",", values.Select(Format)) + "]";

void Time(string name, string root, (string Kind, string Name)[] refs)
{
    int repetitions = name == "SMALL" ? 50 : 1;
    void RunScan() => Scan(root, refs, EnumerateFiles, LinkGuard.Refuse, Directory.Exists);
    void RunIndex() => Index(root, refs, EnumerateFiles, LinkGuard.Refuse, Directory.Exists);

    double Measure(Action run)
    {
        var stopwatch = Stopwatch.StartNew();
        for (int i = 0; i < repetitions; i++) run();
        return stopwatch.Elapsed.TotalMilliseconds / repetitions;
    }

    RunScan();
    RunIndex();
    var scan = new double[7];
    var index = new double[7];
    for (int i = 0; i < scan.Length; i++)
    {
        if (i % 2 == 0)
        {
            scan[i] = Measure(RunScan);
            index[i] = Measure(RunIndex);
        }
        else
        {
            index[i] = Measure(RunIndex);
            scan[i] = Measure(RunScan);
        }
    }

    Console.WriteLine($"TIMING {name} scan={FormatList(scan)} index={FormatList(index)}" +
        $" scan_median={Format(scan.OrderBy(ms => ms).ElementAt(3))} scan_min={Format(scan.Min())} scan_max={Format(scan.Max())}" +
        $" index_median={Format(index.OrderBy(ms => ms).ElementAt(3))} index_min={Format(index.Min())} index_max={Format(index.Max())}" +
        $" ratios={FormatList(index.Zip(scan, (indexMs, scanMs) => indexMs / scanMs))}");
}

(string Name, int Files, int Refs, bool Alternating)[] workloads =
[
    ("SMALL", 40, 40, false),
    ("REALISTIC", 400, 400, false),
    ("LARGE-PRESENT", 4000, 4000, false),
    ("LARGE-MISSING", 4000, 4000, true),
    ("ABSENT-DIR", 0, 5, false)
];
foreach (var (name, files, refCount, alternating) in workloads)
{
    string root = Directory.CreateTempSubdirectory("pbz-tmdl-").FullName;
    try
    {
        string tables = Path.Combine(root, "tables");
        if (files > 0) Directory.CreateDirectory(tables);
        for (int i = 0; i < files; i++)
            File.WriteAllText(Path.Combine(tables, "F" + i.ToString("D4", CultureInfo.InvariantCulture) + ".tmdl"), "");

        var refs = new (string Kind, string Name)[refCount];
        for (int i = 0; i < refs.Length; i++)
            refs[i] = ("table", (alternating && i % 2 != 0 ? "A" : "F") + i.ToString("D4", CultureInfo.InvariantCulture));

        Count(name, root, refs, files == 0);
        if (files > 0) Time(name, root, refs);
    }
    finally
    {
        Directory.Delete(root, recursive: true);
    }
}

return failures == 0 ? 0 : 1;

namespace PbzEngine
{
    public enum ErrorClass { OTHER }

    public sealed class EngineException : Exception
    {
        public ErrorClass Class { get; }
        public EngineException(ErrorClass cls, string message, Exception? inner = null) : base(message, inner) { Class = cls; }
    }
}
