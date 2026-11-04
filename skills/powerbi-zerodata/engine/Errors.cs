using System.Globalization;
using System.Net.Sockets;
using System.Text;
using Microsoft.AnalysisServices.AdomdClient;
using Microsoft.AnalysisServices.Tabular.Tmdl;

namespace PbzEngine;

/// <summary>Closed set of error classes (CONTRACT.md).</summary>
public enum ErrorClass
{
    SYNTAX,
    UNKNOWN_OBJECT,
    TYPE_MISMATCH,
    CONVERSION,
    CIRCULAR,
    DUPLICATE_KEY,
    TIMEOUT,
    ENGINE_UNAVAILABLE,
    BAD_REQUEST,
    OTHER,
}

/// <summary>Malformed request. Its message is internal (logged), never printed.</summary>
public sealed class BadRequestException : Exception
{
    public BadRequestException(string message) : base(message) { }
}

/// <summary>An engine-side failure whose class is already known.</summary>
public sealed class EngineException : Exception
{
    public ErrorClass Class { get; }
    public EngineException(ErrorClass cls, string message, Exception? inner = null) : base(message, inner) { Class = cls; }
}

public static class Errors
{
    /// <summary>Exception type first, then English message patterns.</summary>
    public static ErrorClass Classify(Exception ex) => Classify(ex, null);

    /// <summary>
    /// As <see cref="Classify(Exception)"/>, but echoed query texts are removed before pattern matching
    /// (Analysis Services appends the full query to syntax errors).
    /// </summary>
    public static ErrorClass Classify(Exception ex, IEnumerable<string>? echoes)
    {
        for (Exception? e = ex; e != null; e = e.InnerException)
        {
            switch (e)
            {
                case BadRequestException: return ErrorClass.BAD_REQUEST;
                case EngineException ee: return ee.Class;
                case TimeoutException: return ErrorClass.TIMEOUT;
                case OperationCanceledException: return ErrorClass.TIMEOUT;
                case TmdlFormatException: return ErrorClass.SYNTAX;
                case TmdlDeserializationWithReferenceErrorsException: return ErrorClass.UNKNOWN_OBJECT;
                case AdomdConnectionException: return ErrorClass.ENGINE_UNAVAILABLE;
                case SocketException: return ErrorClass.ENGINE_UNAVAILABLE;
                case Microsoft.AnalysisServices.ConnectionException: return ErrorClass.ENGINE_UNAVAILABLE;
            }
        }
        string chain = Chain(ex);
        if (echoes != null)
            foreach (var q in echoes)
                if (!string.IsNullOrEmpty(q) && q.Length >= 4) chain = chain.Replace(q, " ");
        return ClassifyMessage(chain);
    }

    public static string Chain(Exception ex)
    {
        var sb = new StringBuilder();
        for (Exception? e = ex; e != null; e = e.InnerException) sb.Append(e.Message).Append(' ');
        return sb.ToString();
    }

    public static ErrorClass ClassifyMessage(string? message)
    {
        if (string.IsNullOrWhiteSpace(message)) return ErrorClass.OTHER;
        // Patterns describe the engine's own prose, so quoted names and literals are removed first
        // (a measure called 'Duplicate Values' must not turn into DUPLICATE_KEY).
        string m = Scrubber.StripQuoted(message).ToLower(CultureInfo.InvariantCulture);
        bool Has(params string[] parts) => parts.Any(p => m.Contains(p, StringComparison.Ordinal));

        if (Has("timed out", "timeout", "time-out")) return ErrorClass.TIMEOUT;
        if (Has("actively refused", "no connection could be made", "unable to connect", "connection was closed",
                "connection was forcibly closed", "cannot connect", "could not connect", "connection is broken",
                "connection cannot be made", "transport", "socket"))
            return ErrorClass.ENGINE_UNAVAILABLE;
        if (Has("circular dependency", "circular reference")) return ErrorClass.CIRCULAR;
        if (Has("duplicate value", "contains a duplicate", "duplicate key", "duplicate values")) return ErrorClass.DUPLICATE_KEY;
        if (Has("syntax", "unexpected token", "unexpected end", "parsing error", "invalid indentation")) return ErrorClass.SYNTAX;
        if (Has("cannot convert", "can't convert", "couldn't convert", "could not convert", "failed to convert", "conversion"))
            return ErrorClass.CONVERSION;
        if (Has("type mismatch", "of type", "do not support comparing", "does not support comparing", "data type"))
            return ErrorClass.TYPE_MISMATCH;
        if (Has("cannot be found", "can't be found", "could not be found", "doesn't exist", "does not exist", "not found",
                "failed to resolve", "cannot find", "can't find", "could not find", "unknown column", "unknown table"))
            return ErrorClass.UNKNOWN_OBJECT;
        return ErrorClass.OTHER;
    }
}

/// <summary>
/// Raw diagnostics go only here: private_dir\logs\engine.log, one line per entry,
/// "log_id TAB base64(DPAPI(utf8 text))". Only the log_id is ever printed.
/// </summary>
public static class EngineLog
{
    public static string? Write(string? privateDir, string text)
    {
        if (string.IsNullOrEmpty(privateDir)) return null;
        try
        {
            string dir = Path.Combine(privateDir, "logs");
            Directory.CreateDirectory(dir);
            string path = Path.Combine(dir, "engine.log");
            string prefix = "e-" + DateTime.Now.ToString("yyyyMMdd", CultureInfo.InvariantCulture) + "-";
            string b64 = Convert.ToBase64String(Dpapi.Protect(Encoding.UTF8.GetBytes(text)));
            for (int attempt = 0; attempt < 100; attempt++)
            {
                try
                {
                    using var fs = new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
                    int n = 0;
                    using (var sr = new StreamReader(fs, new UTF8Encoding(false), false, 4096, leaveOpen: true))
                    {
                        string? line;
                        while ((line = sr.ReadLine()) != null)
                            if (line.StartsWith(prefix, StringComparison.Ordinal)) n++;
                    }
                    string id = prefix + (n + 1).ToString("D4", CultureInfo.InvariantCulture);
                    fs.Seek(0, SeekOrigin.End);
                    var bytes = Encoding.UTF8.GetBytes(id + "\t" + b64 + "\n");
                    fs.Write(bytes, 0, bytes.Length);
                    return id;
                }
                catch (IOException)
                {
                    Thread.Sleep(30);
                }
            }
        }
        catch
        {
            // logging must never break the response
        }
        return null;
    }
}
