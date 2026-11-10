using System.Globalization;
using System.Reflection;
using AMO = Microsoft.AnalysisServices;

namespace PbzEngine;

/// <summary>
/// Closed bands for trace durations and counts. Raw milliseconds and raw counts stay in the process.
/// </summary>
public static class TraceBands
{
    public static string Duration(double ms)
    {
        if (double.IsNaN(ms) || ms < 100) return "LT_100MS";
        if (ms < 500) return "100_500MS";
        if (ms < 2000) return "500MS_2S";
        if (ms < 10000) return "2_10S";
        return "GT_10S";
    }

    /// <summary>Storage-engine and DirectQuery event counts.</summary>
    public static string Events(int n)
    {
        if (n <= 0) return "0";
        if (n <= 5) return "1_5";
        if (n <= 20) return "6_20";
        if (n <= 100) return "21_100";
        return "GT_100";
    }

    /// <summary>Query-plan operator counts.</summary>
    public static string Plan(int n)
    {
        if (n <= 0) return "0";
        if (n == 1) return "1";
        if (n <= 5) return "2_5";
        if (n <= 20) return "6_20";
        return "GT_20";
    }
}

/// <summary>
/// AMO trace opened by reflection. Event class and column names are resolved with
/// <see cref="Enum.TryParse(Type, string, bool, out object?)"/>; unknown names are skipped.
/// <c>Traces</c> is not assumed to exist. Event text never leaves this object.
/// </summary>
public sealed class EngineTrace : IDisposable
{
    public sealed class Hit
    {
        public string Class = "";
        public string Subclass = "";
        public long Duration;
        public string ObjectName = "";
        public string Session = "";
        public string Text = "";
    }

    readonly object _gate = new();
    readonly List<Hit> _hits = new();
    object? _server;
    object? _trace;
    EventInfo? _onEvent;
    Delegate? _handler;
    volatile bool _accept;

    public bool Started { get; private set; }

    /// <summary>Starts a trace, or returns null when setup fails. The caller still completes the verb.</summary>
    public static EngineTrace? TryStart(object server, IReadOnlyList<string> eventNames, Ctx ctx)
    {
        var trace = new EngineTrace();
        try
        {
            if (!trace.Attach(server, eventNames))
            {
                trace.Dispose();
                ctx.LogText("trace unavailable");
                return null;
            }
            return trace;
        }
        catch (Exception ex)
        {
            trace.Dispose();
            ctx.LogText("trace unavailable " + Errors.Classify(ex));
            return null;
        }
    }

    public void Arm()
    {
        _accept = false;
        lock (_gate) _hits.Clear();
        _accept = true;
    }

    /// <summary>
    /// Copies events captured since <see cref="Arm"/>. When <paramref name="ready"/> is set, waits up to
    /// <paramref name="waitMs"/> for it to match, then a short beat for events that arrive with QueryEnd.
    /// </summary>
    public List<Hit> Collect(int waitMs, Func<Hit, bool>? ready)
    {
        var sw = System.Diagnostics.Stopwatch.StartNew();
        while (ready != null && sw.ElapsedMilliseconds < waitMs)
        {
            bool done;
            lock (_gate) done = _hits.Any(ready);
            if (done) break;
            Thread.Sleep(40);
        }
        if (waitMs > 0) Thread.Sleep(ready == null ? Math.Min(waitMs, 250) : 80);
        _accept = false;
        lock (_gate)
        {
            var copy = _hits.ToList();
            _hits.Clear();
            return copy;
        }
    }

    public void Dispose()
    {
        _accept = false;
        var trace = _trace;
        _trace = null;
        if (trace == null) return;
        try { _onEvent?.RemoveEventHandler(trace, _handler); } catch { }
        try { trace.GetType().GetMethod("Stop", Type.EmptyTypes)?.Invoke(trace, null); } catch { }
        try
        {
            var drop = trace.GetType().GetMethod("Drop", Type.EmptyTypes);
            if (drop != null)
            {
                drop.Invoke(trace, null);
                return;
            }
        }
        catch { }
        try
        {
            if (_server == null) return;
            var traces = _server.GetType().GetProperty("Traces")?.GetValue(_server);
            if (traces == null) return;
            foreach (var method in traces.GetType().GetMethods())
            {
                var ps = method.GetParameters();
                if (method.Name == "Remove" && ps.Length == 1 && ps[0].ParameterType.IsInstanceOfType(trace))
                {
                    method.Invoke(traces, new[] { trace });
                    break;
                }
            }
        }
        catch { }
    }

