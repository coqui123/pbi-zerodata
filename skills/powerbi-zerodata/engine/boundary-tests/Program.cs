using System.Text.Json.Nodes;
using PbzEngine;

// Synthetic boundary tests for DepsCollect.Collect, the production deps
// enumeration seam. No Desktop and no model: synthetic raw-row iterators
// only. Prints one PASS/FAIL line per case and exits nonzero on any failure.

int failures = 0;

void Check(string name, bool ok, string detail = "")
{
    Console.WriteLine((ok ? "PASS " : "FAIL ") + name + (ok || detail.Length == 0 ? "" : " :: " + detail));
    if (!ok) failures++;
}

string[] Row(string obj, string refObj) =>
    new[] { "MEASURE", "Sales", obj, "COLUMN", "Sales", refObj };

object?[] Raw(string[] cells) => Array.ConvertAll(cells, c => (object?)c);

// Test mapper mirroring Deps.Run's scrubbing: a name passes only when it is
// in the known set (or empty), else "?"; a type must match ^[A-Z_]{1,40}$,
// else "?".
var known = new HashSet<string>(StringComparer.Ordinal) { "Sales", "Amount", "Total", "Other", "Extra" };
string NameOf(object? v)
{
    string s = v as string ?? "";
    return s.Length == 0 || known.Contains(s) ? s : "?";
}
string TypeOf(object? v)
{
    string s = v as string ?? "";
    return System.Text.RegularExpressions.Regex.IsMatch(s, "\\A[A-Z_]{1,40}\\z") ? s : "?";
}
string Identity(object? v) => v as string ?? "";

bool EdgesEqual(JsonArray edges, params string[][] expected)
{
    if (edges.Count != expected.Length) return false;
    for (int i = 0; i < expected.Length; i++)
    {
        var row = edges[i]!.AsArray();
        if (row.Count != expected[i].Length) return false;
        for (int j = 0; j < expected[i].Length; j++)
            if (row[j]!.GetValue<string>() != expected[i][j]) return false;
    }
    return true;
}

IEnumerable<object?[]> Rows(params string[][] rows) => rows.Select(Raw);

IEnumerable<object?[]> Gen(int n, Func<int, string[]> make)
{
    for (int i = 0; i < n; i++) yield return Raw(make(i));
}

IEnumerable<object?[]> Infinite()
{
    for (int i = 0; ; i++)
        yield return Raw(new[] { "MEASURE", "Sales", "M" + i, "COLUMN", "Sales", "Amount" });
}

var A = Row("Total", "Amount");
var B = Row("Other", "Amount");
var C = Row("Extra", "Amount");
var D = Row("Total", "Extra");
var E = Row("Other", "Extra");

