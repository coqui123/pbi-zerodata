using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>
/// health: object counts and object states from TMSCHEMA_* only.
/// Only measure errors carry a (scrubbed) message; column, partition, hierarchy and relationship errors can
/// quote data values (refresh and evaluation errors), so they carry the class alone.
/// </summary>
public static class Health
{
    const int Ready = 1, NoData = 3, CalculationNeeded = 4, SemanticError = 5, EvaluationError = 6, DependencyError = 7, Incomplete = 8, ForceCalculationNeeded = 10;

    static bool IsErrorState(int s) => s is SemanticError or EvaluationError or DependencyError;

    static ErrorClass ClassOf(int state, string? error)
    {
        var cls = Errors.ClassifyMessage(error);
        if (cls == ErrorClass.OTHER && state == DependencyError && string.IsNullOrWhiteSpace(error)) return ErrorClass.UNKNOWN_OBJECT;
        return cls;
    }

    public static JsonObject Run(Req req, Ctx ctx)
    {
        int port = req.Port();
        using var conn = Conn.Open(ctx, port);
        var meta = conn.Meta;
        var scrub = new Scrubber(meta.KnownNames());
        var tables = meta.UserTables.ToList();

        var errors = new JsonArray();
        var unprocessed = new JsonArray();
        int columns = 0, measures = 0;
        foreach (var t in tables)
        {
            columns += t.Columns.Count;
            measures += t.Measures.Count;
            foreach (var m in t.Measures.Where(m => IsErrorState(m.State) || !string.IsNullOrWhiteSpace(m.Error)))
            {
                errors.Add(new JsonObject
                {
                    ["kind"] = "measure",
                    ["table"] = t.Name,
                    ["name"] = m.Name,
                    ["class"] = ClassOf(m.State, m.Error).ToString(),
                    ["message"] = scrub.Scrub(m.Error),
                });
            }
            foreach (var c in t.Columns.Where(c => IsErrorState(c.State) || !string.IsNullOrWhiteSpace(c.Error)))
            {
                errors.Add(new JsonObject
                {
                    ["kind"] = "column",
                    ["table"] = t.Name,
                    ["name"] = c.Name,
                    ["class"] = ClassOf(c.State, c.Error).ToString(),
                });
            }
            foreach (var p in t.Partitions.Where(p => IsErrorState(p.State) || !string.IsNullOrWhiteSpace(p.Error)))
            {
                errors.Add(new JsonObject
                {
                    ["kind"] = "partition",
                    ["table"] = t.Name,
                    ["name"] = p.Name,
                    ["class"] = ClassOf(p.State, p.Error).ToString(),
                });
            }
            foreach (var h in t.HierarchyStates.Where(h => IsErrorState(h.State)))
            {
                errors.Add(new JsonObject
                {
                    ["kind"] = "hierarchy",
                    ["table"] = t.Name,
                    ["name"] = h.Name,
                    ["class"] = ClassOf(h.State, null).ToString(),
                });
            }
            if (t.Partitions.Any(p => p.State is NoData or CalculationNeeded or Incomplete or ForceCalculationNeeded))
                unprocessed.Add(t.Name);
        }
        var userTableSet = new HashSet<TableMeta>(tables);
        var rels = meta.Relationships.Where(r => r.From == null || r.To == null || (userTableSet.Contains(r.From.Table) && userTableSet.Contains(r.To.Table))).ToList();
        foreach (var r in rels.Where(r => IsErrorState(r.State)))
        {
            errors.Add(new JsonObject
            {
                ["kind"] = "relationship",
                ["table"] = r.From?.Table.Name ?? "",
                ["name"] = r.Name,
                ["class"] = ClassOf(r.State, null).ToString(),
            });
        }

        return Ctx.Ok("health", new JsonObject
        {
            ["objects"] = new JsonObject
            {
                ["tables"] = tables.Count,
                ["columns"] = columns,
                ["measures"] = measures,
                ["relationships"] = rels.Count,
            },
            ["errors"] = errors,
            ["unprocessed_tables"] = unprocessed,
        });
    }
}
