from __future__ import annotations
import concurrent.futures, os, re, shutil, subprocess, threading, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from .storage import room_directory

def safe_message(value, secrets=()):
    text=str(value)
    for secret in sorted((s for s in secrets if isinstance(s,str) and s),key=len,reverse=True): text=text.replace(secret,'[已隐藏]')
    text=re.sub(r'https?://[^\s\]<>"\']+', '[链接已隐藏]', text)
    return text[:2000]

def safe_nickname(value,limit=72):
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', str(value or ''))
    value=''.join(c for c in value if not 0xD800<=ord(c)<=0xDFFF)
    value=re.sub(r'\s+', ' ', value).strip(' .')
    value=value[:limit].rstrip(' .') or '未命名直播间'
    if re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',value): value='_'+value
    return value


def recording_output_path(directory,nickname,when=None):
    directory=Path(directory)
    stamp=(when or datetime.now()).strftime('%Y-%m-%d_%H-%M-%S')
    nickname=safe_nickname(nickname)
    # Reserve room for the extension/collision suffix under the conventional
    # Windows path limit; Unicode astral characters consume two UTF-16 units.
    def units(value): return len(str(value).encode('utf-16-le'))//2
    budget=min(72,240-units(directory.resolve())-len(stamp)-12)
    if budget<8: raise ValueError('软件目录路径过长，请移动到较短的目录后重新录制')
    while units(nickname)>budget: nickname=nickname[:-1]
    nickname=nickname.rstrip(' .') or '直播间'
    stem=stamp+'_'+nickname
    for number in range(10000):
        suffix='' if number==0 else f'_{number:03d}'
        output=directory/(stem+suffix+'.ts')
        if not output.exists(): return output
    raise ValueError('同一时间的录制文件数量过多，请更换保存目录')


