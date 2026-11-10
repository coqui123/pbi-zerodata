using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using ModeType = Microsoft.AnalysisServices.Tabular.ModeType;

namespace PbzEngine;

/// <summary>
/// memory: VertiPaq storage attributed to model objects the way VertiPaq Analyzer does, released only as fixed bands.
///
/// Reads $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMNS (DICTIONARY_SIZE of BASIC_DATA columns) and
/// $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS (USED_SIZE) through <see cref="Conn.StorageDmv"/>, the narrow exception
/// to the TMSCHEMA-only DMV rule. Sizes, row counts and segment counts never leave this process. Storage ids embed the
/// TMSCHEMA ids of the objects they belong to:
///   "T (tid)" with column "C (cid)"   table data: dictionary and data segments per column, plus the RowNumber column
///   "H$T (tid)$C (cid)"               attribute hierarchy of column cid
///   "U$T (tid)$H (hid)"               user hierarchy hid
///   "R$T (tid)$R (rid)"               relationship rid (stored with its from table)
/// Names inside storage ids are sanitized by the engine (hyphens become spaces), so objects are matched by id first and by
/// name only as a fallback. Storage that cannot be attributed counts as "other" and makes coverage PARTIAL; storage ids
/// are never printed (only TMSCHEMA object names are), they go to the DPAPI log for the user.
///
/// Optional predicates (constant-free DAX chosen by the engine, booleans only) on columns of imported tables:
/// time_part (dateTime: a non-blank value with a time of day), fractional (double/decimal: a non-integer value),
/// long_text (string: a value longer than 100 characters). Small models (under 100 MiB) get them on every eligible
/// column, larger ones only on columns of 1 MiB and more; at most 500 columns and a bounded total time per run.
/// A predicate that errors or times out is omitted.
/// </summary>
public static class Memory
{
    const long MiB = 1024L * 1024L;
    const long SmallModelBytes = 100 * MiB;
    const long PredicateMinBytes = MiB;
    const int MaxPredicateColumns = 500;
    const int LongTextChars = 100;
    const int MaxStorageRows = 5_000_000;
    const int RowNumberColumnType = 3;   // TOM ColumnType.RowNumber
    const int MaxLoggedIds = 20;

    static readonly Regex StructureId = new(@"^(?<k>[HUR])\$(?<t>.*?) \((?<tid>\d+)\)\$(?<o>.*) \((?<oid>\d+)\)$",
        RegexOptions.Compiled | RegexOptions.CultureInvariant | RegexOptions.Singleline);

    static readonly Regex PlainId = new(@"^(?<n>.*) \((?<id>\d+)\)$",
        RegexOptions.Compiled | RegexOptions.CultureInvariant | RegexOptions.Singleline);

    sealed class ColumnSize
    {
        public long Dictionary, Data, Hierarchy;
        public int Segments;
        public long Total => Dictionary + Data + Hierarchy;
    }

    sealed record HierInfo(ulong Id, TableMeta Table, string Name);

    sealed record RelInfo(ulong Id, string Name, ColumnMeta? From, ColumnMeta? To, bool Active)
    {
        public bool Private => (From?.Table.IsPrivate ?? false) || (To?.Table.IsPrivate ?? false);
    }

    /// <summary>TMSCHEMA ids the storage ids refer to.</summary>
    sealed class Ids
    {
        public Meta Meta = null!;
        public readonly Dictionary<ulong, TableMeta> Tables = new();
        public readonly Dictionary<ulong, ColumnMeta> Columns = new();
        public readonly HashSet<ulong> RowNumbers = new();
        public readonly Dictionary<ulong, HierInfo> Hierarchies = new();
        public readonly Dictionary<ulong, RelInfo> Relationships = new();
        public readonly HashSet<TableMeta> Imported = new();
    }

