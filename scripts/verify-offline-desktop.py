"""Launch an extracted delivery EXE with only Windows System32 on PATH.

This check never reads or operates native UI, starts the WeChat bridge, or
changes certificates/proxies. Inspect the visible window separately with the
approved computer-use tool while --hold-seconds keeps the app open.

Use a fresh extraction. Without --data-dir, mutable data is verified beside
the EXE; with it, the explicitly selected empty directory is verified instead.
Network access is not disabled: readiness, loaded runtime paths and an unchanged
runtime payload establish that this launch did not download dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
EXE = '直播守望.exe'


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def wait_for(predicate, description, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.2)
    raise AssertionError(description)


def isolated_environment():
    discarded = {'PATH', 'PYTHONHOME', 'PYTHONPATH', 'PSMODULEPATH',
                 'WEBVIEW2_BROWSER_EXECUTABLE_FOLDER', 'WEBVIEW2_USER_DATA_FOLDER',
                 'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', 'WEBVIEW2_RELEASE_CHANNEL_PREFERENCE'}
    environment = {key: value for key, value in os.environ.items() if key.upper() not in discarded}
    environment['PATH'] = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32')
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    return environment


def powershell(script, environment, cwd, timeout=20):
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    prefix = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-Command', prefix + script],
                            cwd=cwd, env=environment, capture_output=True, timeout=timeout,
                            creationflags=NO_WINDOW)
    check(result.returncode == 0, 'Windows process/readiness check failed: ' +
          result.stderr.decode('utf-8-sig', 'replace')[:1200])
    return result.stdout.decode('utf-8-sig').strip()


def package_processes(environment, cwd):
    # Process metadata only: no window handles, user32, screenshots or UI text.
    script = ("$prefix=$env:LIVEWATCH_VERIFY_PACKAGE.TrimEnd('\\')+'\\'; "
              "$items=@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
              "$_.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) } | "
              "Select-Object ProcessId,ParentProcessId,ExecutablePath); "
              "ConvertTo-Json -InputObject $items -Compress")
    return json.loads(powershell(script, environment, cwd))


def runtime_snapshot(package):
    return {str(path.relative_to(package)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in (package / 'tools').rglob('*') if path.is_file()}


def verify(args, report):
    check(os.name == 'nt', 'This desktop check requires Windows')
    package = args.package_dir.resolve()
    data = args.data_dir.resolve() if args.data_dir else package
    executable = package / EXE
    check(executable.is_file(), 'The selected directory does not contain the delivery EXE')
    check((package / 'package-manifest.json').is_file(), 'Select an extracted delivery directory with package-manifest.json')
    if args.data_dir:
        check(data == package or not data.is_relative_to(package),
              'An explicit data directory must be outside the package or equal its root')
        check(data == package or not data.exists() or not any(data.iterdir()),
              'The explicit data directory must be empty')
    for root in {package, data}:
        check(not any((root / name).exists() for name in ('data', 'logs', 'video', 'time_begin_over')),
              'Use a fresh extraction and an unused data directory; existing user data is never reused')
    check(not (package / 'tools/.downloads').exists(), 'Unexpected dependency download cache before launch')
    report.update(package_dir=str(package), data_dir=str(data),
                  data_location='explicit independent directory' if data != package else 'beside EXE (default)',
                  ui_validation='Not performed by this script; inspect the window separately',
                  network_disabled=False,
                  system_requirement='Windows 10 22H2 / Windows 11 x64 with .NET Framework 4.8')
    environment = isolated_environment()
    environment['LIVEWATCH_VERIFY_PACKAGE'] = str(package)
    environment['LIVEWATCH_VERIFY_EXE'] = str(executable)
    check(not package_processes(environment, package.parent), 'Another process is already using this extraction')
    readiness = ("$a=[Reflection.Assembly]::LoadFrom($env:LIVEWATCH_VERIFY_EXE); "
                 "$t=$a.GetType('DependencyBootstrapper',$true); "
                 "$m=$t.GetMethod('NeedsPreparation',[Reflection.BindingFlags]'Static,NonPublic'); "
                 "if ($m.Invoke($null,@($env:LIVEWATCH_VERIFY_PACKAGE))) {throw 'Dependencies require preparation'}; "
                 "Write-Output 'READY'")
    check(powershell(readiness, environment, package.parent) == 'READY', 'Local dependencies are not ready')
    report['actual_launcher_needs_dependency_preparation'] = False
    report['path_contains_only_system32'] = environment['PATH']
    report['external_python_or_browser_required'] = False
    python = subprocess.run([str(package / 'tools/python/python.exe'), '-B', '-c',
        'import json,ssl,sqlite3,urllib.request,sys,livewatch;'
        'print(json.dumps({"executable":sys.executable,"version":livewatch.__version__}))'],
        cwd=package.parent, env=environment, capture_output=True, timeout=15, creationflags=NO_WINDOW)
    check(python.returncode == 0, 'Packaged Python cannot import the application and standard modules')
    report['embedded_python'] = json.loads(python.stdout)
    ffmpeg = subprocess.run([str(package / 'tools/ffmpeg.exe'), '-version'], cwd=package.parent,
        env=environment, capture_output=True, timeout=15, creationflags=NO_WINDOW)
    check(ffmpeg.returncode == 0 and b'ffmpeg version' in ffmpeg.stdout, 'Packaged FFmpeg cannot run')
    report['embedded_ffmpeg_started'] = True
    before = runtime_snapshot(package)
    desktop = None
    origin = token = ''
    info_path = data / 'data/instance.json'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None):
        headers = {}
        if body is not None:
            headers = {'Content-Type': 'application/json', 'X-LiveWatch-Token': token}
            body = json.dumps(body).encode('utf-8')
        with opener.open(urllib.request.Request(origin + path, data=body, headers=headers), timeout=5) as response:
            return response.read()

    try:
        command = [str(executable)]
        if args.data_dir:
            command.extend(['--data-dir', str(data)])
        desktop = subprocess.Popen(command, cwd=package.parent, env=environment, creationflags=NO_WINDOW)
        report['desktop_pid'] = desktop.pid

        def instance():
            check(desktop.poll() is None, 'Desktop exited before backend startup')
            try:
                value = json.loads(info_path.read_text('utf-8'))
                return value if type(value.get('port')) is int and 0 < value['port'] <= 65535 else None
            except (OSError, ValueError):
                return None

        info = wait_for(instance, 'The delivery backend did not start', seconds=45)
        origin = 'http://127.0.0.1:' + str(info['port'])
        report['health'] = health = json.loads(request('/api/health'))
        check(health.get('app') == 'LiveWatch', 'Unexpected backend health response')
        match = re.search(rb'<meta name="api-token" content="([^"]+)">', request('/'))
        check(match is not None, 'Missing local API token')
        token = match[1].decode('ascii')
        state = json.loads(request('/api/state'))
        check(state.get('rooms') == [], 'The test must start with an empty room list')
        check(state.get('ffmpeg_available') is True, 'The application cannot find its packaged FFmpeg')
        report.update(isolated_empty_rooms=True, application_ffmpeg_available=True,
                      instance_created_in_expected_data_directory=True)
        check(all((data / name).is_dir() for name in ('data', 'logs', 'video', 'time_begin_over')),
              'Application output directories were not created in the selected data location')
        if data != package:
            check(not (package / 'data').exists(), 'Independent data mode unexpectedly wrote data beside EXE')
        environment['LIVEWATCH_VERIFY_PID'] = str(desktop.pid)

        def browser_modules():
            script = ("$items=@((Get-Process -Id ([int]$env:LIVEWATCH_VERIFY_PID)).Modules | "
                      "Where-Object {$_.ModuleName -in @('WebView2Loader.dll','EmbeddedBrowserWebView.dll')} | "
                      "Select-Object -ExpandProperty FileName); ConvertTo-Json -InputObject $items -Compress")
            values = json.loads(powershell(script, environment, package.parent))
            expected = {str(package / 'WebView2Loader.dll').casefold(),
                        str(package / 'tools/webview2/EBWebView/x64/EmbeddedBrowserWebView.dll').casefold()}
            return values if {str(Path(value)).casefold() for value in values} == expected else None

        modules = wait_for(browser_modules, 'The EXE did not load the packaged WebView2 runtime', seconds=30)
        report['loaded_webview_modules'] = [str(Path(value).relative_to(package)) for value in modules]
        processes = package_processes(environment, package.parent)
        backend = next((p for p in processes if p['ProcessId'] == info.get('pid')), None)
        check(backend and Path(backend['ExecutablePath']) == package / 'tools/python/pythonw.exe',
              'The desktop did not launch the packaged Python backend')
        check(backend['ParentProcessId'] == desktop.pid, 'The backend was not started by this desktop')
        report['backend_uses_packaged_pythonw'] = True
        report['status'] = 'ready_for_visual_check'
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        print(json.dumps({'status': report['status'], 'desktop_pid': desktop.pid,
                          'hold_seconds': args.hold_seconds, 'report': str(args.report.resolve())}), flush=True)
        deadline = time.monotonic() + args.hold_seconds
        while time.monotonic() < deadline:
            check(desktop.poll() is None, 'Desktop exited during visual-check interval')
            time.sleep(min(.25, max(0, deadline - time.monotonic())))
        request('/api/shutdown', {})
        desktop.wait(timeout=45)
        check(desktop.returncode == 0, 'Desktop returned a nonzero exit code')
        wait_for(lambda: not info_path.exists(), 'Backend instance marker was not removed')
        wait_for(lambda: not package_processes(environment, package.parent),
                 'Packaged runtime processes remained after shutdown')
        check(not (package / 'tools/.downloads').exists(), 'Launch created a dependency download cache')
        check(not (package / 'logs/bootstrap.log').exists(), 'Launch entered dependency preparation')
        check(runtime_snapshot(package) == before, 'Launch modified or downloaded runtime payload files')
        report.update(status='passed', dependency_download_cache_created=False,
                      runtime_payload_unchanged=True, clean_shutdown=True,
                      desktop_exit_code=desktop.returncode, packaged_processes_remaining=0)
    finally:
        if desktop is not None and desktop.poll() is None:
            try:
                if token:
                    request('/api/shutdown', {})
                desktop.wait(timeout=15)
            except Exception:
                desktop.terminate()
                desktop.wait(timeout=10)
                report['failure_cleanup_required_desktop_termination'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir', required=True, type=Path)
    parser.add_argument('--data-dir', type=Path, help='Unused empty directory; omit to test default writes beside EXE')
    parser.add_argument('--hold-seconds', type=int, default=0, help='Keep ready window open for separate visual inspection (0-300)')
    parser.add_argument('--report', type=Path, default=PROJECT / 'artifacts/offline-desktop-report.json')
    args = parser.parse_args()
    if not 0 <= args.hold_seconds <= 300:
        parser.error('--hold-seconds must be between 0 and 300')
    report = {'status': 'running'}
    try:
        verify(args, report)
    except Exception as exc:
        report.update(status='failed', error=str(exc), error_type=type(exc).__name__)
        return 1
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    if sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
