using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>Refresh times remain private. Publication is unknown until a source adapter establishes it.</summary>
public static class Provenance
{
    public static JsonObject Run(Req req, Ctx ctx)
    {
        using var conn = Conn.Open(ctx, req.Port());
        var limit = req.Int("max_age_hours") ?? 24;
        if (limit < 1 || limit > 8760) throw new BadRequestException("invalid max age");
        var records = new JsonArray();
        bool stale = false, unknown = false, any = false;
        var now = DateTime.UtcNow;
        foreach (var table in conn.Meta.UserTables)
        foreach (var partition in table.Partitions)
        {
            any = true;
            var refreshed = partition.Refreshed;
            var state = "UNKNOWN";
            if (refreshed.HasValue && refreshed.Value > DateTime.MinValue)
            {
                var utc = refreshed.Value.Kind == DateTimeKind.Unspecified
                    ? DateTime.SpecifyKind(refreshed.Value, DateTimeKind.Utc) : refreshed.Value.ToUniversalTime();
                var age = now - utc;
                state = age < TimeSpan.Zero ? "UNKNOWN" : age.TotalHours <= limit ? "FRESH" : "STALE";
            }
            stale |= state == "STALE";
            unknown |= state == "UNKNOWN";
            records.Add(new JsonObject { ["table"] = table.Name, ["partition"] = partition.Name,
                ["refreshed"] = refreshed?.ToString("O"), ["status"] = state });
        }
        var privateDoc = new JsonObject { ["version"] = 1, ["captured"] = now.ToString("O"), ["partitions"] = records };
        Vault.WriteProtected(Path.Combine(ctx.RequirePrivateDir(), "refresh-provenance.bin"), privateDoc.ToJsonString());
        return Ctx.Ok("provenance", new JsonObject {
            ["refresh"] = stale ? "STALE" : unknown || !any ? "UNKNOWN" : "FRESH",
            ["source_publication"] = "UNKNOWN", ["coverage"] = unknown || !any ? "PARTIAL" : "COMPLETE",
            ["note"] = "Refresh does not certify source publication. Raw timestamps remain in protected storage."
        });
    }
}

/// <summary>Private, signed definition manifests. No row output is allowed without one.</summary>
public static class SyntheticProvenance
{
    const string Marker = "PBZ_SYNTHETIC";

    // TOM serializes definitions, including expressions and dynamic format strings. Keep the
    // entire definition of each object rather than maintaining an expression-only allowlist.
    // These are engine-maintained status fields, never executable model definitions.
    static readonly HashSet<string> RuntimeFields = new(StringComparer.Ordinal)
        { "modifiedTime", "structureModifiedTime", "refreshedTime", "state", "errorMessage" };

    static JsonNode? Canonical(JsonNode? node)
    {
        if (node is JsonObject obj)
        {
            var result = new JsonObject();
            foreach (var entry in obj.OrderBy(e => e.Key, StringComparer.Ordinal))
                if (!RuntimeFields.Contains(entry.Key)) result[entry.Key] = Canonical(entry.Value);
            return result;
        }
        if (node is JsonArray array)
        {
            var result = new JsonArray();
            // Named TOM collections are unordered; other arrays may encode ordered definitions.
            var values = array.Where(n => n is not JsonObject o || o["type"]?.ToString() != "rowNumber").ToList();
            if (values.All(n => n is JsonObject o && o["name"] != null))
                values = values.OrderBy(n => n!["name"]!.ToString(), StringComparer.Ordinal).ToList();
            foreach (var value in values) result.Add(Canonical(value));
            return result;
        }
        return node?.DeepClone();
    }

