using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>
/// deps enumeration: deduplicate a raw DISCOVER_CALC_DEPENDENCY row stream
/// into published edges, bounded by a raw-row cap applied BEFORE
/// deduplication. The cap is demonstrated, not inferred: truncated is true
/// exactly when a (cap + 1)th raw row exists, i.e. the stream was not
/// exhausted within the cap. At most cap + 1 rows are ever pulled from the
/// iterator. Published edges are the deduplication, in first-seen order, of
/// the first cap raw rows, with the caller's name/type scrubbing applied per
/// cell (type for cells 0 and 3, name for cells 1, 2, 4 and 5).
/// </summary>
public static class DepsCollect
{
    public const int Cap = 200000;

    public static (JsonArray Edges, bool Truncated) Collect(
        IEnumerable<object?[]> rawRows, int cap,
        Func<object?, string> name, Func<object?, string> type)
    {
        var edges = new JsonArray();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        int taken = 0;
        bool truncated = false;
        foreach (var r in rawRows)
        {
            if (taken >= cap)
            {
                truncated = true;
                break;
            }
            taken++;
            var row = new[] { type(r[0]), name(r[1]), name(r[2]), type(r[3]), name(r[4]), name(r[5]) };
            if (!seen.Add(string.Join("\u0001", row))) continue;
            var a = new JsonArray();
            foreach (var cell in row) a.Add(cell);
            edges.Add(a);
        }
        return (edges, truncated);
    }
}
