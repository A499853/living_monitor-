using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;

internal static class BootstrapVerification
{
    private static int assertions;
    private static void Check(bool value, string description)
    {
        if (!value) throw new Exception(description);
        assertions++;
        Console.WriteLine("PASS " + description);
    }
    private static void Zip(string path, Dictionary<string, string> files)
    {
        using (var zip = ZipFile.Open(path, ZipArchiveMode.Create))
            foreach (var file in files)
                using (var writer = new StreamWriter(zip.CreateEntry(file.Key).Open())) writer.Write(file.Value);
    }
    private static void Install(string root, DependencyBootstrapper.Package package, string archive)
    {
        try { typeof(DependencyBootstrapper).GetMethod("Install", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { root, package, archive, CancellationToken.None }); }
        catch (TargetInvocationException ex) { throw ex.InnerException; }
    }
    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--echo-arguments")
        {
            for (int index = 1; index < args.Length; index++) Console.WriteLine(args[index]);
            return 0;
        }
        string root = Path.GetFullPath(args[0]);
        Directory.CreateDirectory(root);
        try
        {
            string driveRoot = Path.GetPathRoot(root);
            Check(DesktopPaths.NormalizeDirectory(driveRoot) == driveRoot, "drive root retains its trailing separator");
            Check(DesktopPaths.NormalizeDirectory(root + Path.DirectorySeparatorChar) == DesktopPaths.NormalizeDirectory(root), "ordinary directory aliases normalize consistently");
            Check(DesktopPaths.DataDirectory(root, new string[0]) == DesktopPaths.DataDirectory(root, new[] { "--data-dir", root + Path.DirectorySeparatorChar }), "default and explicit data directories use identical mutex paths");
            Check(DesktopPaths.DataDirectory(root, new[] { "--data-dir", driveRoot }) == driveRoot, "explicit drive-root data directory stays absolute");
            Check(DesktopPaths.DataDirectory(root, new[] { "--data-dir=" + driveRoot }) == driveRoot, "equals-form data-directory option preserves drive root");
            string spaced = Path.Combine(root, "directory with spaces") + Path.DirectorySeparatorChar;
            using (var process = Process.Start(new ProcessStartInfo(Assembly.GetExecutingAssembly().Location) {
                Arguments = "--echo-arguments " + DesktopPaths.QuoteArgument(driveRoot) + " " + DesktopPaths.QuoteArgument(spaced) + " sentinel",
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true }))
            {
                string output = process.StandardOutput.ReadToEnd();
                process.WaitForExit();
                Check(output == driveRoot + Environment.NewLine + spaced + Environment.NewLine + "sentinel" + Environment.NewLine, "Windows process receives root and spaced paths without swallowing subsequent arguments");
            }
            var package = new DependencyBootstrapper.Package { Name = "Python", Target = "python", Required = new[] { "python.exe" }, Sha256 = new string('a', 64) };
            string tools = Path.Combine(root, "tools");
            string target = Path.Combine(tools, "python");
            Directory.CreateDirectory(target);
            File.WriteAllText(Path.Combine(target, "python.exe"), "original");
            string bad = Path.Combine(root, "incomplete.zip");
            Zip(bad, new Dictionary<string, string> { { "unrelated.txt", "invalid" } });
            bool rejected = false;
            try { Install(root, package, bad); } catch (InvalidDataException) { rejected = true; }
            Check(rejected && File.ReadAllText(Path.Combine(target, "python.exe")) == "original", "incomplete extraction preserves installed runtime");
            string traversal = Path.Combine(root, "traversal.zip");
            Zip(traversal, new Dictionary<string, string> { { "../escaped.txt", "outside" } });
            rejected = false;
            try { DependencyBootstrapper.ExtractZip(traversal, Path.Combine(root, "extract"), CancellationToken.None); } catch (InvalidDataException) { rejected = true; }
            Check(rejected && !File.Exists(Path.Combine(root, "escaped.txt")), "archive path traversal is rejected");
            string valid = Path.Combine(root, "complete.zip");
            Zip(valid, new Dictionary<string, string> { { "python.exe", "replacement" }, { "library/data", "resource" } });
            Install(root, package, valid);
            Check(File.ReadAllText(Path.Combine(target, "python.exe")) == "replacement" && !Directory.Exists(target + ".previous"), "complete runtime atomically replaces prior installation");
            Check(File.ReadAllText(Path.Combine(target, "python312._pth")).Contains("../../app"), "embedded Python finds packaged backend");
            Check(DependencyBootstrapper.IsReady(root, package), "complete installed receipt is accepted");
            File.AppendAllText(Path.Combine(target, "library", "data"), "damage");
            Check(!DependencyBootstrapper.IsReady(root, package), "truncated or changed-size runtime is repaired on next launch");
            Install(root, package, valid);
            File.Delete(Path.Combine(target, "library", "data"));
            Check(!DependencyBootstrapper.IsReady(root, package), "missing noncritical runtime file triggers repair");
            Install(root, package, valid);
            File.WriteAllText(Path.Combine(target, ".livewatch-runtime.json"), "null");
            Check(!DependencyBootstrapper.IsReady(root, package), "corrupt installation receipt triggers repair");
            using (var hash = SHA256.Create())
            using (var file = File.OpenRead(valid)) package.Sha256 = BitConverter.ToString(hash.ComputeHash(file)).Replace("-", "").ToLowerInvariant();
            package.Size = new FileInfo(valid).Length;
            Check(DependencyBootstrapper.Matches(valid, package), "download archive verifies against actual SHA-256");
            package.Sha256 = new string('0', 64);
            Check(!DependencyBootstrapper.Matches(valid, package), "incorrect SHA-256 archive is rejected");
            var cancellation = new CancellationTokenSource();
            cancellation.Cancel();
            bool cancelled = false;
            try { DependencyBootstrapper.Download(package, Path.Combine(root, "cancelled.zip"), delegate { }, cancellation.Token); } catch (OperationCanceledException) { cancelled = true; }
            Check(cancelled && !File.Exists(Path.Combine(root, "cancelled.zip.partial")), "cancelled download does not create a partial runtime");
            Console.WriteLine("PASS " + assertions + " bootstrap assertions");
            return 0;
        }
        catch (Exception ex) { Console.Error.WriteLine(ex); return 1; }
    }
}
