using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

// Runs before the backend or browser is started. All mutable files stay beside the EXE.
internal static class DependencyBootstrapper
{
    internal sealed class Package
    {
        public string Name, Url, FileName, Sha256, Target;
        public long Size;
        public string[] Required;
    }

    internal static Package[] ReadPackages(string root)
    {
        var json = new JavaScriptSerializer();
        var core = json.Deserialize<Dictionary<string, Dictionary<string, object>>>(File.ReadAllText(Path.Combine(root, "dependency-manifest.json")));
        var desktop = json.Deserialize<Dictionary<string, Dictionary<string, object>>>(File.ReadAllText(Path.Combine(root, "desktop-dependency-manifest.json")));
        return new[] {
            ReadPackage(core["python"], "Python", "python", new[] { "python.exe", "pythonw.exe", "python312.dll", "python312.zip", "python312._pth", "_ssl.pyd", "_sqlite3.pyd" }),
            ReadPackage(core["ffmpeg"], "FFmpeg", "ffmpeg.exe", new string[0]),
            ReadPackage(desktop["webview2_runtime"], "WebView2", "webview2", new[] { "msedgewebview2.exe", "msedge.dll", "icudtl.dat", "resources.pak", "EBWebView/x64/EmbeddedBrowserWebView.dll" })
        };
    }

    private static Package ReadPackage(Dictionary<string, object> item, string name, string target, string[] required)
    {
        var package = new Package { Name = name, Target = target, Required = required,
            Url = Convert.ToString(item["url"]), FileName = Convert.ToString(item["file"]),
            Sha256 = Convert.ToString(item["sha256"]).ToLowerInvariant(), Size = Convert.ToInt64(item["size"]) };
        Uri uri;
        if (!Uri.TryCreate(package.Url, UriKind.Absolute, out uri) || uri.Scheme != "https" ||
            Path.GetFileName(package.FileName) != package.FileName || package.FileName.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 ||
            !System.Text.RegularExpressions.Regex.IsMatch(package.Sha256, "^[a-f0-9]{64}$") || package.Size <= 0)
            throw new InvalidDataException("依赖清单无效：" + name);
        return package;
    }

    internal static bool IsReady(string root, Package package)
    {
        string target = Path.Combine(root, "tools", package.Target);
        if (package.Name == "FFmpeg") return File.Exists(target) && new FileInfo(target).Length > 1000000;
        foreach (string relative in package.Required)
        {
            string path = Path.Combine(target, relative.Replace('/', Path.DirectorySeparatorChar));
            if (!File.Exists(path) || new FileInfo(path).Length == 0) return false;
        }
        // Receipts also detect incomplete extractions after an interrupted first launch.
        string receipt = Path.Combine(target, ".livewatch-runtime.json");
        string pin = Path.Combine(target, ".livewatch-package-sha256");
        if (File.Exists(pin) && File.ReadAllText(pin).Trim() != package.Sha256) return false;
        if (File.Exists(receipt))
        {
            try
            {
                var files = new JavaScriptSerializer().Deserialize<Dictionary<string, long>>(File.ReadAllText(receipt));
                if (files == null || files.Count == 0) return false;
                foreach (var file in files)
                {
                    string path = SafeChild(target, file.Key);
                    if (!File.Exists(path) || new FileInfo(path).Length != file.Value) return false;
                }
            }
            catch (Exception ex)
            {
                if (!(ex is IOException) && !(ex is ArgumentException) && !(ex is InvalidOperationException) && !(ex is FormatException)) throw;
                return false;
            }
        }
        return true;
    }

    internal static bool NeedsPreparation(string root)
    {
        foreach (var package in ReadPackages(root)) if (!IsReady(root, package)) return true;
        return false;
    }

