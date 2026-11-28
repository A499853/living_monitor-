from __future__ import annotations
import concurrent.futures, csv, hashlib, logging, os, re, shutil, sqlite3, stat, subprocess, threading, time, uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from .storage import now_iso
from .media_options import recording_directory
from .recording_paths import safe_nickname, recording_room_directory, recording_output_path
from .recording_ts import TransportStreamWriter
from .wechat_media import WechatFlvRelay, needs_flv_relay

def safe_message(value, secrets=()):
    text=str(value)
    for secret in sorted((s for s in secrets if isinstance(s,str) and s),key=len,reverse=True): text=text.replace(secret,'[已隐藏]')
    text=re.sub(r'https?://[^\s\]<>"\']+', '[链接已隐藏]', text)
    return text[:2000]

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
        self._process_job=None
    def _own_process(self,process):
        """Windows closes this job on a backend crash, so FFmpeg cannot orphan."""
        if os.name!='nt': return
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        class BasicLimits(ctypes.Structure):
            _fields_=[('ProcessTime',ctypes.c_int64),('JobTime',ctypes.c_int64),('Flags',wintypes.DWORD),('MinWorkingSet',ctypes.c_size_t),('MaxWorkingSet',ctypes.c_size_t),('ActiveProcesses',wintypes.DWORD),('Affinity',ctypes.c_size_t),('Priority',wintypes.DWORD),('Scheduling',wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint64) for name in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
        class ExtendedLimits(ctypes.Structure):
            _fields_=[('Basic',BasicLimits),('Io',IoCounters),('ProcessMemory',ctypes.c_size_t),('JobMemory',ctypes.c_size_t),('PeakProcess',ctypes.c_size_t),('PeakJob',ctypes.c_size_t)]
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];kernel.CreateJobObjectW.restype=wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        if self._process_job is None:
            handle=kernel.CreateJobObjectW(None,None)
            if not handle: raise ctypes.WinError(ctypes.get_last_error())
            limits=ExtendedLimits();limits.Basic.Flags=0x2000  # KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
                error=ctypes.get_last_error();kernel.CloseHandle(handle);raise ctypes.WinError(error)
            self._process_job=(kernel,handle)
        if not kernel.AssignProcessToJobObject(self._process_job[1],wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
    def _save_task(self,room_id,job,state,force=False):
        store=getattr(self.maintenance,'store',None)
        if store is None: return
        now=time.monotonic()
        if not force and now-job.get('persisted_at',0)<5: return
        path=job['path'] if Path(job['path']).exists() else job.get('last_segment_path') or job['path']
        store.save_record_task(room_id,dict(task_id=job['task_id'],state=state,path=path,duration_seconds=job['duration_seconds'],last_media_write_at=job['last_media_write_at'],bytes_written=job['bytes_written'],segment_count=job['segment_count'],process_id=job['process'].pid,resume=self._resume_info(job)))
        job['persisted_at']=now
    def _resume_info(self,job):
        if job.get('pattern')!='': return None
        try:
            info=Path(job['path']).stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size<188: return None
        except OSError: return None
        try:
            with Path(job['path']).open('rb') as handle:
                handle.seek(max(0,info.st_size-8192))
                checkpoint_hash=hashlib.sha256(handle.read(min(8192,info.st_size))).hexdigest()
        except OSError: return None
        return dict(path=job['path'],task_id=job['task_id'],device=str(info.st_dev),inode=str(info.st_ino),
                    size=info.st_size,modified_ns=str(info.st_mtime_ns),duration_seconds=job['duration_seconds'],
                    last_media_write_at=job['last_media_write_at'],timestamp_reference=job.get('timestamp_reference'),
                    checkpoint=not job.get('output_closed',False),checkpoint_hash=checkpoint_hash)
    def _open_output(self,path,resume):
        """Open exactly the owned file; recovery must never truncate or replace it."""
        if self.maintenance: self.maintenance._path(path,'recording')
        if not resume: return path.open('xb',buffering=0)
        descriptor=os.open(path,os.O_RDWR|os.O_APPEND|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0))
        try:
            info=os.fstat(descriptor)
            same_file=(str(info.st_dev),str(info.st_ino))==(resume['device'],resume['inode'])
            unchanged=info.st_size==resume['size'] and str(info.st_mtime_ns)==resume['modified_ns']
            grew=resume.get('checkpoint') and info.st_size>=resume['size']
            if not same_file or not (unchanged or grew) or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                raise ValueError('续录文件已被替换或修改，已保留原文件并等待处理')
            handle=os.fdopen(descriptor,'r+b',buffering=0);descriptor=None
            if grew:
                handle.seek(max(0,resume['size']-8192))
                digest=hashlib.sha256(handle.read(min(8192,resume['size']))).hexdigest()
                if not resume.get('checkpoint_hash') or digest!=resume['checkpoint_hash']:
                    handle.close()
                    raise ValueError('续录检查点内容已更改，已保留原文件并等待处理')
                if info.st_size%188:
                    handle.truncate(info.st_size-info.st_size%188)
            handle.seek(0,os.SEEK_END)
            return handle
        finally:
            if descriptor is not None: os.close(descriptor)
    def _media_progress(self,job,duration):
        """A live process/progress line is insufficient: media time AND disk grow."""
        try: size=Path(job['path']).stat().st_size
        except OSError: size=0
        total=job['closed_bytes']+size
        job['bytes_written']=max(total,job['bytes_written'])
        job['duration_seconds']=max(duration,job['duration_seconds'])
        if duration>job['validated_duration'] and total>job['validated_bytes'] and total>=188:
            job.update(last_progress=time.monotonic(),last_media_write_at=now_iso(),has_media=True,
                       validated_duration=duration,validated_bytes=total)
    def _segment_closed(self,job,line):
        try:
            fields=next(csv.reader([line]))
            path=Path(fields[0]);start=float(fields[1]);end=float(fields[2])
        except (ValueError,IndexError,StopIteration): return False
        expected=Path(job['pattern'] % job['segment_count'])
        if not path.is_absolute(): path=expected.parent/path
        if path.resolve()!=expected.resolve(): return False
        try: size=path.stat().st_size
        except OSError: size=0
        # The segment list reports only closed files. A bounded counter replaces
        # in-memory file lists and directory scans during a months-long stream.
        job['path']=str(path)
        self._media_progress(job,max(job['duration_seconds'],end))
        job['closed_bytes']+=size
        job['segment_count']+=1
        job['last_segment_path']=str(path)
        job['path']=job['pattern'] % job['segment_count']
        if self.maintenance:
            try:
                self.maintenance.register_recording(path,process_id=job['process'].pid)
                self.maintenance.finish_recording(path)
                self.maintenance.register_recording(job['path'],process_id=job['process'].pid,previous_segment=path)
            except (OSError,ValueError,sqlite3.Error):
                job['log_error']='录制片段登记暂时失败，保留文件'
        return True
    def executable(self,settings):
        configured=settings.get('ffmpeg_path','').strip()
        candidates=[Path(configured)] if configured else [self.root/'tools'/'ffmpeg.exe',self.root/'tools'/'ffmpeg'/'ffmpeg.exe',self.root/'tools'/'ffmpeg']
        if not configured:
            # --data-dir separates writable user data from the packaged tools.
            candidates.append(Path(__file__).resolve().parents[2]/'tools'/'ffmpeg.exe')
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
            if len(self.jobs)>=int(settings.get('max_recordings',4)): raise ValueError('录制任务已达到并发上限')
            if self.maintenance and not self.maintenance.disk_status(settings,room=room)['recording_allowed']:
                raise ValueError('磁盘剩余空间低于保护阈值，已暂停录制；监控继续')
            binary=self.executable(settings)
            if not binary: raise ValueError('未找到 FFmpeg，请在设置中配置 ffmpeg.exe')
            if urlsplit(url).scheme not in ['http','https']: raise ValueError('录制源必须为 HTTP(S) 地址')
            segmented=bool(settings.get('record_segment_enabled',False))
            resume=settings.get('_record_resume') if not segmented else None
            directory=Path(resume['path']).parent if resume else recording_directory(self.root,settings,room)/recording_room_directory(room)
            directory.mkdir(parents=True,exist_ok=True)
            stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            output=Path(resume['path']) if resume else recording_output_path(directory)
            pattern=str(output.with_suffix('')).replace('%','%%')+'_%06d.ts' if segmented else ''
            if segmented: output=Path(pattern % 0)
            log=self.root/'logs'/('record_'+rid[:8]+'_'+stamp+'.log')
            log.parent.mkdir(parents=True,exist_ok=True)
            args=[binary,'-hide_banner','-loglevel','warning','-nostats','-progress','pipe:1' if segmented else 'pipe:2','-n','-rw_timeout','15000000']
            # A relay connection is one source acquisition; reconnecting its
            # local URL can replay a finite source and conceal an upstream EOF.
            # Let the engine refresh the signed URL and resume the same TS file.
            if not needs_flv_relay(room,url): args+=self._http_reconnect_args(binary)
            if headers:
                for key,value in headers.items():
                    if any(c in str(key)+str(value) for c in ['\r','\n','\x00']): raise ValueError('请求头包含无效字符')
                args+=['-headers',''.join(f'{k}: {v}\r\n' for k,v in headers.items())]
            args+=['-i',url,'-map','0:v?','-map','0:a?','-c','copy']
            if segmented:
                args+=['-f','segment','-segment_format','mpegts','-segment_time',str(settings.get('record_segment_seconds',1800)),'-reset_timestamps','1','-segment_list','pipe:1','-segment_list_type','csv',pattern]
            else:
                args+=['-muxdelay','0','-muxpreload','0','-avoid_negative_ts','disabled','-output_ts_offset','1','-mpegts_flags','+initial_discontinuity','-f','mpegts','pipe:1']
            handle=None;writer=None;relay=None
            try:
                if not segmented:
                    handle=self._open_output(output,resume)
                    writer=TransportStreamWriter(handle,resume=bool(resume),reference_timestamp=(resume or {}).get('timestamp_reference'))
                if needs_flv_relay(room,url):
                    relay=WechatFlvRelay(url,headers)
                    args[args.index('-i')+1]=relay.url
                process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),shell=False)
            except Exception:
                if relay: relay.close()
                if handle: handle.close()
                raise
            try: self._own_process(process)
            except Exception:
                process.kill();process.communicate(timeout=5)
                if relay: relay.close()
                if handle: handle.close()
                raise
            previous=resume or {}
            base_duration=previous.get('duration_seconds',0)
            job={'process':process,'path':str(output),'duration_seconds':base_duration,'duration_offset':base_duration,'started':time.monotonic(),'stopping':False,'error':'','reported':False,'log':str(log),'has_media':False,'last_progress':time.monotonic(),'last_media_write_at':previous.get('last_media_write_at',''),'bytes_written':previous.get('size',0),'validated_bytes':previous.get('size',0),'validated_duration':base_duration,'closed_bytes':0,'segment_count':0,'pattern':pattern,'last_segment_path':'','task_id':previous.get('task_id') or uuid.uuid4().hex,'output_handle':handle,'ts_writer':writer,'timestamp_reference':previous.get('timestamp_reference')}
            if writer:
                def media_written(_count):
                    with self.lock:
                        first_media=not job['has_media']
                        job['timestamp_reference']=writer.reference_timestamp
                        self._media_progress(job,max(base_duration,writer.duration_seconds))
                        try: self._save_task(rid,job,'recording' if job['has_media'] else 'starting',force=first_media and job['has_media'])
                        except Exception: job['log_error']='录制任务暂时无法保存，媒体读取继续'
                writer.on_write=media_written
            self.jobs[rid]=job
            job['media_relay']=relay
            try: self._save_task(rid,job,'starting',force=True)
            except Exception:
                process.kill();process.communicate(timeout=5);self.jobs.pop(rid,None)
                if relay: relay.close()
                if handle: handle.close()
                raise
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
                    if not segmented:
                        while True:
                            chunk=process.stdout.read1(188*1024)
                            if not chunk: break
                            try:
                                with self.lock: writer.write(chunk)
                            except (OSError,ValueError) as exc:
                                with self.lock:
                                    job['error']='续录文件写入失败：'+safe_message(exc)
                                    job['write_error']=True
                                process.kill()
                                break
                        with self.lock: writer.finish()
                        with self.lock: self._media_progress(job,job['duration_seconds'])
                        return
                    for raw in iter(process.stdout.readline,b''):
                        line=raw.decode('utf-8','replace').strip()
                        if line.startswith('out_time_us='):
                            try:
                                duration=max(0,int(line.split('=',1)[1])/1000000)
                                with self.lock:
                                    self._media_progress(job,duration)
                                    try: self._save_task(rid,job,'recording' if job['has_media'] else 'starting')
                                    except Exception: job['log_error']='录制任务暂时无法保存，媒体读取继续'
                            except ValueError: pass
                        elif segmented and ',' in line:
                            with self.lock:
                                try: self._segment_closed(job,line)
                                except (OSError,ValueError): job['log_error']='录制片段登记暂时失败，保留文件'
                except (OSError,ValueError) as exc:
                    with self.lock:
                        job['error']='续录文件写入失败：'+safe_message(exc)
                        job['write_error']=True
                    if process.poll() is None: process.kill()
                finally:
                    if handle: handle.close()
                    process.stdout.close()
            def read_errors():
                handle=None
                try:
                    try: handle=log.open('a',encoding='utf-8')
                    except OSError:
                        with self.lock: job['log_error']='录制日志暂时无法写入'
                    for raw in iter(process.stderr.readline,b''):
                        raw_line=raw.decode('utf-8','replace').strip()
                        if not segmented and re.match(r'^(?:frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|out_time|dup_frames|drop_frames|speed|progress)=',raw_line):
                            continue
                        line=safe_message(raw_line,secrets)
                        if line:
                            with self.lock:
                                if not job.get('write_error'): job['error']=line
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
            path=job['path'] if Path(job['path']).exists() else job.get('last_segment_path') or job['path']
            return dict(running=code is None,returncode=code,duration_seconds=job['duration_seconds'],path=path,error=job['error'],has_media=job['has_media'],stopping=job['stopping'],last_progress=job['last_progress'],last_media_write_at=job['last_media_write_at'],bytes_written=job['bytes_written'],segment_count=job['segment_count'],task_id=job['task_id'],resume=self._resume_info(job),media_advancing=job['has_media'] and time.monotonic()-job['last_progress']<3,log_error=job.get('log_error',''),stop_attempts=job.get('stop_attempts',0),stop_retry_at=job.get('stop_retry_at',0),stop_retry_exhausted=job.get('stop_attempts',0)>=self.STOP_MAX_ATTEMPTS,stop_error=job.get('stop_error',''))
    def stop(self,room_id):
        future=self.request_stop(room_id)
        return future.result() if future else None
    def request_stop(self,room_id):
        """Reserve a single reaper and return immediately to the scheduler."""
        return self._request_stop(room_id,during_close=False)
    def request_recovery(self,room_id):
        with self.lock:
            job=self.jobs.get(room_id)
            if job and job.get('pattern')=='': job['resume_pending']=True
            return self._request_stop(room_id,during_close=False)
    def finish_output(self,room_id,resume=None):
        with self.lock:
            job=self.jobs.get(room_id)
            if job:
                job['resume_pending']=False
            elif resume and self.maintenance:
                try: self.maintenance.finish_recording(resume['path'])
                except (OSError,ValueError,sqlite3.Error): pass
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
        if job.get('media_relay'): job['media_relay'].close()
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
            job['output_closed']=True
            if job.get('pattern')=='' and job.get('has_media'):
                # Direct MPEG-TS output has no segment-list receipt. Account
                # for its final buffered write before persisting/releasing it.
                self._media_progress(job,job['duration_seconds'])
                job['segment_count']=1
            result=self.status(room_id)
            if job.get('pattern')=='' and job.get('bytes_written',0)>=188:
                result['has_media']=True
            result.update(running=False,stopping=True)
            try: self._save_task(room_id,job,'recovering' if job.get('resume_pending') else 'stopped',force=True)
            except (OSError,ValueError,sqlite3.Error):
                # Process/pipe reclamation must complete even on a full or
                # temporarily unavailable task database. Existing task data
                # remains conservative and will be rebuilt on the next run.
                result['log_error']='录制已停止，但任务状态暂时无法保存'
                logging.getLogger('livewatch').error('record_task_persist_failed room=%s',room_id)
            if self.maintenance:
                try:
                    if job.get('resume_pending'):
                        self.maintenance.register_recording(job['path'],process_id=os.getpid())
                    else: self.maintenance.finish_recording(job['path'])
                except (OSError,ValueError,sqlite3.Error): pass
                try: self.maintenance.release_log(job['log'])
                except (OSError,ValueError,sqlite3.Error): pass
            result['resume_pending']=bool(job.get('resume_pending'))
            if self.jobs.get(room_id) is job: self.jobs.pop(room_id,None)
        if result and not result['has_media']:
            try:
                path=Path(result['path'])
                if path.exists() and path.stat().st_size==0: path.unlink()
            except OSError: pass
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
                if self._process_job:
                    self._process_job[0].CloseHandle(self._process_job[1]);self._process_job=None
            return list(self.close_errors)
        finally:
            self._close_complete.set()
