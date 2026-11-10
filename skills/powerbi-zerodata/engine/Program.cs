using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace PbzEngine;

/// <summary>
/// pbz-engine entry point. Reads one JSON request on stdin, writes one JSON response on stdout.
/// stdout is a closed vocabulary (see CONTRACT.md); stderr is never used.
/// </summary>
public static class Program
{
    public const string ContractVersion = "1";

    static readonly object EmitLock = new();
    static bool _emitted;
    static Stream? _stdout;

    public static int Main(string[] args)
    {
        // Nothing may reach stderr or stdout except the single response we build ourselves.
        _stdout = Console.OpenStandardOutput();
        Console.SetError(TextWriter.Null);
        Console.SetOut(TextWriter.Null);

        string verb = args.Length > 0 ? args[0].Trim() : "";
        // the verb is echoed in the envelope, so anything outside the safe charset is replaced
        if (!System.Text.RegularExpressions.Regex.IsMatch(verb, "^[A-Za-z0-9_\\-]{0,64}$")) verb = "invalid";
        var ctx = new Ctx(verb);
        JsonObject response;
        try
        {
            string input;
            using (var sr = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false)))
                input = sr.ReadToEnd();
            var req = Req.Parse(input);
            ctx.Init(req);
            response = Dispatch(verb, req, ctx);
        }
        catch (Exception ex)
        {
            response = ctx.Fail(ex);
        }
        int rc = Emit(response) ? 0 : 1;
        // abandoned work (a timed-out command still waiting on the engine) must not keep the process alive
        Environment.Exit(rc);
        return rc;
    }

    static JsonObject Dispatch(string verb, Req req, Ctx ctx) => verb switch
    {
        "ping" => Ctx.Ok("ping", new JsonObject { ["version"] = ContractVersion }),
        "connect" => ConnectVerb.Run(req, ctx),
        "health" => Health.Run(req, ctx),
        "provenance" => Provenance.Run(req, ctx),
        "tmdl-validate" => TmdlValidate.Run(req, ctx),
        "refresh" => Refresh.Run(req, ctx),
        "compiles" => Compiles.Run(req, ctx),
        "index" => ValueIndex.Run(req, ctx),
        "check" => Checks.Run(req, ctx),
        "probe" => Probe.Run(req, ctx),
        "snapshot-save" => Snapshots.Save(req, ctx),
        "snapshot-compare" => Snapshots.Compare(req, ctx),
        "smoke" => Smoke.Run(req, ctx),
        "deps" => Deps.Run(req, ctx),
        "synth-query" => SynthQuery.Run(req, ctx),
        "memory" => Memory.Run(req, ctx),
        "perf" => Perf.Run(req, ctx),
        _ => throw new BadRequestException("unknown verb"),
    };

    static readonly JsonSerializerOptions OutOptions = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        WriteIndented = false,
    };

    /// <summary>Writes the response exactly once. Also used by the timeout watchdog.</summary>
    public static bool Emit(JsonObject response)
    {
        lock (EmitLock)
        {
            if (_emitted) return true;
            try
            {
                var bytes = Encoding.UTF8.GetBytes(response.ToJsonString(OutOptions));
                _stdout ??= Console.OpenStandardOutput();
                _stdout.Write(bytes, 0, bytes.Length);
                _stdout.Flush();
                _emitted = true;
                return true;
            }
            catch
            {
                return false;
            }
        }
    }
}

/// <summary>Per-invocation context: verb, private dir, tier, timeouts, error envelope.</summary>
public sealed class Ctx
{
    public string Verb { get; }
    public string? PrivateDir { get; private set; }
    public int TimeoutSec { get; private set; } = 60;
    public bool Strict { get; private set; }

    public Ctx(string verb) { Verb = verb; }

    public void Init(Req req)
    {
        string? pd = req.Str("private_dir");
        if (pd == null)
        {
            if (Verb != "ping") throw new BadRequestException("private_dir is required");
        }
        else
        {
            if (!Path.IsPathFullyQualified(pd)) throw new BadRequestException("private_dir must be an absolute path");
            pd = Path.GetFullPath(pd);
            Directory.CreateDirectory(pd);
            Directory.CreateDirectory(Path.Combine(pd, "logs"));
            Directory.CreateDirectory(Path.Combine(pd, "snapshots"));
            PrivateDir = pd;
        }
        int? t = req.Int("timeout_s");
        if (t != null)
        {
            if (t < 1 || t > 3600) throw new BadRequestException("timeout_s out of range");
            TimeoutSec = t.Value;
        }
        string disclosure = req.Str("disclosure") ?? "shape";
        Strict = disclosure switch
        {
            "shape" => false,
            "strict" => true,
            _ => throw new BadRequestException("disclosure must be shape or strict"),
        };
    }

    public string RequirePrivateDir() => PrivateDir ?? throw new BadRequestException("private_dir is required");

    public static JsonObject Ok(string verb, JsonObject fields)
    {
        var o = new JsonObject { ["ok"] = true, ["verb"] = verb };
        foreach (var kv in fields.ToList())
        {
            fields.Remove(kv.Key);
            o[kv.Key] = kv.Value;
        }
        return o;
    }

    /// <summary>Error envelope: class + log id only. Raw text goes to the DPAPI log.</summary>
    public JsonObject Fail(Exception ex)
    {
        var cls = Errors.Classify(ex);
        var o = new JsonObject { ["ok"] = false, ["verb"] = Verb, ["class"] = cls.ToString() };
        string? id = Log(ex, null);
        if (id != null) o["log_id"] = id;
        return o;
    }

    public string? Log(Exception ex, string? item)
    {
        var sb = new StringBuilder();
        sb.Append("verb=").Append(Verb);
        if (item != null) sb.Append(" item=").Append(item);
        sb.Append('\n').Append(ex.ToString());
        return EngineLog.Write(PrivateDir, sb.ToString());
    }

    public string? LogText(string text) => EngineLog.Write(PrivateDir, "verb=" + Verb + "\n" + text);

    /// <summary>Runs work with a hard deadline. On expiry throws TimeoutException (the abandoned work dies with the process).</summary>
    public T Guard<T>(Func<T> work, int? seconds = null)
    {
        int limit = (seconds ?? TimeoutSec) + 10;
        var task = Task.Run(work);
        try
        {
            if (!task.Wait(TimeSpan.FromSeconds(limit))) throw new TimeoutException("engine operation exceeded its deadline");
        }
        catch (AggregateException ae) when (ae.InnerException != null)
        {
            System.Runtime.ExceptionServices.ExceptionDispatchInfo.Capture(ae.InnerException).Throw();
        }
        return task.Result;
    }
}
