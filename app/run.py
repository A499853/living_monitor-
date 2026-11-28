"""Application entry point; no third-party Python packages are required."""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
from pathlib import Path
import sys
import threading
import urllib.request
import subprocess

from livewatch import __version__


def notify_failure(message):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "直播守望 · 启动失败", 0x10)
    else:
        print(message, file=sys.stderr)


class InstanceLock:
    def __init__(self, root):
        self.path = root / "data" / "instance.lock"
        self.info = root / "data" / "instance.json"
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close()
            self.file = None
            return False

    def release(self):
        if self.file:
            try:
                self.info.unlink(missing_ok=True)
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.file.close()
                self.file = None


def owner_is_alive(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="直播守望 · Windows 本地直播监控")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--owner-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    root = args.data_dir.resolve()
    if not args.no_browser:
        desktop = Path(__file__).resolve().parent.parent / "直播守望.exe"
        if desktop.is_file() and os.name == "nt":
            subprocess.Popen([str(desktop), "--data-dir", str(root)], creationflags=subprocess.CREATE_NO_WINDOW)
            return 0
        notify_failure("请双击免安装包中的直播守望.exe。源码调试可使用 --no-browser 启动本地服务。")
        return 1
    try:
        for name in ("data", "logs", "video", "time_begin_over"):
            (root / name).mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(root)
        if not lock.acquire():
            # A second double-click reopens the current instance instead of recording twice.
            try:
                info = json.loads(lock.info.read_text(encoding="utf-8"))
                port = int(info["port"])
                if not 1 <= port <= 65535:
                    raise ValueError()
                origin = f"http://127.0.0.1:{port}"
                with urllib.request.urlopen(origin + "/api/health", timeout=2) as response:
                    if json.load(response).get("app") != "LiveWatch":
                        raise ValueError()
                return 0
            except Exception:
                notify_failure("软件正在启动或退出，请稍候几秒再双击。")
                return 1
        from livewatch.logging_config import AppLogHandler
        handler = AppLogHandler(root / 'logs' / 'app.log')
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
    except OSError:
        notify_failure("无法写入软件目录。请完整解压到有写入权限的文件夹（如 D:\\直播守望），再重新启动。")
        return 1

    store = engine = server = None
    try:
        from livewatch.storage import Store
        from livewatch.engine import Engine
        from livewatch.server import LocalServer
        store = Store(root)
        engine = Engine(store)
        engine.maintenance.reconcile_inactive(active_paths=[state['record_resume']['path'] for state in engine.states.values() if state.get('record_resume')])
        handler.register_active(engine.maintenance)
        engine.log_handler=handler
        server = LocalServer(root, store, engine, args.port)
        lock.info.write_text(json.dumps({"port": server.server_port, "pid": os.getpid()}), encoding="utf-8")
        engine.start()
        logging.info("LiveWatch %s started on loopback port %s", __version__, server.server_port)
        if args.owner_pid:
            def watch_window():
                while not server._BaseServer__is_shut_down.wait(1):
                    if not owner_is_alive(args.owner_pid):
                        server.shutdown()
                        return
            threading.Thread(target=watch_window, daemon=True, name="window-owner").start()
        if sys.stdout:
            print(server.origin, flush=True)
        server.serve_forever(poll_interval=0.2)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.error("Application stopped unexpectedly: %s", type(exc).__name__)
        notify_failure("启动失败。请查看 logs\\app.log；确认未从压缩包内直接运行、路径可写且未占用指定端口。")
        return 1
    finally:
        if server:
            server.server_close()
        if engine:
            engine.close()
            handler.drain_maintenance(engine.maintenance)
            logging.getLogger().removeHandler(handler)
            handler.close()
            try: engine.maintenance.release_log(handler.baseFilename)
            except (OSError,ValueError): pass
        if store:
            store.close()
        lock.release()
        logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
