using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PbzEngine;

/// <summary>Typed accessors over the request object. Any shape problem is a BAD_REQUEST.</summary>
public sealed class Req
{
    public JsonObject Obj { get; }

    Req(JsonObject o) { Obj = o; }

    public static Req Parse(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return new Req(new JsonObject());
        JsonNode? n;
        try { n = JsonNode.Parse(text); }
        catch (JsonException) { throw new BadRequestException("request is not valid JSON"); }
        if (n is not JsonObject o) throw new BadRequestException("request must be a JSON object");
        return new Req(o);
    }

    public string? Str(string name) => J.Str(Obj, name);
    public int? Int(string name) => J.Int(Obj, name);
    public bool? Bool(string name) => J.Bool(Obj, name);
    public List<string>? StrList(string name) => J.StrList(Obj, name);
    public JsonArray? Arr(string name) => J.Arr(Obj, name);

    public int Port()
    {
        int? p = Int("port");
        if (p == null) throw new BadRequestException("port is required");
        if (p < 1 || p > 65535) throw new BadRequestException("port out of range");
        return p.Value;
    }
}

/// <summary>Helpers for reading request sub-objects.</summary>
public static class J
{
    static readonly Regex IdRx = new(@"^[A-Za-z0-9_.:/\- ]{1,128}$", RegexOptions.Compiled);

    public static string? Str(JsonObject o, string name)
    {
        if (!o.TryGetPropertyValue(name, out var n) || n == null) return null;
        if (n is JsonValue v && v.TryGetValue<string>(out var s)) return s;
        throw new BadRequestException(name + " must be a string");
    }

    public static string ReqStr(JsonObject o, string name)
    {
        var s = Str(o, name);
        if (string.IsNullOrWhiteSpace(s)) throw new BadRequestException(name + " is required");
        return s;
    }

    public static int? Int(JsonObject o, string name)
    {
        if (!o.TryGetPropertyValue(name, out var n) || n == null) return null;
        if (n is JsonValue v)
        {
            if (v.TryGetValue<int>(out var i)) return i;
            if (v.TryGetValue<long>(out var l) && l >= int.MinValue && l <= int.MaxValue) return (int)l;
            if (v.TryGetValue<double>(out var d) && Math.Abs(d - Math.Round(d)) < 1e-9 && Math.Abs(d) < int.MaxValue) return (int)Math.Round(d);
        }
        throw new BadRequestException(name + " must be an integer");
    }

    public static bool? Bool(JsonObject o, string name)
    {
        if (!o.TryGetPropertyValue(name, out var n) || n == null) return null;
        if (n is JsonValue v && v.TryGetValue<bool>(out var b)) return b;
        throw new BadRequestException(name + " must be a boolean");
    }

    public static double? Num(JsonObject o, string name)
    {
        if (!o.TryGetPropertyValue(name, out var n) || n == null) return null;
        if (n is JsonValue v)
        {
            if (v.TryGetValue<double>(out var d)) return d;
            if (v.TryGetValue<long>(out var l)) return l;
            if (v.TryGetValue<int>(out var i)) return i;
        }
        throw new BadRequestException(name + " must be a number");
    }

    public static JsonArray? Arr(JsonObject o, string name)
    {
        if (!o.TryGetPropertyValue(name, out var n) || n == null) return null;
        if (n is JsonArray a) return a;
        throw new BadRequestException(name + " must be an array");
    }

    public static List<string>? StrList(JsonObject o, string name)
    {
        // a single string is accepted where a list is expected
        if (o.TryGetPropertyValue(name, out var n) && n is JsonValue single && single.TryGetValue<string>(out var one))
            return new List<string> { one };
        var a = Arr(o, name);
        if (a == null) return null;
        var list = new List<string>();
        foreach (var item in a)
        {
            if (item is JsonValue v && v.TryGetValue<string>(out var s)) list.Add(s);
            else throw new BadRequestException(name + " must be a list of strings");
        }
        return list;
    }

    /// <summary>Caller-supplied ids are echoed back, so they are restricted to a safe charset.</summary>
    public static string Id(JsonObject o, string name = "id")
    {
        var s = Str(o, name);
        if (s == null || !IdRx.IsMatch(s)) throw new BadRequestException(name + " missing or not a safe id");
        return s;
    }

    public static bool IsSafeId(string? s) => s != null && IdRx.IsMatch(s);

    public static string Inv(long v) => v.ToString(CultureInfo.InvariantCulture);
}