    internal static void Prepare(string root, Action<string> progress, CancellationToken token)
    {
        Directory.CreateDirectory(Path.Combine(root, "tools"));
        // A separate directory lock also protects users starting with different --data-dir values.
        using (var installLock = new FileStream(Path.Combine(root, "tools", ".bootstrap.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
        {
            foreach (var package in ReadPackages(root))
            {
                token.ThrowIfCancellationRequested();
                if (IsReady(root, package)) { progress(package.Name + " 已就绪"); continue; }
                string cache = Path.Combine(root, "tools", ".downloads");
                Directory.CreateDirectory(cache);
                string archive = Path.Combine(cache, package.FileName);
                long spaceRequired = package.Size * 5 + 100L * 1024 * 1024;
                var drive = new DriveInfo(Path.GetPathRoot(root));
                if (drive.IsReady && drive.AvailableFreeSpace < spaceRequired)
                    throw new IOException("软件所在磁盘空间不足，准备 " + package.Name + " 至少需要 " + (spaceRequired / 1024 / 1024) + " MB 可用空间。");
                Download(package, archive, progress, token);
                progress("正在解压并检查 " + package.Name + "…");
                Install(root, package, archive, token);
                File.Delete(archive);
                progress(package.Name + " 安装完成");
            }
        }
        progress("依赖准备完成，正在启动本地程序…");
    }

    internal static bool Matches(string path, Package package)
    {
        if (!File.Exists(path) || new FileInfo(path).Length != package.Size) return false;
        using (var hash = SHA256.Create())
        using (var file = File.OpenRead(path))
            return BitConverter.ToString(hash.ComputeHash(file)).Replace("-", "").Equals(package.Sha256, StringComparison.OrdinalIgnoreCase);
    }

    internal static void Download(Package package, string destination, Action<string> progress, CancellationToken token)
    {
        if (Matches(destination, package)) { progress("已校验缓存 " + package.Name); return; }
        ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
        for (int attempt = 1; attempt <= 3; attempt++)
        {
            token.ThrowIfCancellationRequested();
            string partial = destination + ".partial";
            try
            {
                if (Matches(partial, package))
                {
                    if (File.Exists(destination)) File.Delete(destination);
                    File.Move(partial, destination);
                    return;
                }
                long offset = File.Exists(partial) ? new FileInfo(partial).Length : 0;
                if (offset >= package.Size) { File.Delete(partial); offset = 0; }
                progress("正在下载 " + package.Name + "（第 " + attempt + "/3 次）…");
                var request = (HttpWebRequest)WebRequest.Create(package.Url);
                request.Timeout = 20000;
                request.ReadWriteTimeout = 30000;
                request.UserAgent = "LiveWatch-Bootstrap/2.9";
                if (offset > 0) request.AddRange(offset);
                var timer = Stopwatch.StartNew();
                using (token.Register(request.Abort))
                using (var response = (HttpWebResponse)request.GetResponse())
                {
                    if (response.ResponseUri.Scheme != "https") throw new InvalidDataException("拒绝非 HTTPS 下载。");
                    if (response.StatusCode == HttpStatusCode.PartialContent)
                    {
                        string expectedRange = "bytes " + offset + "-" + (package.Size - 1) + "/" + package.Size;
                        if (response.Headers["Content-Range"] != expectedRange) throw new InvalidDataException("断点续传范围与清单不符。");
                    }
                    else offset = 0; // Servers may ignore Range; restart rather than append duplicate bytes.
                    if (response.ContentLength > 0 && response.ContentLength != package.Size - offset) throw new InvalidDataException("下载大小与依赖清单不符。");
                    using (var input = response.GetResponseStream())
                    using (var output = new FileStream(partial, offset > 0 ? FileMode.Append : FileMode.Create, FileAccess.Write, FileShare.None))
                    {
                        var buffer = new byte[65536];
                        long total = offset, lastProgress = -1;
                        int count;
                        while ((count = input.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            token.ThrowIfCancellationRequested();
                            if (timer.Elapsed > TimeSpan.FromMinutes(15)) throw new TimeoutException("单次下载超过 15 分钟。");
                            total += count;
                            if (total > package.Size) throw new InvalidDataException("下载超过清单大小。");
                            output.Write(buffer, 0, count);
                            long percent = total * 100 / package.Size;
                            if (percent != lastProgress)
                            {
                                progress("正在下载 " + package.Name + "  " + percent + "%（" + (total / 1048576) + "/" + (package.Size / 1048576) + " MB）");
                                lastProgress = percent;
                            }
                        }
                        output.Flush(true);
                    }
                }
                token.ThrowIfCancellationRequested();
                progress("正在校验 " + package.Name + " SHA-256…");
                if (!Matches(partial, package)) throw new InvalidDataException("依赖完整性校验失败，未安装。");
                if (File.Exists(destination)) File.Delete(destination);
                File.Move(partial, destination);
                return;
            }
            catch (Exception ex)
            {
                // Retain a bounded partial download after transport failures/cancellation.
                // Corrupt or mismatched payloads are discarded before any retry.
                if (ex is InvalidDataException && File.Exists(partial)) File.Delete(partial);
                token.ThrowIfCancellationRequested();
                if (!(ex is WebException) && !(ex is IOException) && !(ex is TimeoutException)) throw;
                if (attempt == 3) throw new IOException(package.Name + " 下载失败（已尝试 3 次）：" + ex.Message + " 请检查网络后重新双击程序。", ex);
                progress(package.Name + " 下载中断，稍后重试…");
                if (token.WaitHandle.WaitOne(attempt * 2000)) token.ThrowIfCancellationRequested();
            }
        }
    }

    internal static string SafeChild(string root, string relative)
    {
        string prefix = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        string path = Path.GetFullPath(Path.Combine(prefix, relative));
        if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("压缩包包含非法路径。");
        return path;
    }

    internal static void ExtractZip(string archive, string destination, CancellationToken token)
    {
        using (var zip = ZipFile.OpenRead(archive))
        {
            long total = 0;
            foreach (var entry in zip.Entries)
            {
                token.ThrowIfCancellationRequested();
                total += entry.Length;
                if (total > 2L * 1024 * 1024 * 1024) throw new InvalidDataException("解压大小超过限制。");
                string path = SafeChild(destination, entry.FullName.Replace('/', Path.DirectorySeparatorChar));
                if (entry.Name.Length == 0) Directory.CreateDirectory(path);
                else { Directory.CreateDirectory(Path.GetDirectoryName(path)); entry.ExtractToFile(path, true); }
            }
        }
    }

    private static void Install(string root, Package package, string archive, CancellationToken token)
    {
        string tools = Path.Combine(root, "tools");
        string stage = Path.Combine(tools, ".bootstrap-" + package.Name.ToLowerInvariant());
        string target = Path.Combine(tools, package.Target);
        string backup = target + ".previous";
        // Only fixed, application-owned staging paths are removed; user files are never enumerated.
        if (Directory.Exists(stage)) Directory.Delete(stage, true);
        Directory.CreateDirectory(stage);
        try
        {
            string prepared = stage;
            if (package.Name == "WebView2")
            {
                using (var process = Process.Start(new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "expand.exe"))
                    { Arguments = "-F:* \"" + archive + "\" \"" + stage + "\"", UseShellExecute = false, CreateNoWindow = true }))
                {
                    var timer = Stopwatch.StartNew();
                    while (!process.WaitForExit(250))
                    {
                        if (token.IsCancellationRequested || timer.Elapsed > TimeSpan.FromMinutes(5))
                        {
                            process.Kill(); process.WaitForExit(5000);
                            token.ThrowIfCancellationRequested();
                            throw new TimeoutException("WebView2 解压超时。");
                        }
                    }
                    if (process.ExitCode != 0) throw new IOException("WebView2 解压失败，代码 " + process.ExitCode);
                }
                string[] browsers = Directory.GetFiles(stage, "msedgewebview2.exe", SearchOption.AllDirectories);
                if (browsers.Length != 1) throw new InvalidDataException("WebView2 包结构无效。");
                prepared = Path.GetDirectoryName(browsers[0]);
            }
            else ExtractZip(archive, stage, token);

            if (package.Name == "FFmpeg")
            {
                string[] binaries = Directory.GetFiles(stage, "ffmpeg*.exe", SearchOption.AllDirectories);
                if (binaries.Length != 1) throw new InvalidDataException("FFmpeg 包结构无效。");
                token.ThrowIfCancellationRequested();
                if (File.Exists(target)) File.Replace(binaries[0], target, null);
                else File.Move(binaries[0], target);
            }
            else
            {
                if (package.Name == "Python") File.WriteAllText(Path.Combine(prepared, "python312._pth"), "python312.zip\r\n.\r\n../../app\r\n", Encoding.ASCII);
                foreach (string relative in package.Required)
                    if (!File.Exists(Path.Combine(prepared, relative.Replace('/', Path.DirectorySeparatorChar)))) throw new InvalidDataException(package.Name + " 缺少必要文件：" + relative);
                File.WriteAllText(Path.Combine(prepared, ".livewatch-package-sha256"), package.Sha256, Encoding.ASCII);
                var receipt = new Dictionary<string, long>();
                foreach (string file in Directory.GetFiles(prepared, "*", SearchOption.AllDirectories)) receipt[file.Substring(prepared.Length + 1)] = new FileInfo(file).Length;
                File.WriteAllText(Path.Combine(prepared, ".livewatch-runtime.json"), new JavaScriptSerializer().Serialize(receipt), Encoding.UTF8);
                token.ThrowIfCancellationRequested();
                if (Directory.Exists(backup)) Directory.Delete(backup, true);
                if (Directory.Exists(target)) Directory.Move(target, backup);
                try { Directory.Move(prepared, target); }
                catch { if (!Directory.Exists(target) && Directory.Exists(backup)) Directory.Move(backup, target); throw; }
                if (Directory.Exists(backup)) Directory.Delete(backup, true);
            }
        }
        finally { if (Directory.Exists(stage)) Directory.Delete(stage, true); }
    }
}

internal sealed class DependencyWindow : Form
{
    private readonly CancellationTokenSource cancellation = new CancellationTokenSource();
    private readonly Label status;
    private bool complete;
    internal bool Succeeded { get; private set; }

    internal DependencyWindow(string root)
    {
        Text = "直播守望 · 首次启动";
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(620, 250);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        Font = new Font("Microsoft YaHei UI", 10f);
        status = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter,
            Text = "正在检查本地运行环境…" };
        Controls.Add(status);
        var cancel = new Button { Dock = DockStyle.Bottom, Height = 38, Text = "取消，稍后继续" };
        cancel.Click += delegate { Close(); };
        Controls.Add(cancel);
        Shown += async delegate
        {
            try
            {
                var progress = new Progress<string>(delegate(string message) {
                    status.Text = "运行依赖将保存到软件所在文件夹\n首次启动需要联网，后续可直接打开\n\n" + message + "\n\n" + root;
                });
                await Task.Run(() => DependencyBootstrapper.Prepare(root, ((IProgress<string>)progress).Report, cancellation.Token));
                Succeeded = true;
            }
            catch (OperationCanceledException) { }
            catch (Exception ex)
            {
                try { Directory.CreateDirectory(Path.Combine(root, "logs")); File.AppendAllText(Path.Combine(root, "logs", "bootstrap.log"), DateTimeOffset.Now.ToString("o") + " " + ex + Environment.NewLine); } catch (IOException) { } catch (UnauthorizedAccessException) { }
                MessageBox.Show(this, "依赖准备未完成：" + ex.Message + "\n\n请将软件完整解压到可写目录，检查网络后重新双击。已完成的依赖会保留。", Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            finally { complete = true; Close(); }
        };
        FormClosing += delegate(object sender, FormClosingEventArgs args) {
            if (!complete) { args.Cancel = true; cancel.Enabled = false; status.Text = "正在取消下载…"; cancellation.Cancel(); }
        };
        FormClosed += delegate { cancellation.Dispose(); };
    }
}
