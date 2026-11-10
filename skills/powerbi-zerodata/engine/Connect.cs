using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.AnalysisServices.AdomdClient;

namespace PbzEngine;

public sealed class TableMeta
{
    public ulong Id;
    public string Name = "";
    public bool IsHidden;
    public bool IsPrivate;
    public readonly List<ColumnMeta> Columns = new();
    public readonly List<MeasureMeta> Measures = new();
    public readonly List<PartitionMeta> Partitions = new();
    public readonly List<string> Hierarchies = new();
    public readonly List<(string Name, int State)> HierarchyStates = new();
}

public sealed class ColumnMeta
{
    public ulong Id;
    public TableMeta Table = null!;
    public string Name = "";
    public int DataType;     // TOM DataType enum value
    public int Type;         // 1 Data, 2 Calculated, 3 RowNumber, 4 CalculatedTableColumn
    public int State;
    public string? Error;    // internal only, never printed
    public bool IsHidden;

    public string Key => Table.Name + "[" + Name + "]";
    public string Ref => Dax.QCol(Table.Name, Name);
    public bool IsText => DataType == 2;
    public bool IsNumeric => DataType is 6 or 8 or 10;
    public bool IsDate => DataType == 9;
    public bool IsBinary => DataType == 17;

    public string TypeName => DataType switch
    {
        2 => "string",
        6 => "int64",
        8 => "double",
        9 => "dateTime",
        10 => "decimal",
        11 => "boolean",
        17 => "binary",
        20 => "variant",
        _ => "unknown",
    };

    /// <summary>Predicate (for CALCULATE or a row context) that is true when the column value is blank.</summary>
    public string BlankPredicate => IsText ? Ref + " = \"\"" : "ISBLANK(" + Ref + ")";

    public string NonBlankPredicate => IsText ? Ref + " <> \"\"" : "NOT ISBLANK(" + Ref + ")";
}

public sealed class MeasureMeta
{
    public ulong Id;
    public TableMeta Table = null!;
    public string Name = "";
    public int State;
    public string? Error;
}

public sealed class PartitionMeta
{
    public string Name = "";
    public int State;
    public string? Error;
    public DateTime? Refreshed;
    public int SourceType;   // TOM PartitionSourceType: 2 Calculated, 4 M, 7 CalculationGroup, ...
}

public sealed class RelMeta
{
    public string Name = "";
    public ColumnMeta? From;
    public ColumnMeta? To;
    public int State;
    public bool IsActive;
}

/// <summary>Model metadata read only from $SYSTEM.TMSCHEMA_* (never from storage DMVs).</summary>
public sealed class Meta
{
    public string DatabaseName = "";
    public string DatabaseId = "";
    public long CompatLevel;
    public readonly List<TableMeta> Tables = new();
    public readonly List<RelMeta> Relationships = new();

    /// <summary>User-facing tables (auto date/time and other private tables excluded).</summary>
    public IEnumerable<TableMeta> UserTables => Tables.Where(t => !t.IsPrivate);

    public IEnumerable<string> KnownNames()
    {
        foreach (var t in Tables)
        {
            yield return t.Name;
            foreach (var c in t.Columns) yield return c.Name;
            foreach (var m in t.Measures) yield return m.Name;
            foreach (var h in t.Hierarchies) yield return h;
        }
    }

    public TableMeta? FindTable(string name)
        => Tables.FirstOrDefault(t => string.Equals(t.Name, name, StringComparison.Ordinal))
           ?? Tables.FirstOrDefault(t => string.Equals(t.Name, name, StringComparison.OrdinalIgnoreCase));

    public ColumnMeta? FindColumn(string table, string column)
    {
        var t = FindTable(table);
        if (t == null) return null;
        return t.Columns.FirstOrDefault(c => string.Equals(c.Name, column, StringComparison.Ordinal))
               ?? t.Columns.FirstOrDefault(c => string.Equals(c.Name, column, StringComparison.OrdinalIgnoreCase));
    }

    /// <summary>Resolves "'T'[C]" / "T[C]" against the model; unknown -> UNKNOWN_OBJECT, unparsable -> BAD_REQUEST.</summary>
    public ColumnMeta ResolveColumn(string colRef)
    {
        var parsed = Dax.ParseColRef(colRef) ?? throw new BadRequestException("not a column reference");
        return FindColumn(parsed.Table, parsed.Column)
               ?? throw new EngineException(ErrorClass.UNKNOWN_OBJECT, "column not found in model metadata");
    }
}

/// <summary>One ADOMD connection to the local Desktop engine plus its metadata.</summary>
public sealed class Conn : IDisposable
{
    readonly Ctx _ctx;
    readonly int _port;
    readonly string? _roles;
    readonly string? _user;
    AdomdConnection? _c;
    public Meta Meta { get; private set; } = new();
    public int Port => _port;

