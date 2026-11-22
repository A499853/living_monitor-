"""Verify the actual delivery archive, without using user settings or accounts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import struct
import subprocess
import sys
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parents[1]


def verify(archive):
    archive = Path(archive).resolve()
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    recorded = Path(str(archive)+'.sha256').read_text('ascii').split()[0]
    assert digest == recorded, 'archive SHA-256 differs from published digest'
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        assert len(names) == len(set(names)), 'duplicate archive entries'
        roots = {PurePosixPath(name).parts[0] for name in names}
        assert len(roots) == 1, 'archive must have one application directory'
        root = roots.pop()
        for name in names:
            path = PurePosixPath(name)
            assert not path.is_absolute() and '..' not in path.parts and '\\' not in name and ':' not in name, name
        contents = {name[len(root)+1:] for name in names}
        required = {'直播守望.exe', '直播守望.exe.config', 'app/run.py',
                    'app/livewatch/engine.py', 'app/livewatch/web/app.js',
                    'app/livewatch/web/style.css', 'tools/ffmpeg.exe',
                    'dependency-manifest.json', 'desktop-dependency-manifest.json',
                    'Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll',
                    'WebView2Loader.dll', 'package-manifest.json',
                    '检测超时恢复与交付.md', '异常恢复与媒体设置.md'}
        assert required <= contents, f'missing package files: {required-contents}'
        forbidden = ('data/', 'logs/', 'video/', 'time_begin_over/', 'tools/python/', 'tools/webview2/', 'tools/.downloads/')
        for name in contents:
            assert not name.startswith(forbidden), name
            assert '__pycache__' not in name and Path(name).suffix.lower() not in ('.db', '.kd', '.pyc', '.log', '.ts', '.mp4'), name
        inventory = json.loads(package.read(root+'/package-manifest.json'))
        assert {item['path'] for item in inventory} == contents-{'package-manifest.json'}, 'inventory coverage mismatch'
        for item in inventory:
            with package.open(root+'/'+item['path']) as member:
                actual = hashlib.file_digest(member, 'sha256').hexdigest()
            assert item['sha256'] == actual, item['path']
            assert item['size'] == package.getinfo(root+'/'+item['path']).file_size, item['path']
            relative=item['path']
            current=PROJECT/'build'/'desktop'/relative if relative.endswith(('.exe','.exe.config','.dll')) and '/' not in relative else PROJECT/relative
            if current.is_file():
                with current.open('rb') as source:
                    assert hashlib.file_digest(source,'sha256').hexdigest()==actual, 'archive is stale: '+relative
        executable = package.read(root+'/直播守望.exe')
        pe_offset = struct.unpack_from('<I', executable, 0x3c)[0]
        assert executable[:2] == b'MZ' and executable[pe_offset:pe_offset+4] == b'PE\0\0', 'invalid EXE'
        assert struct.unpack_from('<H', executable, pe_offset+24+68)[0] == 2, 'EXE must use Windows GUI subsystem'
        for filename, keys in [('dependency-manifest.json', ['python']), ('desktop-dependency-manifest.json', ['webview2_runtime'])]:
            manifest = json.loads(package.read(root+'/'+filename))
            for key in keys:
                dependency = manifest[key]
                assert dependency['url'].startswith('https://') and len(dependency['sha256']) == 64 and dependency['size'] > 0
        with tempfile.TemporaryDirectory(prefix='livewatch-archive-') as temporary:
            package.extractall(temporary)
            extracted = Path(temporary)/root
            command = subprocess.run([sys.executable, '-B', str(extracted/'app'/'run.py'), '--version'],
                                     cwd=temporary, capture_output=True, timeout=15,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            assert command.returncode == 0, command.stderr.decode('utf-8', 'replace')
            assert command.stdout.strip() == b'2.8.0', command.stdout
    return dict(package=str(archive),size_bytes=archive.stat().st_size,sha256=digest,
                files=len(names),inventory_verified=True,current_sources_verified=True,private_data_excluded=True,
                python_webview_download_on_first_launch=True,windows_gui_executable=True,
                extracted_backend_version='2.8.0')


if __name__ == '__main__':
    result = verify(sys.argv[1] if len(sys.argv)>1 else PROJECT/'artifacts'/'LiveWatch-Optimized-Windows-x64.zip')
    report = PROJECT/'artifacts'/'delivery-package-report.json'
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
