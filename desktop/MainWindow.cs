using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal sealed class MainWindow : Form
{
	private readonly string root;

	private readonly string data;

	private readonly WebView2 view;

	private readonly Label splash;

	private readonly HttpClient http;

	private readonly Timer monitor;

	private string origin = "";

	private string token = "";

	private Process core;

	private bool approvedClose;

	private bool busyClose;

	private bool ready;

	private int failures;

	private CoreWebView2Environment browserEnvironment;

	private DouyinWindow loginWindow;

	internal MainWindow(string root, string data)
	{
		this.root = root;
		this.data = data;
		Text = "直播守望 2.8 · 本地直播监控";
		Font = new Font("Microsoft YaHei UI", 10f);
		base.AutoScaleMode = AutoScaleMode.Dpi;
		base.StartPosition = FormStartPosition.CenterScreen;
		base.Size = new Size(1580, 960);
		MinimumSize = new Size(1120, 680);
		Rectangle workingArea = Screen.PrimaryScreen.WorkingArea;
		if (base.Width > workingArea.Width || base.Height > workingArea.Height)
		{
			base.Size = new Size(Math.Min(base.Width, workingArea.Width), Math.Min(base.Height, workingArea.Height));
		}
		BackColor = Color.FromArgb(247, 249, 252);
		view = new WebView2
		{
			Dock = DockStyle.Fill,
			DefaultBackgroundColor = BackColor,
			Visible = false
		};
		splash = new Label
		{
			Dock = DockStyle.Fill,
			Text = "直播守望\n\n正在启动本地监控服务…",
			TextAlign = ContentAlignment.MiddleCenter,
			Font = new Font("Microsoft YaHei UI", 18f)
		};
		base.Controls.Add(view);
		base.Controls.Add(splash);
		http = new HttpClient(new HttpClientHandler
		{
			UseProxy = false,
			AllowAutoRedirect = false
		});
		http.Timeout = TimeSpan.FromSeconds(5.0);
		monitor = new Timer
		{
			Interval = 1200
		};
		monitor.Tick += MonitorCore;
		base.Shown += StartAsync;
		base.FormClosing += ClosingAsync;
		base.FormClosed += delegate
		{
			if (loginWindow != null)
			{
				loginWindow.Close();
			}
			monitor.Stop();
			http.Dispose();
			view.Dispose();
			if (core != null)
			{
				core.Dispose();
			}
		};
	}

	private void Log(string message)
	{
		try
		{
			Directory.CreateDirectory(Path.Combine(data, "logs"));
			File.AppendAllText(Path.Combine(data, "logs", "desktop.log"), DateTimeOffset.Now.ToString("o") + " " + message + Environment.NewLine, Encoding.UTF8);
		}
		catch (IOException)
		{
		}
		catch (UnauthorizedAccessException)
		{
		}
	}

	private async void StartAsync(object sender, EventArgs args)
	{
		bool failed = false;
		try
		{
			Directory.CreateDirectory(Path.Combine(data, "data"));
			string runtime = Path.Combine(root, "tools", "python", "pythonw.exe");
			string script = Path.Combine(root, "app", "run.py");
			string browser = Path.Combine(root, "tools", "webview2");
			if (!File.Exists(runtime) || !File.Exists(script) || !File.Exists(Path.Combine(browser, "msedgewebview2.exe")))
			{
				throw new FileNotFoundException("请完整解压软件包，保留 app、tools 文件夹及 WebView2 运行库。");
			}
			if (!(await TryAttachAsync()))
			{
				core = Process.Start(new ProcessStartInfo(runtime)
				{
					Arguments = DesktopPaths.QuoteArgument(script) + " --no-browser --data-dir " + DesktopPaths.QuoteArgument(data) + " --owner-pid " + Process.GetCurrentProcess().Id,
					WorkingDirectory = root,
					UseShellExecute = false,
					CreateNoWindow = true
				});
				bool attached = false;
				for (int attempt = 0; attempt < 120; attempt++)
				{
					if (core.HasExited)
					{
						throw new InvalidOperationException("本地服务启动失败，请查看 logs\\app.log。");
					}
					if (await TryAttachAsync())
					{
						attached = true;
						break;
					}
					await Task.Delay(150);
				}
				if (!attached)
				{
					throw new TimeoutException("本地服务启动超时，请查看日志后重试。");
				}
			}
			Match match = Regex.Match(await http.GetStringAsync(origin + "/"), "<meta\\s+name=\"api-token\"\\s+content=\"([^\"]+)\"");
			if (!match.Success)
			{
				throw new InvalidOperationException("本地服务版本不匹配。");
			}
			token = match.Groups[1].Value;
			splash.Text = "直播守望\n\n正在打开监控窗口…";
			browserEnvironment = await CoreWebView2Environment.CreateAsync(browser, Path.Combine(data, "data", "webview2"));
			await view.EnsureCoreWebView2Async(browserEnvironment);
			view.CoreWebView2.WebMessageReceived += HandleMessage;
			view.CoreWebView2.DownloadStarting += SaveDownload;
			view.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
			view.CoreWebView2.Settings.AreDevToolsEnabled = false;
			view.CoreWebView2.Settings.IsStatusBarEnabled = false;
			view.CoreWebView2.Settings.IsZoomControlEnabled = true;
			view.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = false;
			view.CoreWebView2.Settings.AreDefaultScriptDialogsEnabled = true;
			view.CoreWebView2.NewWindowRequested += delegate(object s, CoreWebView2NewWindowRequestedEventArgs e)
			{
				e.Handled = true;
			};
			view.CoreWebView2.PermissionRequested += delegate(object s, CoreWebView2PermissionRequestedEventArgs e)
			{
				e.State = CoreWebView2PermissionState.Deny;
			};
			view.CoreWebView2.NavigationStarting += delegate(object s, CoreWebView2NavigationStartingEventArgs e)
			{
				Uri result;
				if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out result) || result.GetLeftPart(UriPartial.Authority) != origin)
				{
					e.Cancel = true;
				}
			};
			view.CoreWebView2.NavigationCompleted += delegate(object s, CoreWebView2NavigationCompletedEventArgs e)
			{
				if (e.IsSuccess)
				{
					view.Visible = true;
					splash.Visible = false;
					view.BringToFront();
				}
				else
				{
					splash.Text = "页面加载失败，请退出后重新打开软件。";
					Log("Navigation failed " + e.WebErrorStatus);
				}
			};
			view.CoreWebView2.ProcessFailed += delegate(object s, CoreWebView2ProcessFailedEventArgs e)
			{
				Log("Window renderer failed " + e.ProcessFailedKind);
			};
			view.CoreWebView2.Navigate(origin);
			ready = true;
			monitor.Start();
		}
		catch (Exception ex)
		{
			Log("Startup failed " + ex.GetType().Name + ": " + ex.Message);
			MessageBox.Show(this, "启动窗口失败：" + ex.Message + "\n请确认系统为 Windows 10 22H2 / Windows 11 64 位，且软件已完整解压。", "直播守望", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			failed = true;
		}
		if (failed)
		{
			await StopCoreAsync();
			approvedClose = true;
			Close();
		}
	}

	private void SaveDownload(object sender, CoreWebView2DownloadStartingEventArgs e)
	{
		CoreWebView2Deferral deferral = e.GetDeferral();
		try
		{
			using (SaveFileDialog saveFileDialog = new SaveFileDialog())
			{
				string fileName = Path.GetFileName(e.ResultFilePath);
				saveFileDialog.FileName = fileName;
				saveFileDialog.Filter = "直播列表 (*.kd;*.csv)|*.kd;*.csv|所有文件 (*.*)|*.*";
				saveFileDialog.OverwritePrompt = true;
				e.Handled = true;
				if (saveFileDialog.ShowDialog(this) == DialogResult.OK)
				{
					e.ResultFilePath = saveFileDialog.FileName;
				}
				else
				{
					e.Cancel = true;
				}
			}
		}
		finally
		{
			deferral.Complete();
		}
	}

	private async void HandleMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
	{
		if (e.Source != origin + "/" || busyClose || !ready)
		{
			return;
		}
		try
		{
			Dictionary<string, object> message = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(e.WebMessageAsJson);
			switch (Convert.ToString(message["action"]))
			{
			case "login":
				if (loginWindow != null && !loginWindow.IsDisposed)
				{
					loginWindow.Activate();
					break;
				}
				loginWindow = new DouyinWindow(browserEnvironment, "连接抖音 · 扫码登录后自动返回", "https://live.douyin.com/", true);
				loginWindow.SaveAuthorization = CompleteAuthAsync;
				loginWindow.Show(this);
				break;
			case "play":
			{
				string id = Convert.ToString(message["id"]);
				string quality = (message.ContainsKey("quality") ? Convert.ToString(message["quality"]) : "");
				JavaScriptSerializer serializer = new JavaScriptSerializer();
				await view.CoreWebView2.ExecuteScriptAsync("playRoom(" + serializer.Serialize(id) + "," + serializer.Serialize(quality) + ");");
				break;
			}
			case "copy":
			{
				string text = Convert.ToString(message["text"]);
				if (text.Length > 0 && text.Length < 65536)
				{
					Clipboard.SetText(text);
				}
				break;
			}
			}
		}
		catch (Exception ex)
		{
			MessageBox.Show(this, (ex is InvalidOperationException) ? ex.Message : "操作未完成，请稍后重试。", "直播守望", MessageBoxButtons.OK, MessageBoxIcon.Asterisk);
			Log("Desktop action failed " + ex.GetType().Name);
		}
	}

	private async Task<bool> CompleteAuthAsync(DouyinWindow window)
	{
		try
		{
			string cookie = await window.SessionCookieAsync();
			string agent = await window.UserAgentAsync();
			JavaScriptSerializer serializer = new JavaScriptSerializer();
			using (HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Post, origin + "/api/auth/douyin"))
			{
				request.Headers.Add("X-LiveWatch-Token", token);
				request.Content = new StringContent(serializer.Serialize(new
				{
					cookie = cookie,
					user_agent = agent
				}), Encoding.UTF8, "application/json");
				using (HttpResponseMessage response = await http.SendAsync(request))
				{
					if (!response.IsSuccessStatusCode)
					{
						throw new InvalidOperationException("连接尚未完成，请稍后点击重试。");
					}
				}
			}
			if (!view.IsDisposed)
			{
				view.CoreWebView2.PostWebMessageAsJson("{\"event\":\"authorized\"}");
			}
			return true;
		}
		catch (Exception ex)
		{
			window.ShowAuthorizationStatus((ex is InvalidOperationException) ? ex.Message : "暂时无法保存连接，请点击下方按钮重试。", true);
			Log("Authorization save failed " + ex.GetType().Name);
			return false;
		}
	}

	private async Task<bool> TryAttachAsync()
	{
		try
		{
			string infoPath = Path.Combine(data, "data", "instance.json");
			if (!File.Exists(infoPath))
			{
				return false;
			}
			Dictionary<string, object> info = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(infoPath));
			int port = Convert.ToInt32(info["port"]);
			if (port < 1 || port > 65535)
			{
				return false;
			}
			string candidate = "http://127.0.0.1:" + port;
			string health = await http.GetStringAsync(candidate + "/api/health");
			Dictionary<string, object> value = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(health);
			if (Convert.ToString(value["app"]) != "LiveWatch")
			{
				return false;
			}
			if (!Convert.ToString(value["version"]).StartsWith("2."))
			{
				throw new InvalidOperationException("旧版软件正在运行，请先退出旧版再打开新版。");
			}
			origin = candidate;
			return true;
		}
		catch (IOException)
		{
			return false;
		}
		catch (HttpRequestException)
		{
			return false;
		}
		catch (TaskCanceledException)
		{
			return false;
		}
		catch (ArgumentException)
		{
			return false;
		}
		catch (KeyNotFoundException)
		{
			return false;
		}
	}

	private async void MonitorCore(object sender, EventArgs args)
	{
		if (busyClose || !ready)
		{
			return;
		}
		monitor.Stop();
		try
		{
			if (core != null && core.HasExited)
			{
				approvedClose = true;
				Close();
				return;
			}
			await http.GetStringAsync(origin + "/api/health");
			failures = 0;
		}
		catch (HttpRequestException)
		{
			failures++;
		}
		catch (TaskCanceledException)
		{
			failures++;
		}
		if (failures >= 2)
		{
			busyClose = true;
			splash.Text = "正在结束录制并退出…";
			splash.Visible = true;
			splash.BringToFront();
			await StopCoreAsync();
			approvedClose = true;
			Close();
		}
		else if (!base.IsDisposed)
		{
			monitor.Start();
		}
	}

	private async Task StopCoreAsync()
	{
		if (origin != "" && token != "")
		{
			try
			{
				using (HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Post, origin + "/api/shutdown"))
				{
					request.Headers.Add("X-LiveWatch-Token", token);
					request.Content = new StringContent("{}", Encoding.UTF8, "application/json");
					using (await http.SendAsync(request))
					{
					}
				}
			}
			catch (HttpRequestException)
			{
			}
			catch (TaskCanceledException)
			{
			}
			catch (ObjectDisposedException)
			{
			}
		}
		if (core == null)
		{
			return;
		}
		try
		{
			await Task.Run(() => core.WaitForExit(45000));
		}
		catch (InvalidOperationException)
		{
		}
	}

	private async void ClosingAsync(object sender, FormClosingEventArgs args)
	{
		if (!approvedClose)
		{
			args.Cancel = true;
			if (!busyClose && (!ready || MessageBox.Show(this, "关闭窗口会停止监控并保存正在录制的视频。\n需要继续后台录制时，请使用最小化。\n\n确定退出？", "退出直播守望", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) == DialogResult.Yes))
			{
				busyClose = true;
				monitor.Stop();
				splash.Text = "正在结束录制并保存文件，请稍候…";
				splash.Visible = true;
				splash.BringToFront();
				await StopCoreAsync();
				approvedClose = true;
				Close();
			}
		}
	}
}