    public static string Definition(Microsoft.AnalysisServices.Tabular.Model model)
    {
        foreach (var table in model.Tables) // Includes private, private-prefixed and untagged tables.
        {
            if (table.Columns.Count(c => c.Type == Microsoft.AnalysisServices.Tabular.ColumnType.RowNumber) > 1)
                throw new BadRequestException("SYNTH_DEFINITION_CHANGED: more than one RowNumber column in a table");
            if (table.Partitions.Count == 0 || table.Partitions.Any(p =>
                p.Source is not Microsoft.AnalysisServices.Tabular.CalculatedPartitionSource
                && p.Source is not Microsoft.AnalysisServices.Tabular.CalculationGroupSource))
                throw new BadRequestException("SYNTH_DATA_PARTITION: every table must have only calculated partitions");
        }
        var json = JsonNode.Parse(Microsoft.AnalysisServices.Tabular.JsonSerializer.SerializeObject(model,
            new Microsoft.AnalysisServices.Tabular.SerializeOptions {
                IncludeRestrictedInformation = true, IgnoreChildren = false,
                IgnoreInferredObjects = false, IgnoreInferredProperties = true, IgnoreTimestamps = true
            }))!.AsObject();
        // Identity is checked separately against the catalog pinned at synth-open. Compatibility
        // level and other Desktop model defaults are not part of the executable object definition.
        if ((json["tables"] as JsonArray)?.Count != model.Tables.Count)
            throw new BadRequestException("SYNTH_VERIFICATION_UNAVAILABLE: incomplete table serialization");
        var definition = new JsonObject { ["version"] = 2 };
        foreach (var name in new[] { "tables", "relationships", "roles", "expressions", "functions", "cultures", "perspectives" })
            definition[name] = Canonical(json[name]);
        return definition.ToJsonString();
    }

    static string PathFor(Ctx ctx, string marker)
    {
        if (marker.Length != 64 || marker.Any(c => !Uri.IsHexDigit(c)))
            throw new BadRequestException("SYNTH_NO_MARKER: valid marker required");
        return Path.Combine(ctx.RequirePrivateDir(), "synth-manifest-" + marker + ".bin");
    }

    static string Signature(Vault vault, string marker, string definition, string? catalog)
        => vault.HmacOf(new JsonArray("pbz-synthetic-manifest-v2", marker, definition, catalog).ToJsonString());

    public static void Save(Ctx ctx, string marker, string definition, string? catalog = null)
    {
        var vault = Vault.Open(ctx.RequirePrivateDir());
        Vault.WriteProtected(PathFor(ctx, marker), new JsonObject {
            ["marker"] = marker, ["definition"] = definition, ["catalog"] = catalog,
            ["signature"] = Signature(vault, marker, definition, catalog)
        }.ToJsonString());
    }

    public static JsonObject Load(Ctx ctx, string marker)
    {
        try
        {
            var doc = JsonNode.Parse(System.Text.Encoding.UTF8.GetString(
                Dpapi.Unprotect(File.ReadAllBytes(PathFor(ctx, marker)))))!.AsObject();
            var expected = Signature(Vault.Open(ctx.RequirePrivateDir()), marker,
                doc["definition"]!.GetValue<string>(), doc["catalog"]?.GetValue<string>());
            if (doc["marker"]?.GetValue<string>() != marker || !System.Security.Cryptography.CryptographicOperations.FixedTimeEquals(
                System.Text.Encoding.UTF8.GetBytes(expected), System.Text.Encoding.UTF8.GetBytes(doc["signature"]!.GetValue<string>())))
                throw new BadRequestException("SYNTH_MANIFEST_SIGNATURE: manifest does not verify");
            return doc;
        }
        catch (BadRequestException) { throw; }
        catch (Exception ex) when (ex is not TimeoutException)
        { throw new BadRequestException("SYNTH_VERIFICATION_UNAVAILABLE: trusted manifest unavailable"); }
    }

    public sealed record Snapshot(string MarkerValue, string Catalog, string DefinitionValue, string Version);

