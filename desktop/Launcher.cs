using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class Launcher
{
	[DllImport("user32.dll")]
	private static extern bool SetProcessDpiAwarenessContext(IntPtr value);

	[STAThread]
	private static void Main(string[] args)
	{
		string text = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
		string text2 = text;
		for (int i = 0; i + 1 < args.Length; i++)
		{
			if (args[i] == "--data-dir")
			{
				text2 = Path.GetFullPath(args[++i]);
			}
		}
		string text3;
		using (SHA256 sHA = SHA256.Create())
		{
			text3 = BitConverter.ToString(sHA.ComputeHash(Encoding.UTF8.GetBytes(text2.ToUpperInvariant()))).Replace("-", "").Substring(0, 24);
		}
		bool createdNew;
		using (Mutex mutex = new Mutex(true, "Local\\LiveWatchWindow-" + text3, out createdNew))
		{
			using (EventWaitHandle eventWaitHandle = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\LiveWatchActivate-" + text3))
			{
				if (!createdNew)
				{
					eventWaitHandle.Set();
					return;
				}
				try
				{
					try
					{
						SetProcessDpiAwarenessContext(new IntPtr(-4));
					}
					catch (EntryPointNotFoundException)
					{
					}
					Application.EnableVisualStyles();
					Application.SetCompatibleTextRenderingDefault(false);
					MainWindow window = new MainWindow(text, text2);
					try
					{
						RegisteredWaitHandle registeredWaitHandle = ThreadPool.RegisterWaitForSingleObject(eventWaitHandle, delegate
						{
							try
							{
								window.BeginInvoke((Action)delegate
								{
									if (window.WindowState == FormWindowState.Minimized)
									{
										window.WindowState = FormWindowState.Normal;
									}
									window.Show();
									window.Activate();
								});
							}
							catch (InvalidOperationException)
							{
							}
						}, null, -1, false);
						Application.Run(window);
						registeredWaitHandle.Unregister(null);
					}
					finally
					{
						if (window != null)
						{
							((IDisposable)window).Dispose();
						}
					}
				}
				catch (Exception ex2)
				{
					MessageBox.Show("无法打开软件窗口，请完整解压到可写文件夹。\n" + ex2.GetType().Name, "直播守望", MessageBoxButtons.OK, MessageBoxIcon.Hand);
				}
				finally
				{
					mutex.ReleaseMutex();
				}
			}
		}
	}
}
