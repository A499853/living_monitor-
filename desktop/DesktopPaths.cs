using System;
using System.IO;
using System.Text;

internal static class DesktopPaths
{
    internal static string NormalizeDirectory(string value)
    {
        string full = Path.GetFullPath(value);
        // A drive root must remain D:\, because D: means the drive's current directory.
        if (full.Equals(Path.GetPathRoot(full), StringComparison.OrdinalIgnoreCase)) return full;
        return full.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    }

    internal static string DataDirectory(string root, string[] args)
    {
        string data = NormalizeDirectory(root);
        for (int index = 0; index < args.Length; index++)
        {
            if (args[index] == "--data-dir" && index + 1 < args.Length)
                data = NormalizeDirectory(args[++index]);
            else if (args[index].StartsWith("--data-dir=", StringComparison.Ordinal))
                data = NormalizeDirectory(args[index].Substring("--data-dir=".Length));
        }
        return data;
    }

    internal static string QuoteArgument(string value)
    {
        var quoted = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slashes++; continue; }
            quoted.Append('\\', character == '"' ? slashes * 2 + 1 : slashes);
            quoted.Append(character);
            slashes = 0;
        }
        // Backslashes before the closing quote must be doubled for Windows argv parsing.
        quoted.Append('\\', slashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }
}