    /// <summary>Attributed byte counts (internal only).</summary>
    sealed class Storage
    {
        public long Total, Private, Other;
        public int Unattributed;
        public bool Truncated;
        public readonly Dictionary<ColumnMeta, ColumnSize> Columns = new();
        public readonly Dictionary<TableMeta, long> TableRest = new();     // RowNumber and unattributed storage of a known table
        public readonly Dictionary<ulong, long> Hierarchies = new();
        public readonly Dictionary<ulong, long> Relationships = new();
        public readonly Dictionary<ulong, TableMeta> RelationshipHome = new();
        public readonly List<string> UnattributedIds = new();            // DPAPI log only
        public readonly Dictionary<ColumnMeta, string> Encoding = new();

        public ColumnSize Of(ColumnMeta c)
        {
            if (!Columns.TryGetValue(c, out var s)) Columns[c] = s = new ColumnSize();
            return s;
        }

        public long SizeOf(ColumnMeta c) => Columns.TryGetValue(c, out var s) ? s.Total : 0;
    }

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        bool predicates = req.Bool("predicates") ?? true;
        using var conn = Conn.Open(ctx, port);
        var ids = LoadIds(conn);
        var st = ReadStorage(conn, ids);
        try { ApplyEncodings(conn, ids, st); }
        catch (Exception ex)
        {
            ctx.LogText("memory encoding omitted");
            if (Errors.Classify(ex) == ErrorClass.TIMEOUT)
                try { conn.Reset(); } catch { }
        }
        if (st.Unattributed > 0 || st.Truncated)
            ctx.LogText("memory: storage not attributed (storage ids only)" + (st.Truncated ? ", rows truncated" : "") + "\n" +
                        string.Join("\n", st.UnattributedIds));

