namespace PbzEngine;

/// <summary>Refuse a symlink or any other reparse point before it is opened.</summary>
static class LinkGuard
{
    public static bool IsLink(string path)
    {
        try
        {
            return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0;
        }
        catch (Exception ex) when (ex is FileNotFoundException or DirectoryNotFoundException)
        {
            return false;
        }
    }

    public static void Refuse(string path)
    {
        if (IsLink(path))
            throw new EngineException(ErrorClass.OTHER, "refusing to read through a link");
    }
}