    static string ModelVersion(Conn conn)
    {
        var rows = conn.Dmv("TMSCHEMA_MODEL", new[] { "ModifiedTime", "StructureModifiedTime" });
        if (rows.Count != 1 || rows[0].Length != 2 || rows[0].Any(v => v is not DateTime time || time == DateTime.MinValue))
            throw new BadRequestException("SYNTH_VERIFICATION_UNAVAILABLE: model version unavailable");
        var version = new JsonArray();
        foreach (var value in rows[0])
            version.Add(((DateTime)value!).ToString("O", System.Globalization.CultureInfo.InvariantCulture));
        return version.ToJsonString();
    }

    public static Snapshot Capture(Req req, Ctx ctx, Conn conn)
    {
        try
        {
            // Read independently on each side of the query; never reuse Conn.Meta or cached TOM.
            var version = ModelVersion(conn);
            using var server = new Microsoft.AnalysisServices.Tabular.Server();
            ctx.Guard(() => { server.Connect("Data Source=localhost:" + req.Port() + ";"); return 0; });
            var db = server.Databases.Cast<Microsoft.AnalysisServices.Tabular.Database>().Single();
            var marker = db.Model.Annotations.Find(Marker)?.Value ?? "";
            var definition = Definition(db.Model);
            if (db.Name != conn.Meta.DatabaseName || version != ModelVersion(conn))
                throw new BadRequestException("SYNTH_MODEL_CHANGED_DURING_QUERY: model changed during capture");
            var catalog = new JsonArray(req.Port(), db.ID).ToJsonString();
            return new Snapshot(marker, catalog, definition, version);
        }
        catch (BadRequestException) { throw; }
        catch (Exception ex) when (ex is not TimeoutException)
        { throw new BadRequestException("SYNTH_VERIFICATION_UNAVAILABLE: live definition unavailable"); }
    }

    public static T Buffered<T>(Func<Snapshot> capture, Action<Snapshot> verify, Func<T> query)
    {
        var before = capture();
        verify(before);
        var rows = query();
        var after = capture();
        verify(after);
        if (before != after)
            throw new BadRequestException("SYNTH_MODEL_CHANGED_DURING_QUERY: buffered rows discarded");
        return rows;
    }

    public static readonly HashSet<string> Reasons = new(StringComparer.Ordinal) {
        "SYNTH_DATA_PARTITION", "SYNTH_NO_MARKER", "SYNTH_MANIFEST_SIGNATURE",
        "SYNTH_VERIFICATION_UNAVAILABLE", "SYNTH_DEFINITION_CHANGED", "SYNTH_CATALOG_MISMATCH",
        "SYNTH_MARKER_MISMATCH", "SYNTH_MODEL_CHANGED_DURING_QUERY"
    };

    public static void Verify(Ctx ctx, Snapshot snapshot, bool requireCatalog = true)
    {
        var trusted = Load(ctx, snapshot.MarkerValue);
        var definition = trusted["definition"]!.GetValue<string>();
        if (definition != snapshot.DefinitionValue)
            throw new BadRequestException("SYNTH_DEFINITION_CHANGED: live model differs from signed build");
        if (requireCatalog && trusted["catalog"]?.GetValue<string>() != snapshot.Catalog)
            throw new BadRequestException("SYNTH_CATALOG_MISMATCH: model is not the registered Desktop catalog");
    }

    // Called only by synth-open after Python locates the Desktop instance holding the owned PBIP.
    public static JsonObject Bind(Req req, Ctx ctx)
    {
        using var conn = Conn.Open(ctx, req.Port());
        var snapshot = Capture(req, ctx, conn);
        if (snapshot.MarkerValue != req.Str("marker"))
            throw new BadRequestException("SYNTH_MARKER_MISMATCH: opened model is not the requested build");
        Verify(ctx, snapshot, requireCatalog: false);
        Save(ctx, snapshot.MarkerValue, snapshot.DefinitionValue, snapshot.Catalog);
        return Ctx.Ok("synth-bind", new JsonObject { ["verified"] = true });
    }
}