        string predicateState = "OFF";
        var results = new Dictionary<ColumnMeta, bool>();
        if (predicates)
        {
            bool small = st.Total < SmallModelBytes;
            var wanted = ids.Meta.UserTables.Where(t => ids.Imported.Contains(t))
                .SelectMany(t => t.Columns)
                .Where(c => PredicateKey(c) != null && (small || st.SizeOf(c) >= PredicateMinBytes))
                .ToList();
            var chosen = wanted.OrderByDescending(st.SizeOf).ThenBy(c => c.Key, StringComparer.Ordinal).Take(MaxPredicateColumns).ToList();
            results = RunPredicates(conn, ctx, chosen);
            predicateState = results.Count == wanted.Count ? "DONE" : "PARTIAL";
        }
        var ri = RunIntegrity(conn, ctx, ids);
        return Ctx.Ok("memory", Build(ids, st, results, predicateState, ctx.Strict, ri));
    }

    // ------------------------------------------------------------------ metadata

    static Ids LoadIds(Conn conn)
    {
        var ids = new Ids { Meta = conn.Meta };
        foreach (var t in ids.Meta.Tables)
        {
            ids.Tables[t.Id] = t;
            foreach (var c in t.Columns) ids.Columns[c.Id] = c;
        }
        foreach (var r in conn.Dmv("TMSCHEMA_COLUMNS", new[] { "ID", "Type" }))
            if (I(r[1]) == RowNumberColumnType) ids.RowNumbers.Add(U(r[0]));
        foreach (var r in conn.Dmv("TMSCHEMA_HIERARCHIES", new[] { "ID", "TableID", "Name" }))
            if (ids.Tables.TryGetValue(U(r[1]), out var t)) ids.Hierarchies[U(r[0])] = new HierInfo(U(r[0]), t, S(r[2]));
        foreach (var r in conn.Dmv("TMSCHEMA_RELATIONSHIPS", new[] { "ID", "Name", "FromColumnID", "ToColumnID", "IsActive" }))
            ids.Relationships[U(r[0])] = new RelInfo(U(r[0]), S(r[1]), ids.Columns.GetValueOrDefault(U(r[2])), ids.Columns.GetValueOrDefault(U(r[3])), r[4] is bool active && active);

        // imported = every partition is held in memory (Import, or Default with an Import model); predicates never touch
        // DirectQuery, Dual or DirectLake tables, so they cannot send queries to a source
        int import = (int)ModeType.Import, byDefault = (int)ModeType.Default;
        int modelMode = import;
        foreach (var r in conn.Dmv("TMSCHEMA_MODEL", new[] { "DefaultMode" }))
            if (r[0] != null) modelMode = I(r[0]);
        if (modelMode == byDefault) modelMode = import;
        var modes = new Dictionary<ulong, List<int>>();
        foreach (var r in conn.Dmv("TMSCHEMA_PARTITIONS", new[] { "TableID", "Mode" }))
        {
            if (!modes.TryGetValue(U(r[0]), out var list)) modes[U(r[0])] = list = new List<int>();
            int m = r[1] == null ? byDefault : I(r[1]);
            list.Add(m == byDefault ? modelMode : m);
        }
        foreach (var t in ids.Meta.Tables)
            if (modes.TryGetValue(t.Id, out var ms) && ms.Count > 0 && ms.All(m => m == import))
                ids.Imported.Add(t);
        return ids;
    }

    // ------------------------------------------------------------------ storage attribution

    static Storage ReadStorage(Conn conn, Ids ids)
    {
        var st = new Storage();
        var dictionaries = conn.StorageDmv("DISCOVER_STORAGE_TABLE_COLUMNS", MaxStorageRows + 1);
        var segments = conn.StorageDmv("DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS", MaxStorageRows + 1);
        st.Truncated = dictionaries.Count > MaxStorageRows || segments.Count > MaxStorageRows;
        // dictionaries of data columns only; the hierarchy and relationship structures hold no dictionary of their own
        foreach (var r in dictionaries.Take(MaxStorageRows))
            if (string.Equals(S(r[3]), "BASIC_DATA", StringComparison.Ordinal))
                Attribute(st, ids, S(r[0]), S(r[1]), S(r[2]), Bytes(r[4]), dictionary: true);
        foreach (var r in segments.Take(MaxStorageRows))
            Attribute(st, ids, S(r[0]), S(r[1]), S(r[2]), Bytes(r[3]), dictionary: false);
        return st;
    }

    static void Attribute(Storage st, Ids ids, string dimension, string tableId, string columnId, long bytes, bool dictionary)
    {
        st.Total += bytes;
        char kind = 'T';
        TableMeta? table = null;
        ulong objId = 0;
        string objName = columnId;
        var m = StructureId.Match(tableId);
        if (m.Success)
        {
            kind = m.Groups["k"].Value[0];
            ids.Tables.TryGetValue(Id(m.Groups["tid"].Value), out table);
            objId = Id(m.Groups["oid"].Value);
            objName = m.Groups["o"].Value;
        }
        else
        {
            var p = PlainId.Match(tableId);
            if (p.Success) ids.Tables.TryGetValue(Id(p.Groups["id"].Value), out table);
            var c = PlainId.Match(columnId);
            if (c.Success)
            {
                objId = Id(c.Groups["id"].Value);
                objName = c.Groups["n"].Value;
            }
        }
        // DIMENSION_NAME is the table's own name
        table ??= ids.Meta.Tables.FirstOrDefault(t => string.Equals(t.Name, dimension, StringComparison.Ordinal));
        if (table == null)
        {
            Unattributed(st, null, tableId, columnId, bytes);
            return;
        }
        if (table.IsPrivate)
        {
            st.Private += bytes;
            return;
        }
        switch (kind)
        {
            case 'H':
            {
                var col = FindColumn(ids, table, objId, objName);
                if (col != null)
                {
                    st.Of(col).Hierarchy += bytes;
                    return;
                }
                break;
            }
            case 'U':
            {
                var h = ids.Hierarchies.TryGetValue(objId, out var hi) && hi.Table == table
                    ? hi
                    : Unique(ids.Hierarchies.Values.Where(x => x.Table == table && SameName(x.Name, objName)));
                if (h != null)
                {
                    st.Hierarchies[h.Id] = st.Hierarchies.GetValueOrDefault(h.Id) + bytes;
                    return;
                }
                break;
            }
            case 'R':
            {
                var rel = ids.Relationships.TryGetValue(objId, out var ri)
                    ? ri
                    : Unique(ids.Relationships.Values.Where(x => SameName(x.Name, objName)));
                if (rel != null)
                {
                    // relationships to auto date/time tables go with those tables
                    if (rel.Private) st.Private += bytes;
                    else
                    {
                        st.Relationships[rel.Id] = st.Relationships.GetValueOrDefault(rel.Id) + bytes;
                        st.RelationshipHome.TryAdd(rel.Id, table);
                    }
                    return;
                }
                break;
            }
            default:
            {
                if (ids.RowNumbers.Contains(objId))
                {
                    AddRest(st, table, bytes);
                    return;
                }
                var col = FindColumn(ids, table, objId, objName);
                if (col != null)
                {
                    var s = st.Of(col);
                    if (dictionary) s.Dictionary += bytes;
                    else
                    {
                        s.Data += bytes;
                        // Segment rows already read here (dictionary:false, default kind). Not a new DMV.
                        s.Segments++;
                    }
                    return;
                }
                if (objName.StartsWith("RowNumber", StringComparison.Ordinal))
                {
                    AddRest(st, table, bytes);
                    return;
                }
                break;
            }
        }
        Unattributed(st, table, tableId, columnId, bytes);
    }

    static ColumnMeta? FindColumn(Ids ids, TableMeta table, ulong id, string name)
    {
        if (ids.Columns.TryGetValue(id, out var c) && c.Table == table) return c;
        return table.Columns.FirstOrDefault(x => string.Equals(x.Name, name, StringComparison.Ordinal))
               ?? Unique(table.Columns.Where(x => SameName(x.Name, name)));
    }

    static T? Unique<T>(IEnumerable<T> items) where T : class
    {
        T? found = null;
        foreach (var i in items)
        {
            if (found != null) return null;
            found = i;
        }
        return found;
    }

    /// <summary>Storage ids replace some characters of object names (hyphens become spaces): compare letters and digits.</summary>
    static bool SameName(string a, string b) => string.Equals(Loose(a), Loose(b), StringComparison.Ordinal);

    static string Loose(string s)
    {
        var sb = new StringBuilder(s.Length);
        bool space = false;
        foreach (char ch in s.Trim())
        {
            if (char.IsLetterOrDigit(ch))
            {
                sb.Append(char.ToLowerInvariant(ch));
                space = false;
            }
            else if (!space)
            {
                sb.Append(' ');
                space = true;
            }
        }
        return sb.ToString();
    }

    static void AddRest(Storage st, TableMeta table, long bytes) => st.TableRest[table] = st.TableRest.GetValueOrDefault(table) + bytes;

    static void Unattributed(Storage st, TableMeta? table, string tableId, string columnId, long bytes)
    {
        st.Other += bytes;
        st.Unattributed++;
        if (table != null) AddRest(st, table, bytes);
        if (st.UnattributedIds.Count < MaxLoggedIds) st.UnattributedIds.Add(tableId + " / " + columnId);
    }

    /// <summary>Encoding is not on the StorageDmv allowlist. Failure leaves the field absent. Only Hash or Value is kept.</summary>
    static void ApplyEncodings(Conn conn, Ids ids, Storage st)
    {
        var rows = conn.TryColumnEncoding(MaxStorageRows);
        if (rows == null) return;
        foreach (var r in rows)
        {
            string? enc = EncodingLabel(r.Length > 3 ? r[3] : null);
            if (enc == null) continue;
            var col = DataColumn(ids, S(r[0]), S(r[1]), S(r[2]));
            if (col != null) st.Encoding[col] = enc;
        }
    }

    static string? EncodingLabel(object? v)
    {
        if (v == null || v is DBNull) return null;
        string s = (Convert.ToString(v, CultureInfo.InvariantCulture) ?? "").Trim();
        if (s.Equals("HASH", StringComparison.OrdinalIgnoreCase)) return "Hash";
        if (s.Equals("VALUE", StringComparison.OrdinalIgnoreCase)) return "Value";
        return null;
    }

    static ColumnMeta? DataColumn(Ids ids, string dimension, string tableId, string columnId)
    {
        if (tableId.Contains('$') || columnId.Contains('$')) return null;
        TableMeta? table = null;
        ulong objId = 0;
        string objName = columnId;
        var p = PlainId.Match(tableId);
        if (p.Success) ids.Tables.TryGetValue(Id(p.Groups["id"].Value), out table);
        var c = PlainId.Match(columnId);
        if (c.Success)
        {
            objId = Id(c.Groups["id"].Value);
            objName = c.Groups["n"].Value;
        }
        table ??= ids.Meta.Tables.FirstOrDefault(t => string.Equals(t.Name, dimension, StringComparison.Ordinal));
        if (table == null || table.IsPrivate || ids.RowNumbers.Contains(objId)) return null;
        return FindColumn(ids, table, objId, objName);
    }

    sealed class RiMaps
    {
        public readonly Dictionary<ulong, long> Rel = new();
        public readonly Dictionary<ColumnMeta, long> Col = new();
    }

    /// <summary>
    /// Referential integrity for relationship ends that can be tested with COUNTROWS/FILTER/RELATED.
    /// Counts stay in this process and leave only as Buckets.Count. No key samples and no min/max.
    /// A query that errors is omitted; it does not fail the verb.
    /// </summary>
    static RiMaps RunIntegrity(Conn conn, Ctx ctx, Ids ids)
    {
        var maps = new RiMaps();
        var clock = Stopwatch.StartNew();
        double budget = Math.Clamp(ctx.TimeoutSec * 2.0, 15.0, 300.0);
        foreach (var rel in ids.Relationships.Values)
        {
            if (clock.Elapsed.TotalSeconds >= budget) break;
            if (!rel.Active || rel.Private || rel.From == null || rel.To == null) continue;
            if (!ids.Imported.Contains(rel.From.Table) || !ids.Imported.Contains(rel.To.Table)) continue;
            try
            {
                string table = Dax.QTable(rel.From.Table.Name);
                string keyed = "NOT (" + rel.From.BlankPredicate + ")";
                string q = "EVALUATE ROW(\"n\", COUNTROWS(FILTER(" + table + ", " + keyed + " && ISBLANK(RELATED(" + rel.To.Ref + ")))))";
                long n = ColumnStats.L(conn.Row1(q)[0]);
                if (n < 0) n = 0;
                maps.Rel[rel.Id] = n;
                maps.Col[rel.From] = AddCap(maps.Col.GetValueOrDefault(rel.From), n);
            }
            catch (Exception ex)
            {
                ctx.LogText("memory ri omitted");
                if (Errors.Classify(ex) == ErrorClass.TIMEOUT)
                {
                    try { conn.Reset(); } catch { }
                    break;
                }
            }
        }
        return maps;
    }

    static long AddCap(long a, long b)
    {
        if (b < 0) b = 0;
        if (a > long.MaxValue - b) return long.MaxValue;
        return a + b;
    }

    // ------------------------------------------------------------------ predicates

    static string? PredicateKey(ColumnMeta c) => c.DataType switch
    {
        9 => "time_part",
        8 or 10 => "fractional",
        2 => "long_text",
        _ => null,
    };

    static string PredicateExpr(ColumnMeta c)
    {
        string r = c.Ref;
        return c.IsText
            ? "NOT ISEMPTY(FILTER(DISTINCT(" + r + "), LEN(" + r + ") > " + LongTextChars.ToString(CultureInfo.InvariantCulture) + "))"
            : "NOT ISEMPTY(FILTER(DISTINCT(" + r + "), NOT ISBLANK(" + r + ") && " + r + " <> TRUNC(" + r + ")))";
    }

    /// <summary>One ROW query per table; on an error, one query per column. Errors and timeouts omit the predicate.</summary>
    static Dictionary<ColumnMeta, bool> RunPredicates(Conn conn, Ctx ctx, List<ColumnMeta> chosen)
    {
        var results = new Dictionary<ColumnMeta, bool>();
        var clock = Stopwatch.StartNew();
        double budget = Math.Clamp(ctx.TimeoutSec * 5.0, 30.0, 900.0);
        foreach (var group in chosen.GroupBy(c => c.Table))
        {
            if (clock.Elapsed.TotalSeconds >= budget) break;
            var cols = group.OrderBy(c => c.Name, StringComparer.Ordinal).ToList();
            try
            {
                var parts = new List<string>();
                for (int i = 0; i < cols.Count; i++)
                {
                    parts.Add("\"p" + i.ToString(CultureInfo.InvariantCulture) + "\"");
                    parts.Add(PredicateExpr(cols[i]));
                }
                var (names, rows) = conn.Table("EVALUATE ROW(" + string.Join(", ", parts) + ")", 1);
                if (rows.Count == 1)
                    for (int i = 0; i < names.Count; i++)
                    {
                        string n = Dax.ResultName(names[i]);
                        if (n.Length > 1 && n[0] == 'p'
                            && int.TryParse(n.AsSpan(1), NumberStyles.None, CultureInfo.InvariantCulture, out int k)
                            && k < cols.Count && rows[0][i] is bool b)
                            results[cols[k]] = b;
                    }
            }
            catch (Exception ex)
            {
                ctx.Log(ex, "memory predicates");
                if (Errors.Classify(ex) == ErrorClass.TIMEOUT)
                {
                    try { conn.Reset(); } catch { }
                    continue;
                }
                foreach (var c in cols)
                {
                    if (clock.Elapsed.TotalSeconds >= budget) break;
                    try
                    {
                        if (conn.Row1("EVALUATE ROW(\"p\", " + PredicateExpr(c) + ")")[0] is bool b) results[c] = b;
                    }
                    catch (Exception ex2)
                    {
                        ctx.Log(ex2, "memory predicate");
                        if (Errors.Classify(ex2) == ErrorClass.TIMEOUT)
                        {
                            try { conn.Reset(); } catch { }
                            break;
                        }
                    }
                }
            }
        }
        return results;
    }

    // ------------------------------------------------------------------ response

    static JsonObject Build(Ids ids, Storage st, Dictionary<ColumnMeta, bool> predicates, string predicateState, bool strict, RiMaps ri)
    {
        long total = st.Total;
        var outTables = new JsonArray();
        foreach (var t in ByName(ids.Meta.UserTables, t => t.Name))
        {
            long tableBytes = st.TableRest.GetValueOrDefault(t);
            var cols = new JsonArray();
            foreach (var c in ByName(t.Columns, c => c.Name))
            {
                st.Columns.TryGetValue(c, out var s);
                long bytes = s?.Total ?? 0;
                tableBytes += bytes;
                var o = new JsonObject { ["name"] = c.Name };
                if (!strict)
                {
                    o["size_band"] = Buckets.Size(bytes);
                    o["share_band"] = Buckets.StorageShare(bytes, total);
                }
                o["dictionary_heavy"] = s != null && bytes > 0 && s.Dictionary * 2 >= bytes;
                if (st.Encoding.TryGetValue(c, out var enc)) o["encoding"] = enc;
                if (!strict) o["segment_band"] = Buckets.Count(s?.Segments ?? 0);
                if (ri.Col.TryGetValue(c, out long orphans))
                {
                    o["ri"] = orphans == 0 ? "RI_OK" : "RI_VIOLATION";
                    if (!strict) o["ri_count"] = Buckets.Count(orphans);
                }
                if (predicates.TryGetValue(c, out var flag)) o[PredicateKey(c)!] = flag;
                cols.Add(o);
            }
            var hiers = new JsonArray();
            foreach (var h in ByName(ids.Hierarchies.Values.Where(h => h.Table == t), h => h.Name))
            {
                long bytes = st.Hierarchies.GetValueOrDefault(h.Id);
                tableBytes += bytes;
                var o = new JsonObject { ["name"] = Named(h.Name) };
                if (!strict) o["size_band"] = Buckets.Size(bytes);
                hiers.Add(o);
            }
            foreach (var r in ids.Relationships.Values.Where(r => !r.Private && Home(st, r) == t))
                tableBytes += st.Relationships.GetValueOrDefault(r.Id);
            var to = new JsonObject { ["name"] = t.Name };
            if (!strict)
            {
                to["size_band"] = Buckets.Size(tableBytes);
                to["share_band"] = Buckets.StorageShare(tableBytes, total);
            }
            to["columns"] = cols;
            to["hierarchies"] = hiers;
            outTables.Add(to);
        }

        var rels = new JsonArray();
        foreach (var r in ByName(ids.Relationships.Values.Where(r => !r.Private), r => r.Name))
        {
            var o = new JsonObject { ["name"] = Named(r.Name) };
            if (r.From != null) o["from"] = r.From.Key;
            if (r.To != null) o["to"] = r.To.Key;
            if (!strict) o["size_band"] = Buckets.Size(st.Relationships.GetValueOrDefault(r.Id));
            if (ri.Rel.TryGetValue(r.Id, out long orphans))
            {
                o["ri"] = orphans == 0 ? "RI_OK" : "RI_VIOLATION";
                if (!strict) o["ri_count"] = Buckets.Count(orphans);
            }
            rels.Add(o);
        }

        var resp = new JsonObject();
        if (!strict) resp["model"] = new JsonObject { ["size_band"] = Buckets.Size(total) };
        resp["tables"] = outTables;
        resp["relationships"] = rels;
        if (!strict && st.Unattributed > 0) resp["other"] = Bands(st.Other, total);
        if (!strict && (st.Private > 0 || ids.Meta.Tables.Any(t => t.IsPrivate))) resp["auto_datetime"] = Bands(st.Private, total);
        resp["coverage"] = st.Unattributed == 0 && !st.Truncated ? "FULL" : "PARTIAL";
        resp["predicates"] = predicateState;
        return resp;
    }

    static TableMeta? Home(Storage st, RelInfo r) => r.From?.Table ?? st.RelationshipHome.GetValueOrDefault(r.Id);

    static JsonObject Bands(long bytes, long total) => new()
    {
        ["size_band"] = Buckets.Size(bytes),
        ["share_band"] = Buckets.StorageShare(bytes, total),
    };

    /// <summary>Name order only: no size ranking leaves the engine.</summary>
    static IEnumerable<T> ByName<T>(IEnumerable<T> items, Func<T, string> name)
        => items.OrderBy(name, StringComparer.OrdinalIgnoreCase).ThenBy(name, StringComparer.Ordinal);

    static string Named(string name) => name.Length == 0 ? "?" : name;

    // ------------------------------------------------------------------ conversions

    static long Bytes(object? v)
    {
        if (v == null || !Norm.IsNumeric(v)) return 0;
        double d = Norm.ToDouble(v);
        if (double.IsNaN(d) || d <= 0) return 0;
        return d >= 1e15 ? (long)1e15 : (long)d;
    }

    static ulong Id(string s) => ulong.TryParse(s, NumberStyles.None, CultureInfo.InvariantCulture, out var v) ? v : 0;

    static string S(object? v) => v == null ? "" : Convert.ToString(v, CultureInfo.InvariantCulture) ?? "";
    static ulong U(object? v) => v == null ? 0 : Convert.ToUInt64(v, CultureInfo.InvariantCulture);
    static int I(object? v) => v == null ? 0 : Convert.ToInt32(v, CultureInfo.InvariantCulture);
}
