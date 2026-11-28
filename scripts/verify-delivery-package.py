"""Verify complete offline delivery files without using user accounts or networks.

Accepts either the published ZIP (+ .sha256) or --stage DIRECTORY before ZIP
creation. Runtime payloads are restricted to the restored vendor allowlist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME_PREFIXES = ('tools/python/', 'tools/webview2/')
BRIDGE_FILES = {'wx_video_download.exe', 'LICENSE', 'VERSION.txt', 'download-manifest.json'}
REQUIRED = {'直播守望.exe', '直播守望.exe.config', 'app/run.py',
            'app/livewatch/engine.py', 'app/livewatch/web/app.js', 'app/livewatch/web/style.css',
            'app/livewatch/wechat.py', 'app/livewatch/wechat_capture.py', 'app/livewatch/wechat_capture.js',
            'app/livewatch/wechat_runtime.py',
            'tools/ffmpeg.exe', 'tools/python/python.exe', 'tools/python/pythonw.exe',
            'tools/python/python312.dll', 'tools/python/python312.zip', 'tools/python/python312._pth',
            'tools/python/_ssl.pyd', 'tools/python/_sqlite3.pyd',
            'tools/webview2/msedgewebview2.exe', 'tools/webview2/msedge.dll',
            'tools/webview2/icudtl.dat', 'tools/webview2/resources.pak',
            'tools/webview2/EBWebView/x64/EmbeddedBrowserWebView.dll',
            'dependency-manifest.json', 'desktop-dependency-manifest.json',
            'Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll',
            'WebView2Loader.dll', 'package-manifest.json', 'runtime-source-manifest.json',
            'portable-delivery.md', '先读我.txt', '使用说明.md',
            '检测超时恢复与交付.md', '异常恢复与媒体设置.md'}
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def safe_relative(name):
    path = PurePosixPath(name)
    assert name and not path.is_absolute() and '..' not in path.parts, name
    assert '\\' not in name and ':' not in name and '\0' not in name, name
    assert all(part not in ('', '.') for part in name.split('/')), name
    return path


def load_json(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def verify_files(directory, require_wechat_bridge=False):
    directory = Path(directory).resolve()
    all_files = list(directory.rglob('*'))
    assert not any(p.is_symlink() for p in all_files), 'package may not contain symlinks'
    files = {p.relative_to(directory).as_posix(): p for p in all_files if p.is_file()}
    contents = set(files)
    assert REQUIRED <= contents, f'missing package files: {REQUIRED-contents}'
    for name in contents:
        path = safe_relative(name)
        assert path.parts[0] not in ('data', 'logs', 'video', 'time_begin_over', 'build', 'artifacts'), name
        assert not any(part.lower() in {'__pycache__', '.downloads', 'certs', 'user data',
                                       'cache', 'code cache', 'gpucache', 'ebwebview', 'sessions'}
                       for part in path.parts[:-1] if not (name.startswith('tools/webview2/EBWebView/'))), name
        assert path.suffix.lower() not in ('.db', '.sqlite', '.sqlite3', '.kd', '.pyc', '.pyo',
                                          '.log', '.ts', '.mp4', '.pem', '.key', '.pfx'), name
        assert path.name.lower() not in ('cookies', 'history', 'login data', 'local state', 'preferences'), name
    inventory = load_json(directory/'package-manifest.json')
    assert isinstance(inventory, list) and inventory, 'empty package inventory'
    assert len(inventory) == len({item['path'] for item in inventory}), 'duplicate inventory paths'
    assert {item['path'] for item in inventory} == contents-{'package-manifest.json'}, 'inventory coverage mismatch'
    actual_hashes = {}
    for item in inventory:
        relative = item['path']
        safe_relative(relative)
        actual_hashes[relative] = digest(files[relative])
        assert item['sha256'] == actual_hashes[relative], 'hash mismatch: '+relative
        assert item['size'] == files[relative].stat().st_size, 'size mismatch: '+relative
        current = PROJECT/'build'/'desktop'/relative if relative.endswith(('.exe', '.exe.config', '.dll')) and '/' not in relative else PROJECT/relative
        if current.is_file():
            assert digest(current) == actual_hashes[relative], 'package is stale: '+relative
    # This exact vendor payload allowlist excludes all runtime side effects and
    # prevents an accidentally copied browser profile from entering the ZIP.
    restored = load_json(PROJECT/'restoration-manifest.json')
    ffmpeg_source = next(item for item in restored['files'] if item['path'] == 'tools/ffmpeg.exe')
    assert actual_hashes['tools/ffmpeg.exe'] == ffmpeg_source['sha256'], 'vendor FFmpeg changed'
    assert files['tools/ffmpeg.exe'].stat().st_size == ffmpeg_source['size'], 'vendor FFmpeg size changed'
    expected_runtime = {item['path']: item for item in restored['files'] if item['path'].startswith(RUNTIME_PREFIXES)}
    runtime_source = load_json(directory/'runtime-source-manifest.json')
    assert isinstance(runtime_source, list) and len(runtime_source) == len(expected_runtime)
    assert {item['path']: item for item in runtime_source} == expected_runtime, 'runtime provenance differs from restored vendor files'
    generated = {prefix+name for prefix in RUNTIME_PREFIXES for name in ('.livewatch-runtime.json', '.livewatch-package-sha256')}
    assert {name for name in contents if name.startswith(RUNTIME_PREFIXES)} == set(expected_runtime)|generated, 'unexpected or missing runtime payload'
    for name, item in expected_runtime.items():
        assert actual_hashes[name] == item['sha256'] and files[name].stat().st_size == item['size'], 'vendor runtime changed: '+name
    for runtime, manifest_file, key in [('python', 'dependency-manifest.json', 'python'),
                                         ('webview2', 'desktop-dependency-manifest.json', 'webview2_runtime')]:
        prefix = 'tools/'+runtime+'/'
        dependency = load_json(directory/manifest_file)[key]
        assert dependency['url'].startswith('https://') and re.fullmatch('[a-f0-9]{64}', dependency['sha256'])
        assert files[prefix+'.livewatch-package-sha256'].read_text('ascii').strip() == dependency['sha256']
        receipt = load_json(files[prefix+'.livewatch-runtime.json'])
        assert isinstance(receipt, dict) and receipt, 'invalid bootstrap receipt'
        receipt_files = {}
        for name, size in receipt.items():
            normalized = name.replace('\\', '/')
            safe_relative(normalized)
            assert normalized not in receipt_files, 'duplicate normalized receipt path'
            assert type(size) is int and size > 0, 'invalid receipt size'
            receipt_files[normalized] = size
        expected = {name[len(prefix):]: p.stat().st_size for name, p in files.items()
                    if name.startswith(prefix) and name != prefix+'.livewatch-runtime.json'}
        assert receipt_files == expected, 'bootstrap receipt coverage or size mismatch: '+runtime
    pth = files['tools/python/python312._pth'].read_text('ascii').splitlines()
    assert pth == ['python312.zip', '.', '../../app'], 'embedded Python must be isolated and use only package app/'
    bridge = {name for name in contents if name.startswith('tools/wechat-bridge/')}
    assert bridge <= {'tools/wechat-bridge/'+name for name in BRIDGE_FILES}, 'bridge contains machine-specific config or extra files'
    if require_wechat_bridge or bridge:
        assert {'tools/wechat-bridge/wx_video_download.exe', 'tools/wechat-bridge/LICENSE',
                'tools/wechat-bridge/download-manifest.json'} <= bridge
        assert files['tools/wechat-bridge/wx_video_download.exe'].stat().st_size > 1000000
        assert files['tools/wechat-bridge/LICENSE'].stat().st_size > 0
        bridge_source = load_json(files['tools/wechat-bridge/download-manifest.json'])
        assert bridge_source['url'].startswith('https://github.com/ltaoo/wx_channels_download/releases/download/')
        assert re.fullmatch('[a-f0-9]{64}', bridge_source['sha256']), 'invalid bridge archive hash'
        assert bridge_source['publisher_checksum_verified'] is True, 'bridge release checksum was not verified'
        bridge_inputs = bridge_source['files']
        assert len(bridge_inputs) == 2 and {item['path'] for item in bridge_inputs} == {'wx_video_download.exe', 'LICENSE'}
        for item in bridge_inputs:
            name = 'tools/wechat-bridge/'+item['path']
            assert actual_hashes[name] == item['sha256'], 'bridge release file changed: '+name
            assert files[name].stat().st_size == item['size'], 'bridge release file size changed: '+name
    executable = files['直播守望.exe'].read_bytes()
    pe_offset = struct.unpack_from('<I', executable, 0x3c)[0]
    assert executable[:2] == b'MZ' and executable[pe_offset:pe_offset+4] == b'PE\0\0', 'invalid EXE'
    assert struct.unpack_from('<H', executable, pe_offset+24+68)[0] == 2, 'EXE must use Windows GUI subsystem'
    environment = {key: value for key, value in os.environ.items() if key.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'PSMODULEPATH')}
    environment['PATH'] = str(Path(os.environ.get('WINDIR', r'C:\Windows'))/'System32')
    python = files['tools/python/python.exe']
    command = subprocess.run([str(python), '-B', '-c',
                              'import ssl,sqlite3,urllib.request,ctypes,livewatch;print(livewatch.__version__)'],
                             cwd=directory.parent, env=environment, capture_output=True, timeout=15, creationflags=NO_WINDOW)
    assert command.returncode == 0, command.stderr.decode('utf-8', 'replace')
    assert command.stdout.strip() == b'2.8.0', command.stdout
    ffmpeg = subprocess.run([str(files['tools/ffmpeg.exe']), '-version'], cwd=directory.parent,
                            env=environment, capture_output=True, timeout=15, creationflags=NO_WINDOW)
    assert ffmpeg.returncode == 0 and b'ffmpeg version' in ffmpeg.stdout, 'bundled FFmpeg did not start'
    # Read the actual launcher's decision without invoking Main, creating user
    # data, or executing a download. NeedsPreparation=False skips its download UI.
    environment['LIVEWATCH_VERIFY_EXE'] = str(files['直播守望.exe'])
    command_text = "$ErrorActionPreference='Stop'; $a=[Reflection.Assembly]::LoadFrom($env:LIVEWATCH_VERIFY_EXE); $t=$a.GetType('DependencyBootstrapper',$true); $m=$t.GetMethod('NeedsPreparation',[Reflection.BindingFlags]'Static,NonPublic'); if ($m.Invoke($null,@([IO.Path]::GetDirectoryName($env:LIVEWATCH_VERIFY_EXE)))) { throw 'Offline dependencies are not ready' }; Write-Output 'READY'"
    powershell = Path(os.environ.get('WINDIR', r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    readiness = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command_text],
                               cwd=directory.parent, env=environment, capture_output=True, timeout=30, creationflags=NO_WINDOW)
    assert readiness.returncode == 0 and readiness.stdout.strip() == b'READY', (readiness.stdout+readiness.stderr).decode('utf-8', 'replace')
    return dict(files=len(files), inventory_verified=True, current_sources_verified=True, private_data_excluded=True,
                vendor_runtime_files=len(expected_runtime), runtime_receipts_verified=True,
                python_webview_download_on_first_launch=False, desktop_bootstrap_ready=True,
                windows_gui_executable=True, embedded_python_version='2.8.0', embedded_ffmpeg_started=True,
                wechat_bridge_bundled=bool(bridge), wechat_bridge_source_verified=bool(bridge))


def verify(archive, require_wechat_bridge=False):
    archive = Path(archive).resolve()
    archive_hash = digest(archive)
    recorded = Path(str(archive)+'.sha256').read_text('ascii').split()[0]
    assert archive_hash == recorded, 'archive SHA-256 differs from published digest'
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        assert names and len(names) == len(set(names)), 'duplicate or empty archive entries'
        for name in names:
            safe_relative(name)
        roots = {PurePosixPath(name).parts[0] for name in names}
        assert len(roots) == 1, 'archive must have one application directory'
        root = roots.pop()
        with tempfile.TemporaryDirectory(prefix='livewatch-archive-') as temporary:
            package.extractall(temporary)
            result = verify_files(Path(temporary)/root, require_wechat_bridge)
    return dict(package=str(archive), size_bytes=archive.stat().st_size, sha256=archive_hash, **result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', nargs='?', type=Path)
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--require-wechat-bridge', action='store_true')
    args = parser.parse_args()
    if args.stage:
        assert args.archive is None, 'choose --stage or archive, not both'
        result = dict(stage=str(args.stage.resolve()), **verify_files(args.stage, args.require_wechat_bridge))
        report = PROJECT/'artifacts'/'delivery-stage-report.json'
    else:
        result = verify(args.archive or PROJECT/'artifacts'/'LiveWatch-Optimized-Windows-x64.zip', args.require_wechat_bridge)
        report = PROJECT/'artifacts'/'delivery-package-report.json'
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', 'utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
