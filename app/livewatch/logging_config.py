"""Bound app logs by size and day without database work in logging locks."""
from collections import deque
from datetime import date
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path


class AppLogHandler(RotatingFileHandler):
    def __init__(self,path,max_bytes=2*1024*1024,backups=5):
        super().__init__(path,maxBytes=max_bytes,backupCount=backups,encoding='utf-8')
        self.day=date.today()
        self.pending=deque(maxlen=1)

    def shouldRollover(self,record):
        return self.day!=date.today() or super().shouldRollover(record)

    def doRollover(self):
        super().doRollover()
        self.day=date.today()
        snapshots=[]
        for number in range(1,self.backupCount+1):
            path=Path(self.baseFilename+'.'+str(number))
            try:
                info=path.stat(follow_symlinks=False)
                snapshots.append((path,(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)))
            except OSError: pass
        self.pending.append(snapshots)

    def drain_maintenance(self,maintenance):
        # A log call may hold Store.lock, so handler emission must never obtain
        # maintenance/DB locks. Only the background worker drains this queue.
        self.acquire()
        try:
            snapshots=self.pending.pop() if self.pending else []
            self.pending.clear()
        finally: self.release()
        for path,expected in snapshots:
            try:
                info=path.stat(follow_symlinks=False)
                if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)==expected:
                    maintenance.register_log(path,active=False)
            except (OSError,ValueError): pass
        if snapshots:
            try: self.register_active(maintenance)
            except (OSError,ValueError): pass

    def register_active(self,maintenance):
        maintenance.register_log(self.baseFilename,process_id=os.getpid())

