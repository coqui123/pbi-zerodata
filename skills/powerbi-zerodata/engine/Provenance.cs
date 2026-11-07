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
