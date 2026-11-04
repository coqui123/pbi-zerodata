using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace PbzEngine;

/// <summary>DPAPI (CurrentUser, CRYPTPROTECT_UI_FORBIDDEN, no entropy) via P/Invoke to crypt32.dll.</summary>
public static class Dpapi
{
    const int CRYPTPROTECT_UI_FORBIDDEN = 0x1;

    [StructLayout(LayoutKind.Sequential)]
    struct DATA_BLOB
    {
        public int cbData;
        public IntPtr pbData;
    }

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CryptProtectData(ref DATA_BLOB pDataIn, string? szDataDescr, IntPtr pOptionalEntropy,
        IntPtr pvReserved, IntPtr pPromptStruct, int dwFlags, ref DATA_BLOB pDataOut);

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CryptUnprotectData(ref DATA_BLOB pDataIn, IntPtr ppszDataDescr, IntPtr pOptionalEntropy,
        IntPtr pvReserved, IntPtr pPromptStruct, int dwFlags, ref DATA_BLOB pDataOut);

    [DllImport("kernel32.dll")]
    static extern IntPtr LocalFree(IntPtr hMem);

    public static byte[] Protect(byte[] data) => Run(data, protect: true);

    public static byte[] Unprotect(byte[] data) => Run(data, protect: false);

    static byte[] Run(byte[] data, bool protect)
    {
        var input = new DATA_BLOB { cbData = data.Length, pbData = Marshal.AllocHGlobal(Math.Max(1, data.Length)) };
        var output = new DATA_BLOB();
        try
        {
            Marshal.Copy(data, 0, input.pbData, data.Length);
            bool ok = protect
                ? CryptProtectData(ref input, null, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, CRYPTPROTECT_UI_FORBIDDEN, ref output)
                : CryptUnprotectData(ref input, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, CRYPTPROTECT_UI_FORBIDDEN, ref output);
            if (!ok) throw new CryptographicException(Marshal.GetLastWin32Error());
            var result = new byte[output.cbData];
            Marshal.Copy(output.pbData, result, 0, output.cbData);
            return result;
        }
        finally
        {
            for (int i = 0; i < data.Length; i++) Marshal.WriteByte(input.pbData, i, 0);
            Marshal.FreeHGlobal(input.pbData);
            if (output.pbData != IntPtr.Zero)
            {
                if (!protect) for (int i = 0; i < output.cbData; i++) Marshal.WriteByte(output.pbData, i, 0);
                LocalFree(output.pbData);
            }
        }
    }
}

/// <summary>
/// Key file (private_dir\key.bin = DPAPI(32 random bytes)) and keyed HMAC-SHA256 of normalized values.
/// Python reproduces <see cref="Norm"/> exactly; see README.md.
/// </summary>
public sealed class Vault
{
    public const string BlankText = "∅blank";
    public const char TupleSeparator = '\u001f';

    readonly byte[] _key;

    Vault(byte[] key) { _key = key; }

    public static Vault Open(string privateDir)
    {
        string path = Path.Combine(privateDir, "key.bin");
        for (int attempt = 0; attempt < 10; attempt++)
        {
            if (File.Exists(path))
            {
                byte[] key = Dpapi.Unprotect(File.ReadAllBytes(path));
                if (key.Length != 32) throw new CryptographicException("key.bin does not hold a 32-byte key");
                return new Vault(key);
            }
            byte[] fresh = RandomNumberGenerator.GetBytes(32);
            string tmp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            File.WriteAllBytes(tmp, Dpapi.Protect(fresh));
            try
            {
                File.Move(tmp, path, overwrite: false);
                return new Vault(fresh);
            }
            catch (IOException)
            {
                // another process created the key first: use theirs
                try { File.Delete(tmp); } catch { }
            }
        }
        throw new CryptographicException("could not create or read key.bin");
    }

    public string Hmac(object? value) => HmacOf(Norm.Value(value));

    public string HmacText(string normalizedOrRaw, bool alreadyNormalized)
        => HmacOf(alreadyNormalized ? normalizedOrRaw : Norm.Text(normalizedOrRaw));

    public string HmacTuple(IEnumerable<object?> values)
        => HmacOf(string.Join(TupleSeparator, values.Select(Norm.Value)));

    public string HmacOf(string normalized)
    {
        using var h = new HMACSHA256(_key);
        return Convert.ToHexString(h.ComputeHash(Encoding.UTF8.GetBytes(normalized))).ToLowerInvariant();
    }

    /// <summary>Writes DPAPI(utf8 text) atomically.</summary>
    public static void WriteProtected(string path, string text)
    {
        string tmp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllBytes(tmp, Dpapi.Protect(Encoding.UTF8.GetBytes(text)));
        File.Move(tmp, path, overwrite: true);
    }

    public static void WriteAtomic(string path, string text)
    {
        string tmp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllText(tmp, text, new UTF8Encoding(false));
        File.Move(tmp, path, overwrite: true);
    }
}

/// <summary>Value normalization shared by every HMAC (contract "Secrets and HMAC").</summary>
public static class Norm
{
    /// <summary>s.Normalize(FormC).Trim().ToLowerInvariant()</summary>
    public static string Text(string s) => s.Normalize(NormalizationForm.FormC).Trim().ToLowerInvariant();

    /// <summary>Invariant "R" format of the double rounded to 12 significant digits (-0 folds to 0).</summary>
    public static string Number(double d)
    {
        double r = double.Parse(d.ToString("G12", CultureInfo.InvariantCulture), NumberStyles.Float, CultureInfo.InvariantCulture);
        if (r == 0) r = 0.0;
        return r.ToString("R", CultureInfo.InvariantCulture);
    }

    public static string Date(DateTime dt) => dt.ToString("yyyy-MM-dd'T'HH:mm:ss", CultureInfo.InvariantCulture);

    public static bool IsBlank(object? v) => v == null || v is DBNull;

    public static bool IsNumeric(object? v) => v is double or float or decimal or long or int or short or sbyte or byte or ulong or uint or ushort;

    public static double ToDouble(object v) => v is decimal m ? (double)m : Convert.ToDouble(v, CultureInfo.InvariantCulture);

    public static string Value(object? v) => v switch
    {
        null => Vault.BlankText,
        DBNull => Vault.BlankText,
        string s => Text(s),
        DateTime dt => Date(dt),
        DateTimeOffset dto => Date(dto.DateTime),
        bool b => b ? "true" : "false",
        _ when IsNumeric(v) => Number(ToDouble(v)),
        _ => Text(Convert.ToString(v, CultureInfo.InvariantCulture) ?? ""),
    };
}

/// <summary>Fixed coarse buckets (shape tier only).</summary>
public static class Buckets
{
    public static string Count(long n) => n switch
    {
        <= 0 => "0",
        < 10 => "1-9",
        < 100 => "10-99",
        < 1000 => "100-999",
        < 10000 => "1k-9k",
        _ => "10k+",
    };

    /// <summary>Blank share: none, &lt;1%, 1-10%, 10-50%, &gt;50% (upper bounds inclusive).</summary>
    public static string Share(long part, long total)
    {
        if (part <= 0 || total <= 0) return "none";
        double share = (double)part / total;
        if (share < 0.01) return "<1%";
        if (share <= 0.10) return "1-10%";
        if (share <= 0.50) return "10-50%";
        return ">50%";
    }

    public static string Mismatch(long n) => n switch
    {
        <= 0 => "NONE",
        1 => "ONE",
        <= 5 => "FEW",
        _ => "MANY",
    };
}