    Conn(Ctx ctx, int port, string? roles, string? user)
    {
        _ctx = ctx;
        _port = port;
        _roles = roles;
        _user = user;
    }

    /// <summary>Rejects connection-string breakers. Existing callers pass neither value.</summary>
    public static void RejectIdentity(string? roles, string? effectiveUserName)
    {
        RejectToken(roles, "roles");
        RejectToken(effectiveUserName, "effective_user");
    }

    static void RejectToken(string? value, string field)
    {
        if (string.IsNullOrEmpty(value)) return;
        if (value.IndexOfAny(new[] { ';', '"', '\'' }) >= 0)
            throw new BadRequestException(field + " contains a character that is not allowed");
    }

    /// <summary>The "connect logic": open, require exactly one catalog, load TMSCHEMA metadata.</summary>
    public static Conn Open(Ctx ctx, int port, string? roles = null, string? effectiveUserName = null)
    {
        RejectIdentity(roles, effectiveUserName);
        if (string.IsNullOrWhiteSpace(roles)) roles = null;
        if (string.IsNullOrWhiteSpace(effectiveUserName)) effectiveUserName = null;
        var conn = new Conn(ctx, port, roles, effectiveUserName);
        try
        {
            conn.Reopen();
            conn.Meta = conn.LoadMeta();
            return conn;
        }
        catch (Exception ex)
        {
            conn.Dispose();
            if (ex is EngineException or BadRequestException or TimeoutException) throw;
            throw new EngineException(Errors.Classify(ex) == ErrorClass.TIMEOUT ? ErrorClass.TIMEOUT : ErrorClass.ENGINE_UNAVAILABLE,
                "connect failed", ex);
        }
    }

    string ConnectionString()
    {
        var sb = new StringBuilder();
        sb.Append("Data Source=localhost:");
        sb.Append(_port.ToString(CultureInfo.InvariantCulture));
        sb.Append(";Connect Timeout=");
        sb.Append(Math.Min(_ctx.TimeoutSec, 60).ToString(CultureInfo.InvariantCulture));
        if (!string.IsNullOrEmpty(_roles))
        {
            sb.Append(";Roles=");
            sb.Append(_roles);
        }
        if (!string.IsNullOrEmpty(_user))
        {
            sb.Append(";EffectiveUserName=");
            sb.Append(_user);
        }
        return sb.ToString();
    }

    void Reopen()
    {
        var old = _c;
        _c = null;
        if (old != null) Bounded.Run(() => old.Dispose());
        var c = new AdomdConnection(ConnectionString());
        try
        {
            _ctx.Guard(() => { c.Open(); return 0; }, Math.Min(_ctx.TimeoutSec, 60));
        }
        catch
        {
            Bounded.Run(() => { try { c.Close(); } finally { c.Dispose(); } });
            throw;
        }
        _c = c;
        var cats = Dmv("DBSCHEMA_CATALOGS", new[] { "CATALOG_NAME", "COMPATIBILITY_LEVEL" });
        if (cats.Count != 1)
            throw new EngineException(ErrorClass.ENGINE_UNAVAILABLE, "expected exactly one catalog (Desktop instance)");
        string name = Convert.ToString(cats[0][0], CultureInfo.InvariantCulture) ?? "";
        if (name.Length == 0) throw new EngineException(ErrorClass.ENGINE_UNAVAILABLE, "catalog without a name");
        c.ChangeDatabase(name);
        Meta.DatabaseName = name;
        Meta.DatabaseId = name;
        Meta.CompatLevel = cats[0][1] == null || cats[0][1] is DBNull ? 0 : Convert.ToInt64(cats[0][1], CultureInfo.InvariantCulture);
    }

    /// <summary>After a timeout the session may still be busy, so the next command uses a fresh connection.</summary>
    public void Reset()
    {
        var keep = Meta;
        Reopen();
        keep.DatabaseName = Meta.DatabaseName;
        Meta = keep;
    }

    public AdomdConnection Raw => _c ?? throw new EngineException(ErrorClass.ENGINE_UNAVAILABLE, "not connected");

    /// <summary>Only $SYSTEM.TMSCHEMA_* and DBSCHEMA_CATALOGS may ever be queried (storage DMVs leak counts).</summary>
    public List<object?[]> Dmv(string dmv, string[] columns)
    {
        if (!(dmv.StartsWith("TMSCHEMA_", StringComparison.Ordinal) || dmv == "DBSCHEMA_CATALOGS"))
            throw new InvalidOperationException("DMV not allowed");
        string text = "SELECT " + string.Join(", ", columns.Select(c => "[" + c + "]")) + " FROM $SYSTEM." + dmv;
        return Rows(text, int.MaxValue);
    }