// 1. Below the cap: complete, first-seen order, every row pulled.
{
    var it = new Counting(Rows(A, B, C));
    var (edges, truncated) = DepsCollect.Collect(it, 4, NameOf, TypeOf);
    Check("below-cap", !truncated && it.Pulls == 3 && EdgesEqual(edges, A, B, C));
}
// 2. Exactly at the cap: complete.
{
    var it = new Counting(Rows(A, B, C, D));
    var (edges, truncated) = DepsCollect.Collect(it, 4, NameOf, TypeOf);
    Check("at-cap", !truncated && it.Pulls == 4 && EdgesEqual(edges, A, B, C, D));
}
// 3. One row above the cap: truncated, the lookahead row is not published.
{
    var it = new Counting(Rows(A, B, C, D, E));
    var (edges, truncated) = DepsCollect.Collect(it, 4, NameOf, TypeOf);
    Check("above-cap", truncated && it.Pulls == 5 && EdgesEqual(edges, A, B, C, D));
}
// 4. Above the cap with only duplicates past it: still truncated, and
// consumption stops at the lookahead row.
{
    var it = new Counting(Rows(A, B, C, D, A, B, A, C, D));
    var (edges, truncated) = DepsCollect.Collect(it, 4, NameOf, TypeOf);
    Check("duplicate-overflow", truncated && it.Pulls == 5 && EdgesEqual(edges, A, B, C, D));
}
// 5. Duplicates inside the cap: deduplicated, first-seen order, complete.
{
    var it = new Counting(Rows(A, A, B, C));
    var (edges, truncated) = DepsCollect.Collect(it, 4, NameOf, TypeOf);
    Check("duplicate-inside", !truncated && it.Pulls == 4 && EdgesEqual(edges, A, B, C));
}
// 6. Duplicates inside the cap plus a distinct row past it: truncated and
// the past-cap row is not published.
{
    var it = new Counting(Rows(A, A, B, C, D));
    var (edges, truncated) = DepsCollect.Collect(it, 4, NameOf, TypeOf);
    Check("duplicate-inside-distinct-beyond", truncated && it.Pulls == 5 && EdgesEqual(edges, A, B, C));
}
// 7. Bounded consumption: an infinite iterator is pulled exactly cap + 1 times.
{
    var it = new Counting(Infinite());
    var (edges, truncated) = DepsCollect.Collect(it, 4, Identity, TypeOf);
    Check("bounded-consumption", truncated && it.Pulls == 5 && edges.Count == 4);
}
// 8. Full size at the production constant: exactly Cap rows complete,
// Cap + 1 rows truncated with Cap published edges. The identity name
// mapper keeps every generated row distinct after scrubbing.
{
    Check("cap-constant", DepsCollect.Cap == 200000);
    string[] Make(int i) => new[] { "MEASURE", "Sales", "M" + i, "COLUMN", "Sales", "Amount" };
    var atIt = new Counting(Gen(DepsCollect.Cap, Make));
    var (atEdges, atTruncated) = DepsCollect.Collect(atIt, DepsCollect.Cap, Identity, TypeOf);
    Check("full-size-at-cap", !atTruncated && atIt.Pulls == DepsCollect.Cap && atEdges.Count == DepsCollect.Cap);
    var overIt = new Counting(Gen(DepsCollect.Cap + 1, Make));
    var (overEdges, overTruncated) = DepsCollect.Collect(overIt, DepsCollect.Cap, Identity, TypeOf);
    Check("full-size-above-cap", overTruncated && overIt.Pulls == DepsCollect.Cap + 1 && overEdges.Count == DepsCollect.Cap);
}
// 9. Scrubbing is applied per cell position: unknown names and malformed
// types publish "?".
{
    var unknownName = new[] { "MEASURE", "Sales", "Secret", "COLUMN", "Sales", "Amount" };
    var badType = new[] { "measure", "Sales", "Total", "COLUMN", "Sales", "Amount" };
    var it = new Counting(Rows(unknownName, badType));
    var (edges, truncated) = DepsCollect.Collect(it, 4, NameOf, TypeOf);
    var expectedUnknown = new[] { "MEASURE", "Sales", "?", "COLUMN", "Sales", "Amount" };
    var expectedBadType = new[] { "?", "Sales", "Total", "COLUMN", "Sales", "Amount" };
    Check("scrubbing", !truncated && EdgesEqual(edges, expectedUnknown, expectedBadType));
}

Console.WriteLine(failures == 0 ? "ALL PASS" : $"{failures} FAILURE(S)");
return failures == 0 ? 0 : 1;

// Counting iterator: Pulls records how many rows were successfully yielded.
sealed class Counting : IEnumerable<object?[]>
{
    readonly IEnumerable<object?[]> inner;
    public int Pulls;
    public Counting(IEnumerable<object?[]> inner) => this.inner = inner;
    public IEnumerator<object?[]> GetEnumerator()
    {
        foreach (var r in inner)
        {
            Pulls++;
            yield return r;
        }
    }
    System.Collections.IEnumerator System.Collections.IEnumerable.GetEnumerator() => GetEnumerator();
}