    bool Attach(object server, IReadOnlyList<string> eventNames)
    {
        _server = server;
        var traces = server.GetType().GetProperty("Traces")?.GetValue(server);
        if (traces == null) return false;
        var add = traces.GetType().GetMethod("Add", new[] { typeof(string) });
        if (add == null) return false;
        var trace = add.Invoke(traces, new object[] { "pbz" + Guid.NewGuid().ToString("N") });
        if (trace == null) return false;
        _trace = trace;

        var log = trace.GetType().GetProperty("LogFileName");
        if (log != null && log.CanWrite && log.PropertyType == typeof(string))
            log.SetValue(trace, "");

        var events = trace.GetType().GetProperty("Events")?.GetValue(trace);
        if (events == null) return false;
        var eventEnum = typeof(AMO.Server).Assembly.GetType("Microsoft.AnalysisServices.TraceEventClass");
        var columnEnum = typeof(AMO.Server).Assembly.GetType("Microsoft.AnalysisServices.TraceColumn");
        if (eventEnum == null) return false;

        int subscribed = 0;
        foreach (var name in eventNames)
        {
            if (!Enum.TryParse(eventEnum, name, false, out object? id) || id == null) continue;
            try
            {
                var te = InvokeAdd(events, id);
                if (te == null) continue;
                if (columnEnum != null)
                {
                    var cols = te.GetType().GetProperty("Columns")?.GetValue(te);
                    if (cols != null)
                    {
                        foreach (var col in new[] { "Duration", "TextData", "ObjectName", "EventSubclass", "IntegerData", "SessionID" })
                        {
                            if (!Enum.TryParse(columnEnum, col, false, out object? cv) || cv == null) continue;
                            try { InvokeAdd(cols, cv); } catch { }
                        }
                    }
                }
                subscribed++;
            }
            catch
            {
                // this event is not usable here; the others still subscribe
            }
        }
        if (subscribed == 0) return false;

        trace.GetType().GetMethod("Update", Type.EmptyTypes)?.Invoke(trace, null);

        var onEvent = trace.GetType().GetEvent("OnEvent");
        var method = typeof(EngineTrace).GetMethod(nameof(Handle), BindingFlags.Instance | BindingFlags.NonPublic);
        if (onEvent?.EventHandlerType == null || method == null) return false;
        _handler = Delegate.CreateDelegate(onEvent.EventHandlerType, this, method);
        onEvent.AddEventHandler(trace, _handler);
        _onEvent = onEvent;

        var start = trace.GetType().GetMethod("Start", Type.EmptyTypes);
        if (start == null) return false;
        start.Invoke(trace, null);
        Started = true;
        return true;
    }

    static object? InvokeAdd(object collection, object value)
    {
        var method = collection.GetType().GetMethod("Add", new[] { value.GetType() });
        if (method == null)
        {
            method = collection.GetType().GetMethods()
                .FirstOrDefault(m => m.Name == "Add" && m.GetParameters().Length == 1
                    && m.GetParameters()[0].ParameterType.IsInstanceOfType(value));
        }
        return method?.Invoke(collection, new[] { value });
    }

    void Handle(object sender, AMO.TraceEventArgs args)
    {
        if (!_accept || args == null) return;
        try
        {
            string cls = args.EventClass.ToString();
            var hit = new Hit
            {
                Class = cls,
                Subclass = args.EventSubclass.ToString(),
                Duration = args.Duration < 0 ? 0 : args.Duration,
                ObjectName = args.ObjectName ?? "",
                Session = args.SessionID ?? "",
                Text = Clip(args.TextData ?? "", cls),
            };
            lock (_gate) _hits.Add(hit);
        }
        catch
        {
            // a bad event must not tear the trace down
        }
    }

    static string Clip(string text, string cls)
    {
        int max = cls == "DAXQueryPlan" ? 1_000_000 : 128_000;
        return text.Length <= max ? text : text.Substring(0, max);
    }
}
