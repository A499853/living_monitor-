"""Smoke the current desktop in an isolated folder with local fixed runtimes.

Uses only previously downloaded runtimes under build/bootstrap-smoke/tools.
Does not copy user data, contact a live platform, or use system Python/WebView.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

sys.stdout.reconfigure(encoding='utf-8')

PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT / 'build' / 'bootstrap-smoke'
TARGET = PROJECT / 'build' / 'portable-desktop-smoke' / uuid.uuid4().hex
EXE = '\u76f4\u64ad\u5b88\u671b.exe'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
report = {'test_root': str(TARGET.relative_to(PROJECT))}
desktop = None
token = ''
origin = ''


def request(path, body=None):
    headers = {}
    if body is not None:
        headers = {'Content-Type': 'application/json', 'X-LiveWatch-Token': token}
        body = json.dumps(body).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(origin + path, data=body, headers=headers), timeout=4) as response:
        return response.read()


def wait_for(predicate, description, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(description)


def own_window():
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found = []
    @callback_type
    def visit(handle, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == desktop.pid:
            length = user.GetWindowTextLengthW(handle)
            value = ctypes.create_unicode_buffer(length + 1)
            user.GetWindowTextW(handle, value, len(value))
            if value.value.startswith('\u76f4\u64ad\u5b88\u671b 2.'):
                found.append(value.value)
        return True
    user.EnumWindows(visit, 0)
    return found[0] if found else ''


try:
    if not (FIXTURE / 'tools' / 'python' / 'python.exe').is_file():
        raise AssertionError('Previously downloaded bootstrap runtime fixture is unavailable')
    TARGET.mkdir(parents=True)
    for name in ('app',):
        shutil.copytree(PROJECT / name, TARGET / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('dependency-manifest.json', 'desktop-dependency-manifest.json'):
        shutil.copy2(PROJECT / name, TARGET / name)
    (TARGET / 'tools').mkdir()
    for name in ('python', 'webview2'):
        shutil.copytree(FIXTURE / 'tools' / name, TARGET / 'tools' / name)
    shutil.copy2(PROJECT / 'tools' / 'ffmpeg.exe', TARGET / 'tools' / 'ffmpeg.exe')
    build_environment = dict(os.environ)
    # A parent PowerShell 7 module path cannot be reused by Windows PowerShell 5.1.
    build_environment = {key: value for key, value in build_environment.items() if key.upper() != 'PSMODULEPATH'}
    build = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                            str(PROJECT / 'scripts' / 'build-desktop.ps1'), '-OutputDirectory', str(TARGET)],
                           env=build_environment, creationflags=NO_WINDOW, timeout=45, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert build.returncode == 0, (build.stdout + build.stderr).decode('utf-8', 'replace')
    environment = dict(os.environ)
    environment['PATH'] = str(Path(os.environ['WINDIR']) / 'System32')
    environment.pop('PYTHONHOME', None)
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([str(TARGET / EXE), '--prepare-dependencies'], cwd=TARGET.parent,
                            env=environment, creationflags=NO_WINDOW, timeout=60)
    assert result.returncode == 0, 'Dependency readiness check failed'
    report['fixed_runtime_receipts_verified'] = True
    report['system_python_or_browser_on_path'] = False
    result = subprocess.run([str(TARGET / 'tools' / 'python' / 'python.exe'), '-c',
                             'import ssl,sqlite3,urllib.request,ctypes,livewatch;print(livewatch.__version__)'],
                            cwd=TARGET.parent, env=environment, creationflags=NO_WINDOW,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    report['embedded_python_imports'] = 'passed'
    desktop = subprocess.Popen([str(TARGET / EXE)], cwd=TARGET.parent, env=environment,
                               creationflags=NO_WINDOW)
    info_path = TARGET / 'data' / 'instance.json'
    def instance():
        assert desktop.poll() is None, 'Desktop exited before backend became ready'
        try:
            value = json.loads(info_path.read_text('utf-8'))
            return value if value.get('port') else None
        except (OSError, ValueError):
            return None
    info = wait_for(instance, 'Isolated backend did not start')
    origin = 'http://127.0.0.1:' + str(info['port'])
    report['health'] = json.loads(request('/api/health'))
    assert report['health'].get('app') == 'LiveWatch'
    match = re.search(rb'<meta name="api-token" content="([^"]+)">', request('/'))
    assert match, 'Missing local API token'
    token = match[1].decode('ascii')
    report['desktop_title'] = wait_for(own_window, 'Native desktop window was not created')
    report['default_data_directory_beside_exe'] = info_path.is_file()
    state = json.loads(request('/api/state'))
    assert state.get('rooms') == [], 'Smoke data must be isolated from user rooms'
    report['isolated_empty_rooms'] = True
    # Windows module lookup confirms WebView was loaded from our own copy.
    command = "(Get-Process -Id " + str(desktop.pid) + ").Modules | Where-Object {$_.ModuleName -in @('WebView2Loader.dll','EmbeddedBrowserWebView.dll')} | Select-Object -ExpandProperty FileName | ConvertTo-Json -Compress"
    def loaded_browser():
        result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
                                creationflags=NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if result.returncode:
            return None
        raw = result.stdout.decode('utf-8-sig', 'replace').strip()
        try:
            values = json.loads(raw)
        except ValueError:
            return None
        values = [values] if isinstance(values, str) else values
        if len(values) >= 2 and all(str(TARGET).lower() in value.lower() for value in values):
            return [str(Path(value).relative_to(TARGET)) for value in values]
        return None
    report['local_webview_modules'] = wait_for(loaded_browser, 'Fixed WebView runtime was not loaded from EXE folder')
    request('/api/shutdown', {})
    desktop.wait(timeout=30)
    wait_for(lambda: not info_path.exists(), 'Backend instance marker was not removed')
    remaining_command = "@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -and $_.ExecutablePath.StartsWith('" + str(TARGET).replace("'", "''") + "', [StringComparison]::OrdinalIgnoreCase)}).Count"
    def processes_exited():
        remaining = subprocess.run(['powershell', '-NoProfile', '-Command', remaining_command],
                                   env=build_environment, creationflags=NO_WINDOW,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        return remaining.returncode == 0 and remaining.stdout.strip() == b'0'
    wait_for(processes_exited, 'Isolated runtime processes did not exit')
    report['shutdown'] = {'desktop_exit_code': desktop.returncode, 'instance_file_removed': True,
                          'isolated_runtime_processes_remaining': 0}
    report['result'] = 'passed'
finally:
    if desktop is not None and desktop.poll() is None:
        try:
            if token:
                request('/api/shutdown', {})
            desktop.wait(timeout=15)
        except Exception:
            desktop.terminate()
            desktop.wait(timeout=10)
    (PROJECT / 'artifacts').mkdir(exist_ok=True)
    (PROJECT / 'artifacts' / 'portable-desktop-current-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')

print(json.dumps(report, ensure_ascii=False, indent=2))