    /// <summary>
    /// The memory verb's narrow exception to the rule above: exactly two storage DMVs, each read with a fixed column
    /// list (storage identity plus one size). Callers cannot choose DMVs or columns. COLUMN_ENCODING is deliberately
    /// not in this allowlist; <see cref="TryColumnEncoding"/> is a separate query that may fail. Sizes stay inside the
    /// process; the memory verb releases fixed bands only (CONTRACT.md v1.4).
    /// </summary>
    static readonly Dictionary<string, string[]> StorageColumns = new(StringComparer.Ordinal)
    {
        ["DISCOVER_STORAGE_TABLE_COLUMNS"] = new[] { "DIMENSION_NAME", "TABLE_ID", "COLUMN_ID", "COLUMN_TYPE", "DICTIONARY_SIZE" },
        ["DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS"] = new[] { "DIMENSION_NAME", "TABLE_ID", "COLUMN_ID", "USED_SIZE" },
    };

    public List<object?[]> StorageDmv(string dmv, int maxRows)
    {
        if (!StorageColumns.TryGetValue(dmv, out var columns)) throw new InvalidOperationException("storage DMV not allowed");
        return Rows("SELECT " + string.Join(", ", columns.Select(c => "[" + c + "]")) + " FROM $SYSTEM." + dmv, maxRows);
    }

    /// <summary>
    /// Separate from <see cref="StorageColumns"/>. Reads COLUMN_ENCODING only. A missing column or an engine that
    /// rejects the query returns null so the caller can omit the field. Timeout still propagates.
    /// </summary>
    public List<object?[]>? TryColumnEncoding(int maxRows)
    {
        try
        {
            return Rows(
                "SELECT [DIMENSION_NAME], [TABLE_ID], [COLUMN_ID], [COLUMN_ENCODING] FROM $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMNS",
                maxRows);
        }
        catch (Exception ex) when (Errors.Classify(ex) != ErrorClass.TIMEOUT)
        {
            return null;
        }
    }

