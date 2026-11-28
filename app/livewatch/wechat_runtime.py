"""Own the bundled Channels bridge, current-user certificate and proxy changes.

Only this module changes Windows integration. Recovery is journaled before each
change, restores matching values only, and never adopts an unrelated listener.
The capture receiver can close while this runtime remains alive for monitoring.
"""
from __future__ import annotations

import csv
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

from .wechat import _NoRedirect

PROXY_KEYS = ('ProxyEnable', 'ProxyServer', 'ProxyOverride', 'AutoConfigURL', 'AutoDetect')
PROXY_VALUES = {'ProxyServer': ['127.0.0.1:2023', 1], 'ProxyOverride': ['<local>', 1],
                'AutoConfigURL': None, 'AutoDetect': [0, 4], 'ProxyEnable': [1, 4]}


class RuntimeErrorSafe(RuntimeError):
    """A message safe for the application UI; never includes command output."""


class CertificateConfirmationPending(RuntimeErrorSafe):
    """Windows needs a human certificate-removal confirmation before recovery."""


class _WindowsJob:
    def __init__(self, kernel, handle):
        self.kernel, self.handle = kernel, handle

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WindowsIntegration:
    """Small OS boundary, replaced entirely by a fake in offline checks."""

    @staticmethod
    def _run(name, args, timeout=15):
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / name
        try:
            result = subprocess.run([str(executable), *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeErrorSafe('无法完成视频号的 Windows 配置操作。') from None
        if result.returncode:
            raise RuntimeErrorSafe('Windows 未能完成视频号配置，请检查当前用户权限。')
        return result.stdout

    def lock(self, path):
        if os.name != 'nt':
            raise RuntimeErrorSafe('内置视频号捕获目前需要 Windows。')
        import msvcrt
        handle = path.open('a+b')
        if handle.seek(0, 2) == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            raise RuntimeErrorSafe('另一个直播守望窗口正在使用视频号捕获。') from None
        return handle

    def secure_directory(self, path):
        output = self._run('whoami.exe', ['/user', '/fo', 'csv', '/nh']).decode('utf-8', 'replace')
        rows = list(csv.reader(io.StringIO(output)))
        sid = rows[-1][-1].strip() if rows and rows[-1] else ''
        if not re.fullmatch(r'S-1-[0-9-]+', sid):
            raise RuntimeErrorSafe('无法识别当前 Windows 用户，未启动视频号捕获。')
        # Replace the DACL atomically. Merely removing inherited ACEs with
        # icacls would leave explicit grants to other users on an old directory.
        from ctypes import wintypes
        advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        flags = 'OICI' if path.is_dir() else ''
        descriptor = ctypes.c_void_p()
        sddl = 'D:P(A;' + flags + ';FA;;;' + sid + ')(A;' + flags + ';FA;;;SY)'
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise RuntimeErrorSafe('无法准备视频号私钥的访问权限。')
        try:
            present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
            if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
                raise RuntimeErrorSafe('无法读取视频号私钥的访问权限。')
            if advapi.SetNamedSecurityInfoW(str(path), 1, 4 | 0x80000000, None, None, dacl, None):
                raise RuntimeErrorSafe('无法限制视频号私钥的访问权限，未启动捕获。')
        finally:
            kernel.LocalFree(descriptor)

    @staticmethod
    def registry_read():
        import winreg
        result = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            for name in PROXY_KEYS:
                try:
                    value, kind = winreg.QueryValueEx(key, name)
                    result[name] = [value, kind]
                except FileNotFoundError:
                    result[name] = None
        return result

    @staticmethod
    def registry_write(name, entry):
        import winreg
        if name not in PROXY_KEYS:
            raise RuntimeErrorSafe('代理恢复字段无效。')
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Internet Settings', 0, winreg.KEY_SET_VALUE) as key:
            if entry is None:
                try: winreg.DeleteValue(key, name)
                except FileNotFoundError: pass
            else:
                winreg.SetValueEx(key, name, 0, entry[1], entry[0])

    @staticmethod
    def notify_proxy():
        wininet = ctypes.WinDLL('wininet', use_last_error=True)
        wininet.InternetSetOptionW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        wininet.InternetSetOptionW.restype = ctypes.c_int
        if not wininet.InternetSetOptionW(None, 39, None, 0) or not wininet.InternetSetOptionW(None, 37, None, 0):
            raise RuntimeErrorSafe('Windows 代理设置已写入，但刷新通知失败。')

    @staticmethod
    def listener_pid(port):
        api = ctypes.WinDLL('iphlpapi', use_last_error=True)
        size = ctypes.c_uint32()
        api.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                                           ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        result = api.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)
        if result not in (0, 122):
            raise RuntimeErrorSafe('无法确认视频号服务端口的所属进程。')
        buffer = ctypes.create_string_buffer(size.value)
        if api.GetExtendedTcpTable(buffer, ctypes.byref(size), False, 2, 3, 0):
            raise RuntimeErrorSafe('无法读取视频号服务端口状态。')
        count = ctypes.c_uint32.from_buffer(buffer).value
        for index in range(count):
            row = (ctypes.c_uint32 * 6).from_buffer(buffer, 4 + index * 24)
            if socket.ntohs(row[2] & 0xffff) == port:
                return int(row[5])
        return None

    @staticmethod
    def create_job():
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        class Basic(ctypes.Structure):
            _fields_ = [('ProcessTime', ctypes.c_int64), ('JobTime', ctypes.c_int64),
                ('Flags', wintypes.DWORD), ('MinWorkingSet', ctypes.c_size_t),
                ('MaxWorkingSet', ctypes.c_size_t), ('ActiveProcesses', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('Priority', wintypes.DWORD), ('Scheduling', wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]
        class Limits(ctypes.Structure):
            _fields_ = [('Basic', Basic), ('Io', Counters), ('ProcessMemory', ctypes.c_size_t),
                ('JobMemory', ctypes.c_size_t), ('PeakProcess', ctypes.c_size_t), ('PeakJob', ctypes.c_size_t)]
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise RuntimeErrorSafe('无法创建视频号进程保护。')
        limits = Limits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(handle)
            raise RuntimeErrorSafe('无法配置视频号进程保护。')
        return _WindowsJob(kernel, handle)

    @staticmethod
    def assign_job(job, process):
        from ctypes import wintypes
        job.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not job.kernel.AssignProcessToJobObject(job.handle, wintypes.HANDLE(int(process._handle))):
            raise RuntimeErrorSafe('无法将视频号服务纳入进程保护，已取消启动。')

    @staticmethod
    def process_key(pid):
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() in (87, 1168):
                return None
            raise RuntimeErrorSafe('暂时无法确认视频号进程身份。')
        try:
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                raise RuntimeErrorSafe('暂时无法确认视频号进程状态。')
            if exit_code.value != 259:
                return None
            kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            length = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(length.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                raise RuntimeErrorSafe('暂时无法确认视频号进程路径。')
            class FileTime(ctypes.Structure):
                _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]
            values = [FileTime() for _ in range(4)]
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FileTime)] * 4
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                raise RuntimeErrorSafe('暂时无法确认视频号进程创建时间。')
            return {'pid': pid, 'image': str(Path(path.value).resolve()),
                    'created': str((values[0].high << 32) | values[0].low)}
        finally:
            kernel.CloseHandle(handle)

    @staticmethod
    def terminate_record(record):
        # A PID can be reused after a crash; verify path and creation time before
        # opening a termination handle, then recheck through that handle below.
        if not record or WindowsIntegration.process_key(record['pid']) != record:
            return
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        handle = kernel.OpenProcess(0x100001, False, record['pid'])
        if not handle:
            if WindowsIntegration.process_key(record['pid']) == record:
                raise RuntimeErrorSafe('无法结束本次视频号服务，已保留恢复记录。')
            return
        try:
            if WindowsIntegration.process_key(record['pid']) == record:
                if not kernel.TerminateProcess(handle, 0):
                    raise RuntimeErrorSafe('视频号服务尚未退出，请重试关闭。')
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                if kernel.WaitForSingleObject(handle, 3000) != 0:
                    raise RuntimeErrorSafe('视频号服务退出超时，已保留恢复记录。')
        finally:
            kernel.CloseHandle(handle)

    @staticmethod
    def _certificate_action(fingerprint, remove=False):
        # Open the physical CurrentUser registry store, not the logical SYSTEM
        # store which can also inherit machine roots. Only the exact journaled
        # certificate is eligible for removal; never run certutil's CLI here.
        if not isinstance(fingerprint, str) or not re.fullmatch(r'[A-Fa-f0-9]{40}', fingerprint):
            raise RuntimeErrorSafe('视频号证书指纹无效。')
        from ctypes import wintypes
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        crypt.CertOpenStore.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.c_void_p]
        crypt.CertOpenStore.restype = ctypes.c_void_p
        name = ctypes.create_unicode_buffer('ROOT')
        # CERT_STORE_PROV_SYSTEM_REGISTRY_W, CURRENT_USER, OPEN_EXISTING.
        store = crypt.CertOpenStore(ctypes.c_void_p(13), 0, None, 0x10000 | 0x4000, name)
        if not store:
            raise RuntimeErrorSafe('无法读取当前用户的证书状态。')
        class Blob(ctypes.Structure):
            _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        raw = bytes.fromhex(fingerprint)
        data = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        blob = Blob(len(raw), data)
        crypt.CertFindCertificateInStore.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        crypt.CertFindCertificateInStore.restype = ctypes.c_void_p
        crypt.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
        crypt.CertDeleteCertificateFromStore.argtypes = [ctypes.c_void_p]
        crypt.CertDeleteCertificateFromStore.restype = wintypes.BOOL
        crypt.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        try:
            certificate = crypt.CertFindCertificateInStore(store, 0x10001, 0, 0x10000, ctypes.byref(blob), None)
            if certificate:
                if remove:
                    # The delete API consumes the context even when it fails.
                    if not crypt.CertDeleteCertificateFromStore(certificate):
                        raise RuntimeErrorSafe('无法移除本次视频号证书，已保留恢复记录。')
                else:
                    crypt.CertFreeCertificateContext(certificate)
                return True
            if ctypes.get_last_error() & 0xffffffff != 0x80092004:  # CRYPT_E_NOT_FOUND
                raise RuntimeErrorSafe('无法确认当前用户的视频号证书状态。')
            return False
        finally:
            crypt.CertCloseStore(store, 0)

    @staticmethod
    def certificate_present(fingerprint):
        return WindowsIntegration._certificate_action(fingerprint)

    def certificate_add(self, path):
        self._run('certutil.exe', ['-user', '-addstore', 'Root', str(path)])

    def certificate_remove(self, fingerprint):
        if not isinstance(fingerprint, str) or not re.fullmatch(r'[A-Fa-f0-9]{40}', fingerprint):
            raise RuntimeErrorSafe('视频号证书指纹无效。')
        # CurrentUser Root deletion can show a Windows confirmation dialog.
        # Keep that supported flow, but isolate it so ignoring the dialog cannot
        # indefinitely block application shutdown or network restoration.
        command = ('import sys;sys.path.insert(0,sys.argv[1]);'
                   'from livewatch.wechat_runtime import certificate_removal_main;'
                   'sys.exit(certificate_removal_main(sys.argv[2]))')
        try:
            result = subprocess.run([sys.executable, '-B', '-c', command,
                str(Path(__file__).resolve().parents[1]), fingerprint],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise CertificateConfirmationPending(
                'Windows 证书移除确认已超时。请重试“关闭桥接并恢复网络”，并在 Windows 确认窗口点击“是”。') from None
        except OSError:
            raise RuntimeErrorSafe('无法启动视频号证书移除，请重试关闭桥接。') from None
        if result.returncode:
            raise CertificateConfirmationPending(
                'Windows 尚未确认移除本次视频号证书。请重试“关闭桥接并恢复网络”，并在 Windows 确认窗口点击“是”。')


class WechatRuntime:
    """One app-owned bridge. Call recover at app startup and stop at app exit.

    start(script_path) is idempotent while running; stopping the capture inbox
    alone should not stop this runtime if rooms still need live checks.
    """
    api_base = 'http://127.0.0.1:2022'

    def __init__(self, root, bundle_root=None, system=None):
        self.root = Path(root).resolve()
        self.bundle_root = Path(bundle_root).resolve() if bundle_root else Path(__file__).resolve().parents[2]
        self.executable = self.bundle_root / 'tools' / 'wechat-bridge' / 'wx_video_download.exe'
        self.workdir = self.root / 'data' / 'wechat-bridge'
        self.journal_path = self.workdir / 'recovery.json'
        self.config_path = self.workdir / 'config.yaml'
        self.system = system or WindowsIntegration()
        self.lock = threading.RLock()
        self.file_lock = None
        self.process = None
        self.job = None
        self.guard = None
        self.state = None
        self.running = False
        self.last_error = ''
        self._watchdog_stop = threading.Event()

    def snapshot(self):
        alive = self.running and self.process is not None and self.process.poll() is None
        return {'running': alive,
                'api_base': self.api_base, 'proxy_address': '127.0.0.1:2023',
                'recovery_pending': self.journal_path.exists() and not alive,
                'last_error': self.last_error}

    def _watch_process(self, process, stopped):
        while not stopped.wait(.25):
            if process.poll() is None:
                continue
            with self.lock:
                if stopped.is_set() or self.process is not process or not self.running:
                    return
                self.last_error = '视频号服务意外退出，已恢复本机网络设置，请重新启动捕获。'
                try:
                    self._cleanup()
                except Exception as exc:
                    self.last_error = ('视频号服务意外退出。' + str(exc) if isinstance(exc, RuntimeErrorSafe)
                                       else '视频号服务意外退出，部分设置尚未恢复，请重新启动软件恢复。')
                finally:
                    self._release()
            return

    def _acquire(self):
        self.workdir.mkdir(parents=True, exist_ok=True)
        if (self.workdir.is_symlink() or (hasattr(self.workdir, 'is_junction') and self.workdir.is_junction())
                or not self.workdir.resolve().is_relative_to(self.root)):
            raise RuntimeErrorSafe('视频号运行目录无效。')
        if self.file_lock is None:
            self.file_lock = self.system.lock(self.workdir / 'runtime.lock')

    def _safe_certs_directory(self):
        certs = self.workdir / 'certs'
        if certs.is_symlink() or (hasattr(certs, 'is_junction') and certs.is_junction()):
            raise RuntimeErrorSafe('视频号证书目录不能是链接或目录联接。')
        if not certs.resolve().is_relative_to(self.workdir.resolve()):
            raise RuntimeErrorSafe('视频号证书目录超出运行目录。')
        return certs

    def _release(self):
        if self.file_lock is not None:
            self.file_lock.close()
            self.file_lock = None

    def _clear_stale_pid_file(self, owned_record=None):
        """Remove only our bridge's stale marker while holding the workdir lock.

        Never signal a process based on this PID-only file. During cleanup only
        the recorded activation's PID is eligible; startup also accepts a dead
        PID or one now used by an unrelated executable.
        """
        if self.file_lock is None:
            raise RuntimeErrorSafe('视频号进程记录清理缺少目录锁。')
        path = self.workdir / 'wx_video_download.pid'
        try:
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise RuntimeErrorSafe('视频号进程记录不能是链接。')
            info = path.stat()
            if info.st_size > 32 or path.resolve().parent != self.workdir.resolve():
                raise RuntimeErrorSafe('视频号进程记录无效，请检查运行目录。')
            content = path.read_text('ascii')
        except FileNotFoundError:
            return
        except (OSError, UnicodeError):
            raise RuntimeErrorSafe('无法读取视频号进程记录，请重试捕获。') from None
        text = content.strip()
        if not re.fullmatch(r'[0-9]{1,10}', text) or not 0 < int(text) <= 0xffffffff:
            raise RuntimeErrorSafe('视频号进程记录无效，请检查运行目录。')
        pid = int(text)
        if owned_record is not None and pid != owned_record['pid']:
            return  # Another activation's marker is not ours to remove here.
        current = self.system.process_key(pid)
        if owned_record is not None:
            if current == owned_record:
                raise RuntimeErrorSafe('本次视频号服务尚未退出，已保留进程记录。')
        elif current and Path(current['image']).resolve() == self.executable.resolve():
            raise RuntimeErrorSafe('视频号进程记录对应的服务仍在运行，请先关闭该服务。')
        # A file can be replaced by the upstream process as it exits. Do not
        # erase a different marker even though this application holds its lock.
        try:
            latest = path.stat()
            if (latest.st_ino, latest.st_mtime_ns, latest.st_size) != (info.st_ino, info.st_mtime_ns, info.st_size):
                raise RuntimeErrorSafe('视频号进程记录已变化，请重试捕获。')
            if path.read_text('ascii') != content:
                raise RuntimeErrorSafe('视频号进程记录已变化，请重试捕获。')
            path.unlink()
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            raise RuntimeErrorSafe('无法清理视频号旧进程记录，请重试捕获。') from None

    def _save(self):
        temporary = self.journal_path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(self.state, handle, ensure_ascii=False)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, self.journal_path)

    def _load(self):
        if not self.journal_path.exists():
            return None
        try:
            if self.journal_path.stat().st_size > 65536:
                raise ValueError
            state = json.loads(self.journal_path.read_text(encoding='utf-8'))
            if not isinstance(state, dict) or state.get('version') != 1:
                raise ValueError
            backup = state.get('proxy_backup')
            if backup is not None:
                if not isinstance(backup, dict) or set(backup) != set(PROXY_KEYS):
                    raise ValueError
                for entry in backup.values():
                    if entry is not None and (not isinstance(entry, list) or len(entry) != 2
                            or entry[1] not in (1, 2, 4) or not isinstance(entry[0], (str, int))):
                        raise ValueError
            if not isinstance(state.get('proxy_keys', []), list) or any(key not in PROXY_KEYS for key in state.get('proxy_keys', [])):
                raise ValueError
            fingerprint = state.get('certificate', '')
            if fingerprint and not re.fullmatch(r'[A-F0-9]{40}', fingerprint):
                raise ValueError
            process = state.get('process')
            if process and (not isinstance(process, dict) or type(process.get('pid')) is not int
                    or process['pid'] <= 0 or not re.fullmatch(r'[0-9]{1,24}', str(process.get('created', '')))
                    or Path(process.get('image', '')).resolve() != self.executable.resolve()):
                raise ValueError
            return state
        except (ValueError, TypeError, OSError):
            raise RuntimeErrorSafe('视频号恢复记录无效，已保留记录，未修改 Windows 设置。') from None

    def _owns_api(self):
        record = (self.state or {}).get('process')
        return bool(record and self.system.process_key(record['pid']) == record
                    and self.system.listener_pid(2022) == record['pid'])

    def _api(self, path, data=None, timeout=5):
        if not self._owns_api():
            raise RuntimeErrorSafe('视频号服务端口不属于本次启动的进程。')
        from .providers import _DeadlineHTTPHandler, _PROBE_CONTEXT, _remaining_probe_time
        previous = getattr(_PROBE_CONTEXT, 'deadline', None)
        previous_watchdogs = getattr(_PROBE_CONTEXT, 'watchdogs', None)
        watchdogs = _PROBE_CONTEXT.watchdogs = []
        _PROBE_CONTEXT.deadline = time.monotonic() + timeout
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), _DeadlineHTTPHandler())
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.api_base + path, data=body,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with opener.open(request, timeout=_remaining_probe_time()) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                _remaining_probe_time()
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 0:
                raise ValueError
            return result.get('data', {})
        except Exception:
            raise RuntimeErrorSafe('视频号服务未能完成配置，请重试捕获。') from None
        finally:
            for timer in watchdogs:
                timer.cancel()
            _PROBE_CONTEXT.watchdogs = previous_watchdogs
            _PROBE_CONTEXT.deadline = previous

    def _spawn(self):
        return subprocess.Popen([str(self.executable), 'server', '--config', str(self.config_path),
            '--workdir', str(self.workdir)], cwd=str(self.workdir), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def _launch_guard(self):
        # This child is deliberately outside the bridge Job. It can restore
        # Windows settings after the main backend dies. Tree-wide forced kills
        # still require recover() on the next application launch.
        command = ('import sys;sys.path.insert(0,sys.argv[1]);'
                   'from livewatch.wechat_runtime import guardian_main;'
                   'guardian_main(*sys.argv[2:])')
        return subprocess.Popen([sys.executable, '-B', '-c', command,
            str(Path(__file__).resolve().parents[1]), str(self.root), str(self.bundle_root),
            self.state['guard_token']], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def _wait_api(self):
        until = time.monotonic() + 20
        while time.monotonic() < until:
            if self.process.poll() is not None:
                raise RuntimeErrorSafe('内置视频号服务启动失败。')
            owner = self.system.listener_pid(2022)
            if owner == self.process.pid:
                return
            if owner:
                raise RuntimeErrorSafe('视频号服务端口已被其他程序占用。')
            time.sleep(.1)
        raise RuntimeErrorSafe('内置视频号服务启动超时。')

    def _wait_proxy(self):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            owner = self.system.listener_pid(2023)
            if owner == self.process.pid:
                return
            if owner:
                raise RuntimeErrorSafe('视频号代理端口已被其他程序占用。')
            time.sleep(.1)
        raise RuntimeErrorSafe('视频号本机代理启动超时。')

    def _certificate(self):
        name = 'LiveWatch-' + secrets.token_hex(16)
        result = self._api('/api/proxy/certificate/generate',
            {'name': name, 'install': False, 'restart': False, 'valid_years': 2}, timeout=15)
        paths = result.get('config', {}).get('cert', {})
        cert = Path(paths.get('file', '')).resolve()
        key = Path(paths.get('key', '')).resolve()
        parent = self._safe_certs_directory().resolve()
        if cert.parent != parent or key.parent != parent or not cert.is_file() or not key.is_file():
            raise RuntimeErrorSafe('视频号服务未生成有效的本机证书。')
        # The parent ACL is restricted before generation, so newly written keys
        # are never briefly readable through a broadly inherited user ACL.
        self.system.secure_directory(parent)
        self.system.secure_directory(key)
        try:
            der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding='ascii'))
            fingerprint = hashlib.sha1(der).hexdigest().upper()
        except (ValueError, OSError, UnicodeError):
            raise RuntimeErrorSafe('视频号本机证书格式无效。') from None
        if self.system.certificate_present(fingerprint):
            raise RuntimeErrorSafe('视频号证书已存在，未接管已有证书。')
        self.state.update(certificate=fingerprint, certificate_owned=True)
        self._save()  # Intent first: a crash after certutil can still be undone.
        self.system.certificate_add(cert)
        if not self.system.certificate_present(fingerprint):
            raise RuntimeErrorSafe('当前用户的证书安装未生效。')
        return {'cert.file': str(cert), 'cert.key': str(key), 'cert.name': name}

    def _apply_proxy(self):
        self.state['proxy_backup'] = self.system.registry_read()
        self._save()
        for name, entry in PROXY_VALUES.items():
            self.state['proxy_keys'].append(name)
            self._save()  # Journal intent before each possibly partial write.
            self.system.registry_write(name, entry)
        self.system.notify_proxy()

    def _restore_proxy(self):
        backup = self.state.get('proxy_backup')
        names = self.state.get('proxy_keys', [])
        if backup is None or not names:
            return
        current = self.system.registry_read()
        if current.get('ProxyServer') == PROXY_VALUES['ProxyServer']:
            # A user can change another field while our proxy is active. Restore
            # only entries still carrying exactly the value we set.
            for name in reversed(names):
                if current.get(name) == PROXY_VALUES[name]:
                    self.system.registry_write(name, backup[name])
        # A previous recovery can restore all values and then fail to notify
        # WinINet. In that case ProxyServer no longer matches ours, but its
        # cached settings still need refreshing before the journal is cleared.
        # Refreshing also respects any values the user has changed since then.
        self.system.notify_proxy()
        self.state['proxy_keys'] = []
        self.state['proxy_backup'] = None
        self._save()

    def _cleanup(self, defer_certificate_confirmation=False):
        self._watchdog_stop.set()
        errors = []
        confirmation_pending = False
        try:
            self._restore_proxy()
        except Exception:
            errors.append('Windows 代理尚未恢复')
        # Even if registry writes fail, close our listener instead of letting a
        # half-configured service keep intercepting. The journal remains retryable.
        try:
            if self._owns_api():
                self._api('/api/service/stop', {'name': 'interceptor'}, timeout=3)
        except Exception:
            pass
        try:
            termination_error = None
            try:
                if self.process is not None and self.process.poll() is None:
                    self.process.terminate()
                    self.process.wait(timeout=5)
                elif self.state.get('process'):
                    self.system.terminate_record(self.state['process'])
            except Exception as exc:
                termination_error = exc
            finally:
                if self.job is not None:
                    self.job.close()
                    self.job = None
            if termination_error:
                if self.process is not None:
                    self.process.wait(timeout=3)
                elif self.state.get('process') and self.system.process_key(self.state['process']['pid']) == self.state['process']:
                    raise termination_error
            if self.state.get('process'):
                self._clear_stale_pid_file(self.state['process'])
            self.state['process'] = None
            self._save()
        except Exception:
            errors.append('视频号服务尚未退出')
        fingerprint = self.state.get('certificate')
        if fingerprint and self.state.get('certificate_owned'):
            try:
                self.state['certificate_confirmation_pending'] = False
                self._save()
                if self.system.certificate_present(fingerprint):
                    if defer_certificate_confirmation:
                        raise CertificateConfirmationPending(
                            '本次视频号证书仍待移除。请点击“关闭桥接并恢复网络”，并在 Windows 确认窗口点击“是”。')
                    self.system.certificate_remove(fingerprint)
                if self.system.certificate_present(fingerprint):
                    raise RuntimeErrorSafe('证书仍然存在')
                self.state['certificate_owned'] = False
                self._save()
            except Exception as exc:
                if isinstance(exc, CertificateConfirmationPending):
                    confirmation_pending = True
                    self.state['certificate_confirmation_pending'] = True
                    self._save()
                    errors.append(str(exc))
                else:
                    errors.append('本次视频号证书尚未移除')
        self.running = False
        if not errors:
            self.journal_path.unlink(missing_ok=True)
            self.state = None
            self.process = None
            if self.guard is not None:
                try: self.guard.wait(timeout=2)
                except subprocess.TimeoutExpired: pass
                self.guard = None
        if errors:
            error_type = CertificateConfirmationPending if confirmation_pending else RuntimeErrorSafe
            raise error_type('；'.join(errors) + ' 已保存恢复记录，可重试关闭桥接或重新启动软件恢复。')

    def recover(self, defer_certificate_confirmation=False):
        """Undo an interrupted activation. Refuse if another instance owns it."""
        with self.lock:
            if self.running:
                return self.snapshot()
            self._acquire()
            try:
                self.state = self._load()
                if self.state:
                    self._cleanup(defer_certificate_confirmation=defer_certificate_confirmation)
                return self.snapshot()
            finally:
                self._release()

    def start(self, script_path):
        with self.lock:
            if self.running and self.process and self.process.poll() is None:
                return self.snapshot()
            self._acquire()
            try:
                self.state = self._load()
                if self.state:
                    self._cleanup()
                if not self.executable.is_file():
                    raise RuntimeErrorSafe('安装包缺少内置视频号组件，请使用完整安装包。')
                script = Path(script_path).resolve()
                if not script.is_file():
                    raise RuntimeErrorSafe('视频号捕获脚本尚未准备好。')
                if self.system.listener_pid(2022) or self.system.listener_pid(2023):
                    raise RuntimeErrorSafe('视频号使用的 2022 或 2023 端口已被占用，请先关闭占用程序。')
                self._clear_stale_pid_file()
                certs = self._safe_certs_directory()
                self.system.secure_directory(self.workdir)
                certs.mkdir(exist_ok=True)
                self.system.secure_directory(certs)
                # JSON is valid YAML and avoids quoting Windows paths by hand.
                config = {'workdir': str(self.workdir),
                    'api': {'hostname': '127.0.0.1', 'port': 2022},
                    'proxy': {'enabled': False, 'system': False, 'tun': False, 'hostname': '127.0.0.1',
                              'port': 2023, 'skipInstallRootCert': True, 'tcpRelay': {'enabled': False}},
                    'inject': {'globalScript': str(script)}, 'log': {'enabled': False},
                    'debug': {'error': False}, 'mcp': {'enabled': False}, 'bridge': {'enabled': False},
                    'filehelper': {'enabled': False}}
                self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
                self.state = {'version': 1, 'process': None, 'owner': self.system.process_key(os.getpid()),
                              'guard_token': secrets.token_hex(16), 'certificate': '', 'certificate_owned': False,
                              'proxy_backup': None, 'proxy_keys': []}
                self._save()
                self.job = self.system.create_job()
                self.process = self._spawn()
                self.system.assign_job(self.job, self.process)
                self.state['process'] = self.system.process_key(self.process.pid)
                if (not self.state['process']
                        or Path(self.state['process']['image']).resolve() != self.executable.resolve()):
                    raise RuntimeErrorSafe('无法确认新启动的视频号服务身份。')
                self._save()
                self.guard = self._launch_guard()
                self._wait_api()
                values = self._certificate()
                values.update({'proxy.enabled': True, 'proxy.system': False, 'proxy.tun': False,
                               'proxy.skipInstallRootCert': True, 'proxy.hostname': '127.0.0.1', 'proxy.port': 2023})
                self._api('/api/proxy/config', {'values': values, 'restart': False})
                self._api('/api/service/start', {'name': 'interceptor'})
                self._wait_proxy()
                if self.guard is not None and self.guard.poll() is not None:
                    raise RuntimeErrorSafe('视频号网络恢复守护未能启动，已取消捕获。')
                self._apply_proxy()
                self.running = True
                self.last_error = ''
                self._watchdog_stop = threading.Event()
                threading.Thread(target=self._watch_process,
                    args=(self.process, self._watchdog_stop), daemon=True,
                    name='wechat-bridge-guard').start()
                return self.snapshot()
            except Exception as exc:
                safe = str(exc) if isinstance(exc, RuntimeErrorSafe) else '内置视频号捕获启动失败。'
                if self.state and not isinstance(exc, CertificateConfirmationPending):
                    try: self._cleanup()
                    except RuntimeErrorSafe as cleanup: safe += ' ' + str(cleanup)
                self._release()
                raise RuntimeErrorSafe(safe) from None

    def stop(self):
        """Restore Windows settings before stopping this owned bridge process."""
        with self.lock:
            self._acquire()
            try:
                if self.state is None:
                    self.state = self._load()
                if self.state:
                    self._cleanup()
                return self.snapshot()
            finally:
                self._release()


