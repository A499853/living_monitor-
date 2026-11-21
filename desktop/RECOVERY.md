# 桌面宿主恢复说明

此目录是从随包的 `直播守望.exe` 恢复出的可编辑、可重新编译 C# 工程。恢复日期为 2026-11-21。原始桌面源码、项目文件、注释、调试符号及历史测试未在工作区找到，因此不能把这里称为逐字找回的原始源码。

后端 Python 和网页源码已直接保存在项目根目录的 `app/`。桌面宿主负责独立窗口、WebView2、后端启动/退出和抖音授权窗口。

## 构建

从项目根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-desktop.ps1
```

脚本使用 Windows 自带的 `.NET Framework` x64 C# 5 编译器，以及根目录已恢复的 WebView2 SDK DLL，无需下载、安装 SDK 或访问网络。目标运行环境为 Windows 10 22H2 / Windows 11 x64、.NET Framework 4.8。产物写入 `build/desktop/`；不会默认替换项目根目录 EXE。

该宿主保留原来的路径规则：程序资源 `app/`、`tools/python/`、`tools/webview2/` 必须与 EXE 在同一个根目录。`--data-dir` 仅指定数据目录，不会更改资源目录。直接双击仅含构建产物的目录中的 EXE 会因缺少这些资源而失败。

若要用恢复后的源码版本运行，先正常退出直播守望，再执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-desktop.ps1 -Install
```

这会先把根目录原 EXE 及配置备份到 `build/desktop/backups/`，再安装新 EXE 和配置。随后运行根目录的 `start.cmd`。发布归档 `release/` 不会被改动，脚本也拒绝把该目录用作构建输出。

`直播守望.csproj` 另提供 `net48`、Windows Forms、x64 工程给具备 .NET SDK 和 .NET Framework 4.8 targeting pack 的 IDE。当前机器未安装这些开发包，因此本次验证采用上面的离线 C# 编译脚本；系统自带的 MSBuild 4 不支持 SDK 风格工程。

这里的“可重新构建”指相同源码、依赖和命令能生成可运行宿主。旧版系统编译器不会生成逐字节确定性产物，时间戳、模块标识和编译器生成代码均可能与发布 EXE 不同。

## 恢复来源

- 输入：项目根目录 `直播守望.exe`，与 `release/LiveWatch-2.8-Windows10-11-x64/直播守望.exe` 的 SHA-256 一致：`4F92A3291F2BB277D6DF655E32A5B02C524A3C917F9C232762E8B0AF8E67C88D`。
- 反编译工具：ILSpy command line 9.1.0.7988，来自 NuGet 官方源 `https://api.nuget.org/v3-flatcontainer/ilspycmd/9.1.0.7988/ilspycmd.9.1.0.7988.nupkg`。下载包 SHA-256 为 `2B5058F5CCC164C33B7AABF1A5EB0CF3D3A6AF6C145AAF58EFD3ED891443AF7C`。
- 仅用于反编译的本地运行时：Microsoft .NET 8.0.34 win-x64 ZIP。已按微软 8.0 发布元数据校验 SHA-512；ZIP 的 SHA-256 为 `7C605E2A187CB315D90B6B595358260443DF863040D6C95597F8D8A2F2061786`。
- 工具均放在项目内 `.build-tools/`，没有全局安装或修改 PATH。完成恢复后，日常构建不依赖该目录。

恢复命令（项目根目录）：

```powershell
& .build-tools/dotnet/dotnet.exe .build-tools/ilspycmd/tools/net8.0/any/ilspycmd.dll --disable-updatecheck -p -lv CSharp5 -r . -o desktop '直播守望.exe'
```

不要直接把上述命令再次输出到已修改的 `desktop/`；要比较原始反编译结果，请使用新的输出目录。

## 恢复时的调整和验证

1. ILSpy 自动推断 `net40`，但宿主使用 `async` / `System.Net.Http`，且原 EXE.config 明确要求 .NET Framework 4.8。因此工程目标改为 `net48`，原配置和 DPI/权限 manifest 保留。
2. `DouyinWindow.SessionCookieAsync()` 的异步循环被 ILSpy 输出为未初始化的数组/索引和错误的 `goto`，无法编译。已根据原始 IL 还原成依次遍历 `https://www.douyin.com/`、`https://live.douyin.com/` 的循环；保留会话/有效期筛选、分号检查、后取值覆盖同名 Cookie、授权字段判定和输出方式。原始状态机 IL 保存在 `recovery/SessionCookieAsync.original.il` 便于核对。
3. 其余 C# 逻辑来自反编译结果，未作性能或产品功能改写。
4. 已在 PowerShell 7 和 Windows PowerShell 5.1 中实际编译通过，C# 编译器版本为 14.8.10001.0；重建程序集的名称、版本及引用程序集版本与原宿主一致。没有读取用户登录 Cookie。
5. 重建 EXE 已在隔离数据目录 `build/ui-rebuilt/` 实际启动：健康接口报告 2.8.0，窗口响应正常，WebView2Loader 与 EmbeddedBrowserWebView 已加载，独立 Python/WebView2 子进程正常。结束本次测试宿主后，后端的宿主存活检查在约 1 秒内释放 `instance.json`。没有改动正在使用的原版窗口。
6. 上述启动测试在 `build/desktop/` 下临时创建了指向项目根 `app/`、`tools/` 的目录联接，使相对路径布局成立；验证后已仅移除联接本身，实际项目文件保留。
7. 编译与启动检查不等于线上登录/录制或全天运行验收；实际恢复检查另见根目录恢复记录。
