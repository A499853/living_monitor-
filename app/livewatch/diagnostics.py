"""Numeric resource observations for this process only; no user data is read."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('PageFaultCount', wintypes.DWORD),
        ('PeakWorkingSetSize', ctypes.c_size_t),
        ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t),
        ('PeakPagefileUsage', ctypes.c_size_t),
        ('PrivateUsage', ctypes.c_size_t),
    ]


def _windows_health():
    result = {'working_set_bytes': None, 'private_bytes': None, 'handle_count': None}
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        current_process = kernel.GetCurrentProcess
        current_process.argtypes = []
        current_process.restype = wintypes.HANDLE
        process = current_process()
    except (AttributeError, OSError, TypeError, ValueError):
        return result
    try:
        memory_info = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
        memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD]
        memory_info.restype = wintypes.BOOL
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if memory_info(process, ctypes.byref(counters), counters.cb):
            result['working_set_bytes'] = int(counters.WorkingSetSize)
            result['private_bytes'] = int(counters.PrivateUsage)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        handle_count = kernel.GetProcessHandleCount
        handle_count.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle_count.restype = wintypes.BOOL
        count = wintypes.DWORD()
        if handle_count(process, ctypes.byref(count)):
            result['handle_count'] = int(count.value)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return result


def system_health():
    """Return Python thread count and optional Windows process counters.

    Byte counts are current process working set and committed private memory.
    Unavailable native counters remain None. This function neither enumerates
    other processes nor reads paths, environment variables or user profiles.
    """
    result = {'thread_count': threading.active_count(), 'working_set_bytes': None,
              'private_bytes': None, 'handle_count': None}
    if os.name == 'nt':
        result.update(_windows_health())
    return result