def certificate_removal_main(fingerprint):
    """Bounded by the parent; this helper changes only one CurrentUser root."""
    # Keep a bound even if the backend is killed while the Windows dialog is
    # open. WinDLL releases the GIL, so this timer can end the isolated helper.
    deadline = threading.Timer(30, lambda: os._exit(2))
    deadline.daemon = True
    deadline.start()
    try:
        WindowsIntegration._certificate_action(fingerprint, remove=True)
        return 1 if WindowsIntegration.certificate_present(fingerprint) else 0
    except Exception:
        return 1
    finally:
        deadline.cancel()


def guardian_main(root, bundle_root, token):
    """Hidden helper: recover only this activation after its exact owner exits."""
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        return
    runtime = WechatRuntime(root, bundle_root=bundle_root)
    failures = 0
    while True:
        try:
            state = runtime._load()
            if state is None or state.get('guard_token') != token:
                return
            if state.get('certificate_confirmation_pending'):
                return
            owner = state.get('owner')
            if not isinstance(owner, dict) or type(owner.get('pid')) is not int:
                return
            if runtime.system.process_key(owner['pid']) == owner:
                time.sleep(.25)
                continue
            runtime.recover()
            return
        except CertificateConfirmationPending:
            # Cancellation/timeout needs a user-initiated retry, not three
            # consecutive confirmation dialogs after the window has exited.
            return
        except Exception:
            # A dying owner may not have released its file lock yet. Retrying
            # also handles a transient certificate-store/registry failure.
            failures += 1
            if failures >= 3:
                return
            time.sleep(1)
