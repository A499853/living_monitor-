"""Conservative retention of explicitly registered application output files."""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time


class Maintenance:
    def __init__(self,store):
        self.store=store
        self.root=Path(store.root)
        with store.lock:
            if not hasattr(store,'_maintenance_lock'): store._maintenance_lock=threading.RLock()
            self.lock=store._maintenance_lock
        self._cleanup_cursor=''

    def _path(self,value,kind):
        path=Path(value)
        if not path.is_absolute(): path=self.root/path
        path=Path(os.path.abspath(path))
        folder=self.root/('video' if kind=='recording' else 'logs')
        try: relative=path.relative_to(folder)
        except ValueError: raise ValueError('维护文件路径超出本应用输出目录') from None
        if not relative.parts or ':' in str(relative) or '..' in relative.parts:
            raise ValueError('维护文件路径无效')
        # Never traverse symbolic links, Windows junctions, or other reparse
        # points, even when they resolve back inside the application directory.
        current=self.root
        for part in (folder.name,*relative.parts):
            current=current/part
            try: info=current.lstat()
            except FileNotFoundError: continue
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
                raise ValueError('维护文件路径包含符号链接或重解析点')
        if path.resolve(strict=False)!=path: raise ValueError('维护文件路径发生跳转')
        if kind=='log' and not re.fullmatch(r'(?:app\.log(?:\.[1-9][0-9]*)?|record_[a-f0-9]{8}_[0-9_]+\.log)',path.name):
            raise ValueError('仅登记本应用生成的日志')
        return path

    @staticmethod
    def _identity(path):
        try: info=path.stat(follow_symlinks=False)
        except FileNotFoundError: return (None,None,None,None)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise ValueError('维护对象必须为普通单链接文件')
        return str(info.st_dev),str(info.st_ino),info.st_size,str(info.st_mtime_ns)

    def _register(self,path,kind,active,process_id):
        if type(active) is not bool or process_id is not None and (type(process_id) is not int or process_id<=0):
            raise ValueError('文件登记状态无效')
        with self.lock:
            path=self._path(path,kind)
            identity=self._identity(path)
            stamp=time.time()
            with self.store.lock,self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO owned_files VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (str(path.relative_to(self.root)),kind,int(active),stamp,None if active else stamp,process_id,*identity))
            return str(path)

    def register_recording(self,path,active=True,process_id=None):
        return self._register(path,'recording',active,process_id)

    def register_log(self,path,active=True,process_id=None):
        return self._register(path,'log',active,process_id)

    def _finish(self,path,kind):
        with self.lock:
            path=self._path(path,kind)
            identity=self._identity(path)
            relative=str(path.relative_to(self.root))
            with self.store.lock,self.store.db:
                previous=self.store.db.execute('SELECT * FROM owned_files WHERE path=? AND kind=?',(relative,kind)).fetchone()
                if previous is None: return False
                if previous['inode'] is not None and identity[:2]!=(previous['device'],previous['inode']):
                    # A rollover or user replacement must be explicitly
                    # registered again; finishing a stale owner cannot claim it.
                    return False
                cursor=self.store.db.execute('UPDATE owned_files SET active=0,finished_at=?,device=?,inode=?,size=?,modified_ns=? WHERE path=? AND kind=?',
                    (time.time(),*identity,relative,kind))
                return bool(cursor.rowcount)

    def finish_recording(self,path): return self._finish(path,'recording')
    def release_log(self,path): return self._finish(path,'log')

    @staticmethod
    def _process_alive(pid):
        if not pid: return None
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            kernel.OpenProcess.restype=wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            handle=kernel.OpenProcess(0x1000,False,pid)
            if not handle: return False if ctypes.get_last_error()==87 else None
            try:
                code=wintypes.DWORD()
                return code.value==259 if kernel.GetExitCodeProcess(handle,ctypes.byref(code)) else None
            finally: kernel.CloseHandle(handle)
        try: os.kill(pid,0); return True
        except ProcessLookupError: return False
        except (OSError,PermissionError): return None

    def reconcile_inactive(self,active_paths=()):
        """Call only after acquiring the application's exclusive instance lock.

        Files with unknown or live owner PIDs remain active after a crash.
        This reconciles registrations and never deletes a file.
        """
        protected={os.path.normcase(os.path.abspath(value)) for value in active_paths}
        released=0
        with self.store.lock:
            rows=self.store.db.execute('SELECT * FROM owned_files WHERE active=1').fetchall()
        for row in rows:
            with self.lock:
                with self.store.lock:
                    current_row=self.store.db.execute('SELECT * FROM owned_files WHERE path=?',(row['path'],)).fetchone()
                if current_row is None or tuple(current_row)!=tuple(row): continue
                path=self.root/row['path']
                if os.path.normcase(str(path)) in protected or self._process_alive(row['process_id']) is not False: continue
                try:
                    path=self._path(path,row['kind'])
                    if self._identity(path)==(None,None,None,None):
                        with self.store.lock,self.store.db: self.store.db.execute('DELETE FROM owned_files WHERE path=?',(row['path'],))
                        released+=1
                        continue
                    if self._finish(path,row['kind']): released+=1
                except (OSError,ValueError): pass
        return released

    def disk_status(self,settings=None):
        settings=self.store.get_settings() if settings is None else settings
        minimum=settings.get('min_free_disk_gib',2)
        if type(minimum) not in (int,float) or not math.isfinite(minimum) or minimum<0: minimum=2
        min_bytes=int(minimum*1024**3)
        try:
            folder=self._path(self.root/'video'/'.disk-check','recording').parent
            free=shutil.disk_usage(folder).free
            return dict(free_bytes=free,min_free_bytes=min_bytes,recording_allowed=free>=min_bytes,error='')
        except (OSError,ValueError):
            return dict(free_bytes=None,min_free_bytes=min_bytes,recording_allowed=False,error='无法读取录制磁盘的剩余空间')

    @staticmethod
    def _active_log_paths():
        # Protect handlers even if a caller accidentally released their record.
        paths=set()
        for logger in [logging.getLogger(),*(value for value in logging.Logger.manager.loggerDict.values() if isinstance(value,logging.Logger))]:
            for handler in logger.handlers:
                name=getattr(handler,'baseFilename',None)
                if name and getattr(handler,'stream',None) is not None: paths.add(os.path.normcase(os.path.abspath(name)))
        return paths

    def _unlink_verified(self,path,expected):
        if os.name!='nt':
            # Recheck the full component chain and final identity immediately
            # before unlink; Windows uses a delete-capable verified handle.
            if self._identity(path)!=expected: return False
            path.unlink()
            return True
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.GetFinalPathNameByHandleW.argtypes=[wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD]
        kernel.GetFinalPathNameByHandleW.restype=wintypes.DWORD
        kernel.SetFileInformationByHandle.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD]
        kernel.SetFileInformationByHandle.restype=wintypes.BOOL
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.CreateFileW(str(path),0x80000000|0x10000,1,None,3,0x00200000,None)
        if handle==ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
        descriptor=None
        try:
            buffer=ctypes.create_unicode_buffer(32768)
            length=kernel.GetFinalPathNameByHandleW(handle,buffer,len(buffer),0)
            if not length or length>=len(buffer): return False
            resolved=buffer.value
            if resolved.startswith('\\\\?\\UNC\\'): resolved='\\\\'+resolved[8:]
            elif resolved.startswith('\\\\?\\'): resolved=resolved[4:]
            if os.path.normcase(resolved)!=os.path.normcase(str(path)): return False
            descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
            info=os.fstat(descriptor)
            actual=(str(info.st_dev),str(info.st_ino),info.st_size,str(info.st_mtime_ns))
            if actual!=expected or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: return False
            disposition=wintypes.BOOL(True)
            if not kernel.SetFileInformationByHandle(handle,4,ctypes.byref(disposition),ctypes.sizeof(disposition)):
                raise ctypes.WinError(ctypes.get_last_error())
            return True
        finally:
            if descriptor is None: kernel.CloseHandle(handle)
            else: os.close(descriptor)

    def cleanup(self,settings=None,protected_paths=(),now=None):
        settings=self.store.get_settings() if settings is None else settings
        stamp=time.time() if now is None else now
        protected={os.path.normcase(os.path.abspath(value)) for value in protected_paths}|self._active_log_paths()
        result=dict(deleted=0,protected=0,failed=0,missing=0,recordings_deleted=0,logs_deleted=0)
        # Read at most 200 registrations per maintenance wake. A cursor also
        # progresses past protected/replaced files without starving later ones.
        with self.store.lock:
            rows=self.store.db.execute('SELECT * FROM owned_files WHERE path>? ORDER BY path LIMIT 200',(self._cleanup_cursor,)).fetchall()
        self._cleanup_cursor=rows[-1]['path'] if len(rows)==200 else ''
        for row in rows:
            with self.lock:
                with self.store.lock:
                    current_row=self.store.db.execute('SELECT * FROM owned_files WHERE path=?',(row['path'],)).fetchone()
                if current_row is None or tuple(current_row)!=tuple(row): continue
                path=self.root/row['path']
                if row['active'] or row['finished_at'] is None or os.path.normcase(str(path)) in protected:
                    result['protected']+=1; continue
                try:
                    path=self._path(path,row['kind'])
                    current=self._identity(path)
                    if current==(None,None,None,None):
                        with self.store.lock,self.store.db: self.store.db.execute('DELETE FROM owned_files WHERE path=?',(row['path'],))
                        result['missing']+=1; continue
                    # Missing finalized output registrations can be forgotten
                    # even when retention is disabled. Existing files always
                    # require an explicitly enabled retention setting.
                    days=settings.get('recording_retention_days' if row['kind']=='recording' else 'log_retention_days',0)
                    if type(days) is not int or days<=0 or row['finished_at']>stamp-days*86400: continue
                    expected=(row['device'],row['inode'],row['size'],row['modified_ns'])
                    if None in expected:
                        result['protected']+=1; continue
                    if current!=expected or int(current[3])/1e9>stamp-days*86400:
                        result['protected']+=1; continue
                    # Recheck component boundaries immediately before opening
                    # the verified delete handle, which never follows links.
                    self._path(path,row['kind'])
                    if not self._unlink_verified(path,expected):
                        result['protected']+=1; continue
                    with self.store.lock,self.store.db: self.store.db.execute('DELETE FROM owned_files WHERE path=?',(row['path'],))
                    result['deleted']+=1
                    result['recordings_deleted' if row['kind']=='recording' else 'logs_deleted']+=1
                except FileNotFoundError: result['missing']+=1
                except (OSError,ValueError): result['failed']+=1
        return result