    /// <summary>Runs a statement and discards every cell. The row count is not returned.</summary>
    public void Drain(string text)
    {
        var c = Raw;
        _ctx.Guard(() =>
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = text;
            cmd.CommandTimeout = _ctx.TimeoutSec;
            using var r = cmd.ExecuteReader();
            while (r.Read()) { }
            return 0;
        });
    }

    /// <summary>Counts result rows without keeping cell values.</summary>
    public long CountRows(string text)
    {
        var c = Raw;
        return _ctx.Guard(() =>
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = text;
            cmd.CommandTimeout = _ctx.TimeoutSec;
            using var r = cmd.ExecuteReader();
            long n = 0;
            while (r.Read()) n++;
            return n;
        });
    }

    /// <summary>Runs a statement and returns rows (values stay inside the process).</summary>
    public List<object?[]> Rows(string text, int maxRows)
    {
        var c = Raw;
        return _ctx.Guard(() =>
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = text;
            cmd.CommandTimeout = _ctx.TimeoutSec;
            using var r = cmd.ExecuteReader();
            var rows = new List<object?[]>();
            while (rows.Count < maxRows && r.Read())
            {
                var row = new object?[r.FieldCount];
                for (int i = 0; i < r.FieldCount; i++)
                {
                    object? v = r.GetValue(i);
                    row[i] = v is DBNull ? null : v;
                }
                rows.Add(row);
            }
            return rows;
        });
    }

    /// <summary>Runs a query and returns column names plus rows.</summary>
    public (List<string> Columns, List<object?[]> Rows) Table(string text, int maxRows = int.MaxValue)
    {
        var c = Raw;
        return _ctx.Guard(() =>
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = text;
            cmd.CommandTimeout = _ctx.TimeoutSec;
            using var r = cmd.ExecuteReader();
            var cols = Enumerable.Range(0, r.FieldCount).Select(r.GetName).ToList();
            var rows = new List<object?[]>();
            while (rows.Count < maxRows && r.Read())
            {
                var row = new object?[r.FieldCount];
                for (int i = 0; i < r.FieldCount; i++)
                {
                    object? v = r.GetValue(i);
                    row[i] = v is DBNull ? null : v;
                }
                rows.Add(row);
            }
            return (cols, rows);
        });
    }

    /// <summary>First row of a query (EVALUATE ROW(...)); throws when the query returns no row.</summary>
    public object?[] Row1(string text)
    {
        var rows = Rows(text, 1);
        if (rows.Count == 0) throw new EngineException(ErrorClass.OTHER, "query returned no row");
        return rows[0];
    }

    Meta LoadMeta()
    {
        var meta = new Meta { DatabaseName = Meta.DatabaseName, DatabaseId = Meta.DatabaseId, CompatLevel = Meta.CompatLevel };
        var byId = new Dictionary<ulong, TableMeta>();
        foreach (var r in Dmv("TMSCHEMA_TABLES", new[] { "ID", "Name", "IsHidden", "IsPrivate", "ShowAsVariationsOnly" }))
        {
            var t = new TableMeta
            {
                Id = U(r[0]),
                Name = S(r[1]),
                IsHidden = B(r[2]),
            };
            t.IsPrivate = B(r[3]) || B(r[4])
                          || t.Name.StartsWith("LocalDateTable_", StringComparison.Ordinal)
                          || t.Name.StartsWith("DateTableTemplate_", StringComparison.Ordinal);
            meta.Tables.Add(t);
            byId[t.Id] = t;
        }
        var colById = new Dictionary<ulong, ColumnMeta>();
        foreach (var r in Dmv("TMSCHEMA_COLUMNS", new[] { "ID", "TableID", "ExplicitName", "InferredName", "ExplicitDataType", "InferredDataType", "Type", "State", "ErrorMessage", "IsHidden" }))
        {
            if (!byId.TryGetValue(U(r[1]), out var t)) continue;
            int type = I(r[6]);
            if (type == 3) continue; // RowNumber
            int dt = I(r[4]);
            if (dt == 1 || dt == 0) dt = I(r[5]);
            string name = S(r[2]);
            if (name.Length == 0) name = S(r[3]);
            var c = new ColumnMeta
            {
                Id = U(r[0]), Table = t, Name = name, DataType = dt, Type = type, State = I(r[7]),
                Error = r[8] as string, IsHidden = B(r[9]),
            };
            t.Columns.Add(c);
            colById[c.Id] = c;
        }
        foreach (var r in Dmv("TMSCHEMA_MEASURES", new[] { "ID", "TableID", "Name", "State", "ErrorMessage" }))
        {
            if (!byId.TryGetValue(U(r[1]), out var t)) continue;
            t.Measures.Add(new MeasureMeta { Id = U(r[0]), Table = t, Name = S(r[2]), State = I(r[3]), Error = r[4] as string });
        }
        foreach (var r in Dmv("TMSCHEMA_PARTITIONS", new[] { "TableID", "Name", "State", "ErrorMessage", "RefreshedTime", "Type" }))
        {
            if (!byId.TryGetValue(U(r[0]), out var t)) continue;
            t.Partitions.Add(new PartitionMeta
            {
                Name = S(r[1]), State = I(r[2]), Error = r[3] as string,
                Refreshed = r[4] is DateTime d ? d : null,
                SourceType = I(r[5]),
            });
        }
        foreach (var r in Dmv("TMSCHEMA_HIERARCHIES", new[] { "TableID", "Name", "State" }))
        {
            if (!byId.TryGetValue(U(r[0]), out var t)) continue;
            t.Hierarchies.Add(S(r[1]));
            t.HierarchyStates.Add((S(r[1]), I(r[2])));
        }
        foreach (var r in Dmv("TMSCHEMA_RELATIONSHIPS", new[] { "Name", "FromColumnID", "ToColumnID", "State", "IsActive" }))
        {
            meta.Relationships.Add(new RelMeta
            {
                Name = S(r[0]),
                From = colById.TryGetValue(U(r[1]), out var f) ? f : null,
                To = colById.TryGetValue(U(r[2]), out var to) ? to : null,
                State = I(r[3]),
                IsActive = B(r[4]),
            });
        }
        return meta;
    }

    /// <summary>Latest partition refresh time (stored in snapshots, never printed).</summary>
    public string LatestRefresh()
    {
        DateTime max = DateTime.MinValue;
        foreach (var r in Dmv("TMSCHEMA_PARTITIONS", new[] { "RefreshedTime" }))
            if (r[0] is DateTime d && d > max) max = d;
        return max.ToString("o", CultureInfo.InvariantCulture);
    }

    static string S(object? v) => v == null ? "" : Convert.ToString(v, CultureInfo.InvariantCulture) ?? "";
    static ulong U(object? v) => v == null ? 0 : Convert.ToUInt64(v, CultureInfo.InvariantCulture);
    static int I(object? v) => v == null ? 0 : Convert.ToInt32(v, CultureInfo.InvariantCulture);
    static bool B(object? v) => v is bool b && b;

    public void Dispose()
    {
        var c = _c;
        _c = null;
        if (c != null) Bounded.Run(() => { c.Close(); c.Dispose(); });
    }
}

public static class ConnectVerb
{
    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        using var conn = Conn.Open(ctx, port);
        var tables = new JsonArray();
        foreach (var t in conn.Meta.UserTables) tables.Add(t.Name);
        return Ctx.Ok("connect", new JsonObject
        {
            ["database_id"] = conn.Meta.DatabaseId,
            ["compat_level"] = conn.Meta.CompatLevel,
            ["tables"] = tables,
        });
    }
}
