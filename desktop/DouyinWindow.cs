using System;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal sealed class DouyinWindow : Form
{
	private readonly WebView2 browser;

	private readonly CoreWebView2Environment environment;

	private readonly string address;

	private readonly bool authorization;

	private readonly Button finish;

	private readonly Timer authorizationPoll;

	private Label authorizationStatus;

	private bool checkingAuthorization;

	private bool automaticSaveFailed;

	internal Func<DouyinWindow, Task<bool>> SaveAuthorization;

	internal DouyinWindow(CoreWebView2Environment environment, string title, string address, bool authorization)
	{
		this.environment = environment;
		this.address = address;
		this.authorization = authorization;
		Text = title;
		base.Size = new Size(1100, 820);
		MinimumSize = new Size(750, 600);
		base.StartPosition = FormStartPosition.CenterParent;
		browser = new WebView2
		{
			Dock = DockStyle.Fill
		};
		base.Controls.Add(browser);
		finish = new Button
		{
			Text = "已完成登录？点击连接",
			Dock = DockStyle.Bottom,
			Height = 46,
			Enabled = false,
			Font = new Font("Microsoft YaHei UI", 10f)
		};
		if (authorization)
		{
			base.Controls.Add(finish);
			authorizationStatus = new Label
			{
				Dock = DockStyle.Top,
				Height = 68,
				Padding = new Padding(18, 10, 18, 8),
				Text = "使用手机抖音扫码，或选择你习惯的登录方式。\n登录成功后会自动连接并返回；密码只在抖音官方页面输入。",
				ForeColor = Color.FromArgb(55, 76, 105),
				BackColor = Color.FromArgb(241, 247, 255),
				Font = new Font("Microsoft YaHei UI", 10f)
			};
			base.Controls.Add(authorizationStatus);
		}
		authorizationPoll = new Timer
		{
			Interval = 1200
		};
		authorizationPoll.Tick += async delegate
		{
			await TryFinishAuthorization(false);
		};
		finish.Click += async delegate
		{
			await TryFinishAuthorization(true);
		};
		base.Shown += Initialize;
		base.FormClosed += delegate
		{
			authorizationPoll.Stop();
			authorizationPoll.Dispose();
			browser.Dispose();
		};
	}

	private static bool Official(Uri uri)
	{
		string text = uri.Host.ToLowerInvariant();
		return uri.Scheme == "https" && (text == "douyin.com" || text.EndsWith(".douyin.com") || text == "snssdk.com" || text.EndsWith(".snssdk.com") || text == "bytedance.com" || text.EndsWith(".bytedance.com"));
	}

	private async void Initialize(object sender, EventArgs args)
	{
		try
		{
			if (authorization)
			{
				// Reuse the persistent profile under data/webview2. Saved local
				// authorization is also held by the backend without an expiry;
				// do not rewrite the platform's cookie expiration dates here.
				await browser.EnsureCoreWebView2Async(environment);
			}
			else
			{
				CoreWebView2ControllerOptions options = environment.CreateCoreWebView2ControllerOptions();
				options.ProfileName = "AnonymousPlayback";
				options.IsInPrivateModeEnabled = true;
				await browser.EnsureCoreWebView2Async(environment, options);
			}
			browser.CoreWebView2.Settings.IsWebMessageEnabled = false;
			browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
			browser.CoreWebView2.Settings.IsStatusBarEnabled = false;
			browser.CoreWebView2.NavigationStarting += delegate(object s, CoreWebView2NavigationStartingEventArgs e)
			{
				Uri result;
				if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out result) || !Official(result))
				{
					e.Cancel = true;
				}
			};
			browser.CoreWebView2.NewWindowRequested += delegate(object s, CoreWebView2NewWindowRequestedEventArgs e)
			{
				e.Handled = true;
				Uri result;
				if (Uri.TryCreate(e.Uri, UriKind.Absolute, out result) && Official(result))
				{
					browser.CoreWebView2.Navigate(e.Uri);
				}
			};
			browser.CoreWebView2.PermissionRequested += delegate(object s, CoreWebView2PermissionRequestedEventArgs e)
			{
				e.State = CoreWebView2PermissionState.Deny;
			};
			browser.CoreWebView2.DownloadStarting += delegate(object s, CoreWebView2DownloadStartingEventArgs e)
			{
				e.Cancel = true;
			};
			browser.CoreWebView2.Navigate(address);
			finish.Enabled = true;
			if (authorization)
			{
				authorizationPoll.Start();
			}
		}
		catch (Exception)
		{
			MessageBox.Show(this, "无法打开抖音窗口，请完整解压软件后重试。", "直播守望");
			Close();
		}
	}

	internal void ShowAuthorizationStatus(string message, bool failed)
	{
		if (!base.IsDisposed && authorizationStatus != null)
		{
			authorizationStatus.Text = message;
			authorizationStatus.ForeColor = (failed ? Color.FromArgb(164, 95, 35) : Color.FromArgb(55, 76, 105));
		}
	}

	private async Task TryFinishAuthorization(bool manual)
	{
		if (!authorization || checkingAuthorization || base.IsDisposed || browser.CoreWebView2 == null || (!manual && automaticSaveFailed))
		{
			return;
		}
		checkingAuthorization = true;
		finish.Enabled = false;
		try
		{
			try
			{
				await SessionCookieAsync();
			}
			catch (InvalidOperationException)
			{
				if (manual)
				{
					ShowAuthorizationStatus("还未检测到登录成功，请先在下方抖音页面完成扫码或登录。", false);
				}
				return;
			}
			ShowAuthorizationStatus("已检测到登录，正在连接…", false);
			if (SaveAuthorization != null && await SaveAuthorization(this))
			{
				authorizationPoll.Stop();
				Close();
			}
			else
			{
				automaticSaveFailed = true;
			}
		}
		catch (Exception)
		{
			ShowAuthorizationStatus("暂时无法检查登录状态，请稍后点击连接。", true);
		}
		finally
		{
			checkingAuthorization = false;
			if (!base.IsDisposed)
			{
				finish.Enabled = true;
			}
		}
	}

	internal async Task<string> SessionCookieAsync()
	{
		Dictionary<string, string> values = new Dictionary<string, string>();
		// Restored from the original async state machine: iterate both official URLs.
		// ILSpy 9.1 emitted invalid uninitialized goto/array locals for this loop.
		foreach (string url in new string[] { "https://www.douyin.com/", "https://live.douyin.com/" })
		{
			foreach (CoreWebView2Cookie item in await browser.CoreWebView2.CookieManager.GetCookiesAsync(url))
			{
				if ((item.IsSession || item.Expires > DateTime.UtcNow) && !item.Name.Contains(";") && !item.Value.Contains(";"))
				{
					values[item.Name] = item.Value;
				}
			}
		}
		if (!new string[3] { "sessionid", "sessionid_ss", "sid_tt" }.Any((string key) => values.ContainsKey(key) && values[key].Length > 0))
		{
			throw new InvalidOperationException("请先在抖音官方页面完成登录，再点击授权。");
		}
		return string.Join("; ", values.Select((KeyValuePair<string, string> pair) => pair.Key + "=" + pair.Value));
	}

	internal async Task<string> UserAgentAsync()
	{
		string json = await browser.CoreWebView2.ExecuteScriptAsync("navigator.userAgent");
		return new JavaScriptSerializer().Deserialize<string>(json);
	}
}
