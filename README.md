# 直播守望 LiveWatch 2.8.0 开发工程

本目录从 `release/LiveWatch-2.8-Windows10-11-x64` 还原，可直接运行和修改。Python 后端、HTML/CSS/JavaScript 前端、原版桌面程序、Python/FFmpeg/WebView2 运行库、许可证和发布文档均已复制到项目根目录下，保留原有相对布局。原始 `release` 目录保留为对照副本。

## 启动

- 桌面窗口：双击根目录 `start.cmd` 或 `直播守望.exe`。
- 开发服务：双击 `dev.cmd`；控制台会输出本机地址，在浏览器中打开即可查看页面。抖音内置登录等桌面桥接功能仍需桌面窗口。按 Ctrl+C 停止开发服务。
- 命令行（在本目录执行）：

```powershell
.\tools\python\python.exe .\run.py --version
.\tools\python\python.exe .\run.py --no-browser
```

根目录 `run.py` 是开发入口，默认将数据存入本项目根目录；可传 `--data-dir <其他目录>` 隔离数据。若使用隔离目录，需在设置中指定本项目 `tools/ffmpeg.exe` 的完整路径。直接运行 `app/run.py` 时应显式传入 `--data-dir`。

Windows 10/11 x64 使用随项目的 Python 3.12.10、FFmpeg 和 WebView2，以及系统 .NET Framework 4.8。后端只依赖 Python 标准库，无需安装 pip 包。也可用正常安装的 Python 3.12 运行根目录 `run.py`。

## 目录

| 路径 | 用途 |
| --- | --- |
| `app/run.py` | 发布包原始服务入口 |
| `app/livewatch/` | 监控、录制、存储、本地 API 等 Python 源码 |
| `app/livewatch/probe_pool.py`、`probe_worker.py` | 有界探测子进程、硬截止时间与故障回收 |
| `app/livewatch/web/` | 原始网页界面源码，修改后重启/刷新生效 |
| `desktop/` | 从发布 EXE 恢复的 C# 桌面源码与工程 |
| `scripts/` | 桌面构建、文件校验与离线冒烟验证 |
| `tools/` | Python、FFmpeg、固定版本 WebView2 |
| `licenses/` | 随发布包保留的第三方许可证 |
| `data/`、`logs/`、`video/`、`time_begin_over/` | 启动后生成的本地数据、日志、录像和开播记录 |
| `release/` | 原始发布副本 |

## 还原与验证

`restoration-manifest.json` 记录原始发布文件的大小和 SHA-256。校验不依赖发布目录；附加 `--check-release` 可同时验证原包。

```powershell
.\tools\python\python.exe .\scripts\check-restoration.py --check-release
.\tools\python\python.exe .\scripts\verify-project.py
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1
```

编译结果位于 `build/desktop/`。修改桌面源码后，关闭软件，使用 `build-desktop.ps1 -Install` 将新 EXE 和配置安装到项目根目录（会先备份），再运行 `start.cmd`。桌面构建的来源、限制及详细步骤见 [desktop/RECOVERY.md](desktop/RECOVERY.md)。本次验证结果见 [恢复记录.md](恢复记录.md)。

桌面原始 C# 工程、Git 历史以及历史测试源码未包含在发布包中；相邻旧项目目录为空。`desktop/` 属于从现有 EXE 反编译恢复并补齐构建配置的工程，无法恢复原始注释和所有原始命名。Python 和前端则是发布包中的原始源文件。随包 `验收记录.md` 中的历史 306 项测试不代表本次执行结果；本次新增离线验证的结果另行记录。

## 性能说明

免安装版本直接执行 CPython、FFmpeg 和 WebView2，不存在每次运行解压整包的步骤。2026-11-21 的并发改造保留现有桌面窗口和 HTTP API，默认采用独立补位调度：慢房间和短暂故障补查不再阻塞本轮其余房间；仍可选择原整批模式。

新建配置默认启用高频状态模式：每房间5秒发起周期、32路总并发、32路抖音状态并发、单次4秒截止时间。周期、超时和1～64并发都可在设置页调整；高频模式不受旧的逐房间间隔与整轮等待影响。同一房间不会重入，慢房间不阻塞健康房间。手机观看身份隔离默认开启，监控请求不使用手机账号会话。

5秒轮询并不保证5秒检测延迟；排队、请求、平台更新和界面刷新都会增加时间。未知/过期状态明确展示，并保留最近确认结果。高频模式要求固定直播间地址或有效状态接口，账号/主页/短分享链接需先替换为固定房间地址。关闭高频模式后可使用原兼容探测（总并发1～16、抖音1～5），支持较慢的主页解析和完整元数据路径。容量估算、使用方法、真实能力边界和24小时长测命令见 [弱网150房间方案.md](弱网150房间方案.md)，本次执行结果见 [高频监控验收.md](高频监控验收.md)。

默认开启探测进程隔离，工作进程按需启动并复用，只负责网络检查；主进程集中维护 SQLite、直播场次和录制。检查超时或工作进程崩溃会回收该进程，下次检查按需重建；检查失败保留未知/最近确认状态，不当作下播。进程会增加内存开销，设置中可关闭隔离改用线程。状态 API 的 `monitor` 包含工作进程数、回收次数和超时次数，运行资源日志也记录这些数值。

录制器使用 FFmpeg `-c copy`，主要受网络、磁盘和并发录制数量影响。内嵌播放器使用 `libx264` 软件转码（每路 2 线程、最大宽度 1280），打开多个画面可能增加 CPU 占用。监控频率还受到批次设置、网络和平台接口响应影响。还原后的源码可用于后续测量和有针对性的优化。

本轮没有新增 C++ 模块：已确认的瓶颈是调度等待与无法取消的网络系统调用，媒体处理已由原生 FFmpeg 完成。进程隔离解决故障范围与回收问题；平台空响应、限流或网络断开仍需要退避，不能靠更换语言保证成功率。后续仅在 CPU 分析确认热点后迁移对应模块。验证命令和边界见 [并发改造与验证.md](并发改造与验证.md)。
