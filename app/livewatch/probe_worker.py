"""Private persistent probe worker. No application database or logging setup."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import threading
import time

# Embedded Python ignores PYTHONPATH; support direct absolute-path launching.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from livewatch.probe_pool import MAX_MESSAGE_BYTES, _encode_message
from livewatch.providers import ProviderRegistry


def _watch_owner(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            os._exit(0)
        try:
            while kernel.WaitForSingleObject(handle, 1000) == 258:
                pass
        finally:
            kernel.CloseHandle(handle)
        os._exit(0)
    else:
        while os.getppid() == pid:
            time.sleep(1)
        os._exit(0)


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--owner-pid', required=True, type=int)
    args = parser.parse_args(argv)
    if args.owner_pid <= 0:
        return 1
    threading.Thread(target=_watch_owner, args=(args.owner_pid,),
                     name='probe-owner', daemon=True).start()
    provider = ProviderRegistry()
    source, target = sys.stdin.buffer, sys.stdout.buffer
    while True:
        line = source.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return 0
        if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b'\n'):
            return 1
        try:
            request = json.loads(line)
            if (not isinstance(request, dict) or request.get('version') != 1
                    or type(request.get('id')) is not int
                    or not isinstance(request.get('room'), dict)
                    or not isinstance(request.get('settings'), dict)):
                return 1
            result = provider.check(request['room'], request['settings'])
            response = dict(version=1, id=request['id'], ok=True, result=asdict(result))
            encoded = _encode_message(response)
        except Exception:
            # Exceptions, URLs and credentials never go to stderr or back
            # through diagnostic strings. The parent reports a safe failure.
            try:
                encoded = _encode_message(dict(version=1, id=request.get('id'), ok=False))
            except Exception:
                return 1
        try:
            target.write(encoded)
            target.flush()
        except OSError:
            return 0


if __name__ == '__main__':
    raise SystemExit(main())
