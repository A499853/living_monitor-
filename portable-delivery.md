# Windows 免安装交付

将 ZIP **完整解压到有写入权限的文件夹**（例如 `D:\直播守望`），双击 `直播守望.exe`。请勿在压缩包预览窗口中直接运行。

第一次启动显示依赖准备窗口，自动下载 Python 3.12.10、FFmpeg、WebView2 固定版本运行库到 EXE 同目录的 `tools/`，下载总量约 338 MiB。准备后自动进入本地程序；后续启动复用这些文件，无需 Python、pip、浏览器或管理员安装。联网监控直播仍需网络。支持 Windows 10 22H2 / Windows 11 x64，自带的 .NET Framework 4.8 是启动器的系统前提。

首次准备建议至少预留 2 GB 磁盘空间，录像空间另计。可以取消下载；重新打开会保留已经安装的依赖，并在下载源支持时续传未完成文件。下载失败会重试，最终失败会说明原因，记录在 `logs/bootstrap.log`。如所在网络无法访问下载源，换可用网络后重新打开。不会将失败或校验不符的文件作为运行库使用。

所有下载均使用发布包中固定的 HTTPS 地址、大小和 SHA-256 清单。解压在暂存目录完成，检查必需文件后才替换安装目录；启动会检查运行库收据的文件大小和缺失文件。压缩包路径不能越出暂存目录。下载请求有连接、读取和总耗时限制；WebView2 解压也有期限。运行库和下载文件不会写入系统目录或修改 PATH。

`app/`、两个依赖 JSON、WebView2 SDK 的三个 DLL 必须随 EXE 保留。它们体积较小，已在轻量 ZIP 中提供。用户数据、录像及设置默认也保存在 EXE 所在目录，搬迁软件时请整体移动文件夹。

## 开发者重新打包

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-portable.ps1
```

输出为 `artifacts/LiveWatch-Optimized-Windows-x64.zip` 及 SHA-256 文件。只包含启动器、程序源码、界面、清单和许可证，不打包已下载的 `tools/` 或任何用户数据。压缩条目顺序和时间固定；C# 5 编译器本身不保证每次 EXE 字节一致，所以相同二进制输入可复现 ZIP，但重新编译可能改变 ZIP 哈希。

仅准备依赖、不启动界面的诊断命令：

```powershell
Start-Process -FilePath '.\直播守望.exe' -ArgumentList '--prepare-dependencies' -Wait
```

该模式将进度写入 `logs/bootstrap.log`，成功退出码为 0，失败为 1。常规用户直接双击 EXE 即可。

## 本次验证（2026-11-22）

- 17 项启动器回归断言通过：盘根和带空格目录、Windows 参数传递、相同数据目录共用实例锁、校验错误、缺失文件、篡改文件大小、损坏的安装收据、ZIP 路径越界、取消、安装失败保留原目录及成功替换。
- 在 `build/bootstrap-smoke/` 的空 `tools/` 目录实际联网下载三个依赖并完成 SHA-256 校验及解压。FFmpeg 下载到 86% 时强制结束测试进程，再启动后从 86% 继续，最终校验通过。
- 使用下载的 Python 成功导入 SSL、SQLite 等标准库，下载的 FFmpeg 正确报告 7.1。使用这些运行库启动实际桌面 EXE，健康接口返回 `LiveWatch 2.8.0`，宿主加载了本地 `WebView2Loader.dll` 和 `EmbeddedBrowserWebView.dll`。
- 通过本地退出接口关闭隔离测试实例，窗口、后端和浏览器进程均退出，实例文件移除。第二次依赖准备退出码为 0，直接复用三个运行库，没有再次下载。

可读验证记录在开发目录 `artifacts/portable-bootstrap-report.json`，原始下载进度在 `build/bootstrap-smoke/logs/bootstrap.log`。这验证了当前机器上的交付启动链路；没有代替不同 Windows 版本、代理网络和全天录制的实际验收。