class Recorder:
    STOP_MAX_ATTEMPTS=3
    STOP_RETRY_DELAY=2.0
    def __init__(self,root,maintenance=None):
        self.root=Path(root)
        self.lock=threading.RLock()
        self.jobs={}
        self._http_capabilities={}
        self.maintenance=maintenance
        self._reapers=set()
        self._closing=False
        self._closed=False
        self._close_complete=threading.Event()
        self.close_errors=[]
    def executable(self,settings):
        configured=settings.get('ffmpeg_path','').strip()
        candidates=[Path(configured)] if configured else [self.root/'tools'/'ffmpeg.exe',self.root/'tools'/'ffmpeg'/'ffmpeg.exe',self.root/'tools'/'ffmpeg']
        for path in candidates:
            if path.is_file(): return str(path.resolve())
        if not configured: return shutil.which('ffmpeg') or ''
        return ''
    def available(self,settings): return bool(self.executable(settings))
    def _http_reconnect_args(self,binary):
        """Use bounded HTTP recovery only when the selected FFmpeg supports it.

        Connection/5xx failures can recover inside one recording. EOF and 4xx
        return to the engine for status/source revalidation instead of looping
        on an ended broadcast, expired URL, or access restriction.
        """
        try:
            stat=Path(binary).stat()
            identity=(str(binary),stat.st_size,stat.st_mtime_ns)
        except OSError:
            return []
        if identity not in self._http_capabilities:
            try:
                result=subprocess.run([binary,'-hide_banner','-h','protocol=http'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=3,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=False)
                options=set(re.findall(rb'^\s+-(\w+)\s',result.stdout,re.MULTILINE)) if result.returncode==0 else set()
            except (OSError,subprocess.SubprocessError):
                options=set()
            # Keep the cache bounded if the user switches custom binaries.
            if len(self._http_capabilities)>=8: self._http_capabilities.clear()
            self._http_capabilities[identity]=options
        supported=self._http_capabilities[identity]
        required={b'reconnect',b'reconnect_streamed',b'reconnect_on_network_error',b'reconnect_on_http_error',b'reconnect_delay_max'}
        if not required.issubset(supported) or not supported.intersection({b'reconnect_max_retries',b'reconnect_delay_total_max'}):
            return []
        args=['-reconnect','1','-reconnect_streamed','1','-reconnect_on_network_error','1','-reconnect_on_http_error','5xx','-reconnect_delay_max','2']
        for option,value in [('reconnect_max_retries','3'),('reconnect_delay_total_max','6'),('reconnect_at_eof','0'),('respect_retry_after','1')]:
            if option.encode() in supported: args+=['-'+option,value]
        return args
    def start(self,room,url,headers,settings):
        rid=room['id']
        with self.lock:
            if self._closing or self._closed: raise ValueError('录制服务正在退出，不能开始新的录制')
            if rid in self.jobs:
                if self.jobs[rid].get('stopping') or self.jobs[rid]['process'].poll() is None: return
                raise ValueError('上一段录制尚未回收，请稍候')
            binary=self.executable(settings)
            if not binary: raise ValueError('未找到 FFmpeg，请在设置中配置 ffmpeg.exe')
            if urlsplit(url).scheme not in ['http','https']: raise ValueError('录制源必须为 HTTP(S) 地址')
            directory=self.root/'video'/room_directory(room)
            directory.mkdir(parents=True,exist_ok=True)
            stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            output=recording_output_path(directory,room.get('nickname') or '未命名直播间')
            log=self.root/'logs'/('record_'+rid[:8]+'_'+stamp+'.log')
            log.parent.mkdir(parents=True,exist_ok=True)
            args=[binary,'-hide_banner','-loglevel','warning','-nostats','-progress','pipe:1','-n','-rw_timeout','15000000']
            args+=self._http_reconnect_args(binary)
            if headers:
                for key,value in headers.items():
                    if any(c in str(key)+str(value) for c in ['\r','\n','\x00']): raise ValueError('请求头包含无效字符')
                args+=['-headers',''.join(f'{k}: {v}\r\n' for k,v in headers.items())]
            args+=['-i',url,'-map','0:v?','-map','0:a?','-c','copy','-f','mpegts',str(output)]
            process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),shell=False)
            job={'process':process,'path':str(output),'duration_seconds':0,'started':time.monotonic(),'stopping':False,'error':'','reported':False,'log':str(log),'has_media':False,'last_progress':time.monotonic()}
            self.jobs[rid]=job
            if self.maintenance:
                try:
                    self.maintenance.register_recording(output,process_id=process.pid)
                    self.maintenance.register_log(log,process_id=os.getpid())
                except (OSError,ValueError):
                    # Unregistered output is never eligible for automatic deletion.
                    pass
            secrets=list(headers.values())+[url]
            def read_progress():
                try:
                    for raw in iter(process.stdout.readline,b''):
                        line=raw.decode('utf-8','replace').strip()
                        if line.startswith('out_time_us='):
                            try:
                                duration=max(0,int(line.split('=',1)[1])/1000000)
                                with self.lock:
                                    if duration>job['duration_seconds']: job['last_progress']=time.monotonic()
                                    job['duration_seconds']=duration
                                    if duration>0: job['has_media']=True
                            except ValueError: pass
                finally: process.stdout.close()
            def read_errors():
                handle=None
                try:
                    try: handle=log.open('a',encoding='utf-8')
                    except OSError:
                        with self.lock: job['log_error']='录制日志暂时无法写入'
                    for raw in iter(process.stderr.readline,b''):
                        line=safe_message(raw.decode('utf-8','replace').strip(),secrets)
                        if line:
                            with self.lock: job['error']=line
                            if handle:
                                try:
                                    if handle.tell()>=2*1024*1024:
                                        handle.seek(0); handle.truncate()
                                    handle.write(line+'\n'); handle.flush()
                                except OSError:
                                    try: handle.close()
                                    except OSError: pass
                                    handle=None
                                    with self.lock: job['log_error']='录制日志暂时无法写入'
                finally:
                    if handle:
                        try: handle.close()
                        except OSError: pass
                    process.stderr.close()
            job['threads']=[threading.Thread(target=read_progress,daemon=True),threading.Thread(target=read_errors,daemon=True)]
            for thread in job['threads']: thread.start()
    def status(self,room_id):
        with self.lock:
            job=self.jobs.get(room_id)
            if not job: return None
            code=job['process'].poll()
            return dict(running=code is None,returncode=code,duration_seconds=job['duration_seconds'],path=job['path'],error=job['error'],has_media=job['has_media'],stopping=job['stopping'],last_progress=job['last_progress'],log_error=job.get('log_error',''),stop_attempts=job.get('stop_attempts',0),stop_retry_at=job.get('stop_retry_at',0),stop_retry_exhausted=job.get('stop_attempts',0)>=self.STOP_MAX_ATTEMPTS,stop_error=job.get('stop_error',''))
    def stop(self,room_id):
        future=self.request_stop(room_id)
        return future.result() if future else None
    def request_stop(self,room_id):
        """Reserve a single reaper and return immediately to the scheduler."""
        return self._request_stop(room_id,during_close=False)
    def _request_stop(self,room_id,during_close):
        with self.lock:
            job=self.jobs.get(room_id)
            if not job: return None
            previous=job.get('stop_future')
            if self._closed or self._closing and not during_close:
                if previous: return previous
                future=concurrent.futures.Future()
                future.set_exception(RuntimeError('录制服务正在退出'))
                return future
            if previous:
                if not previous.done(): return previous
                if not previous.cancelled() and previous.exception() is None: return previous
                if job.get('stop_attempts',0)>=self.STOP_MAX_ATTEMPTS or time.monotonic()<job.get('stop_retry_at',0): return previous
            job['stopping']=True
            future=concurrent.futures.Future()
            # Consumers may wait on a stop but must not cancel its worker.
            future.set_running_or_notify_cancel()
            job['stop_future']=future
            job['stop_attempts']=job.get('stop_attempts',0)+1
            job['stop_error']=''
            def failed(exc):
                with self.lock:
                    job['stop_error']='录制退出未完成（'+type(exc).__name__+'），文件继续受保护'
                    job['stop_retry_at']=time.monotonic()+self.STOP_RETRY_DELAY
                future.set_exception(exc)
            def reap():
                try: result=self._finish_stop(room_id,job)
                except Exception as exc: failed(exc)
                else: future.set_result(result)
            # Keep independent ownership until the whole worker has returned,
            # including maintenance calls after the process exits. Prune only
            # threads that have actually ended, never from inside the worker.
            self._reapers={thread for thread in self._reapers if thread.is_alive()}
            thread=None
            try:
                thread=threading.Thread(target=reap,name='record-stop-'+room_id[:8],daemon=True)
                self._reapers.add(thread)
                thread.start()
            except Exception as exc:
                if thread is not None: self._reapers.discard(thread)
                failed(exc)
            return future
    def _finish_stop(self,room_id,job):
        process=job['process']
        if process.poll() is None:
            try:
                process.stdin.write(b'q\n'); process.stdin.flush()
            except (OSError,ValueError): pass
            try: process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try: process.kill()
                except OSError:
                    if process.poll() is None: raise
                process.wait(timeout=3)
        if process.poll() is None: raise RuntimeError('录制进程尚未退出，文件继续受保护')
        for thread in job.get('threads',[]):
            # A reader can have been constructed but not started if startup
            # ran out of threads. It owns no active work and cannot be joined.
            if thread.ident is not None: thread.join(timeout=1)
        if any(thread.is_alive() for thread in job.get('threads',[])):
            raise RuntimeError('录制读取线程尚未退出，文件继续受保护')
        for name in ('stdin','stdout','stderr'):
            pipe=getattr(process,name,None)
            if pipe is not None:
                try: pipe.close()
                except (OSError,ValueError): pass
        with self.lock:
            result=dict(running=False,returncode=process.poll(),duration_seconds=job['duration_seconds'],path=job['path'],error=job['error'],has_media=job['has_media'],stopping=True,last_progress=job['last_progress'])
        if self.maintenance:
            try: self.maintenance.finish_recording(job['path'])
            except (OSError,ValueError): pass
            try: self.maintenance.release_log(job['log'])
            except (OSError,ValueError): pass
        if result and not result['has_media']:
            try:
                path=Path(result['path'])
                if path.exists() and path.stat().st_size==0: path.unlink()
            except OSError: pass
        with self.lock:
            if self.jobs.get(room_id) is job: self.jobs.pop(room_id,None)
        return result
    def close(self):
        with self.lock:
            if self._closed: return list(self.close_errors)
            owner=not self._closing
            self._closing=True
        if not owner:
            self._close_complete.wait()
            return list(self.close_errors)
        try:
            while True:
                with self.lock: ids=list(self.jobs)
                # Reserve every stop before waiting; multiple processes exit
                # concurrently rather than multiplying shutdown by room count.
                futures=[self._request_stop(rid,during_close=True) for rid in ids]
                for future in futures:
                    if future:
                        try: future.result()
                        except Exception: pass
                with self.lock:
                    retry=[job for job in self.jobs.values() if job.get('stop_attempts',0)<self.STOP_MAX_ATTEMPTS]
                    if not retry: break
                    delay=max(0,min(job.get('stop_retry_at',0) for job in retry)-time.monotonic())
                if delay: self._close_complete.wait(delay)
            with self.lock: reapers=list(self._reapers)
            for thread in reapers: thread.join()
            with self.lock:
                self.close_errors=[dict(room_id=rid,error=job.get('stop_error') or '录制回收未完成，文件继续受保护') for rid,job in self.jobs.items()]
                self._reapers.clear()
                self._closed=True
            return list(self.close_errors)
        finally:
            self._close_complete.set()
