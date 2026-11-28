using System.Globalization;
using System.Text;

namespace PbzEngine;

/// <summary>TMDL file naming: characters invalid in file names (and '%') become %XX.</summary>
public static class TmdlNameCodec
{
    const string Reserved = "\\/:*?\"<>|%";

    public static string EncodeName(string name)
    {
        var sb = new StringBuilder();
        foreach (char c in name)
        {
            if (c < 0x20 || Reserved.IndexOf(c) >= 0) sb.Append('%').Append(((int)c).ToString("X2", CultureInfo.InvariantCulture));
            else sb.Append(c);
        }
        return sb.ToString();
    }

    public static string DecodeName(string fileName)
    {
        var sb = new StringBuilder();
        for (int i = 0; i < fileName.Length; i++)
        {
            if (fileName[i] == '%' && i + 2 < fileName.Length &&
                int.TryParse(fileName.AsSpan(i + 1, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out int code))
            {
                sb.Append((char)code);
                i += 2;
            }
            else sb.Append(fileName[i]);
        }
        return sb.ToString();
    }
}

/// <summary>Invocation-local index of one directory's *.tmdl entries, built once.</summary>
public sealed class TmdlDirIndex
{
    readonly List<string> _entries = new();
    readonly Dictionary<string, string> _byName = new(StringComparer.OrdinalIgnoreCase);

    TmdlDirIndex() { }

    public static TmdlDirIndex Build(string dir, Func<string, IEnumerable<string>> enumerateFiles,
        Action<string> refuse, Func<string, bool> exists)
    {
        var index = new TmdlDirIndex();
        refuse(dir);
        if (!exists(dir)) return index;
        foreach (var f in enumerateFiles(dir))
        {
            refuse(f);
            index._entries.Add(f);
            string name = TmdlNameCodec.DecodeName(Path.GetFileNameWithoutExtension(f));
            if (!index._byName.ContainsKey(name)) index._byName[name] = f;
        }
        return index;
    }

    public string? Find(string name) => _byName.TryGetValue(name, out var f) ? f : null;

    public IReadOnlyList<string> Entries => _entries;
}
