from __future__ import annotations
import concurrent.futures, hashlib, logging, math, os, re, threading, time
from collections import deque
from datetime import datetime, timedelta, timezone
from .storage import now_iso
from .recorder import Recorder, safe_message
from .providers import start_time_priority, monitor_error_message, sanitize_probe_diagnostic, _douyin_target
from .auth import cookie_is_authorized
from .maintenance import Maintenance

class Engine:
    def __init__(self,store,providers=None,recorder=None):
        isolated_probes=providers is None
        if providers is None:
            from .providers import ProviderRegistry
            providers=ProviderRegistry()
        self.store,self.providers=store,providers
        self.maintenance=Maintenance(store)
        self.recorder=recorder or Recorder(store.root,self.maintenance)
        self.lock=threading.RLock()
        self.stop_event=threading.Event()
        self.wake=threading.Event()
        initial_settings=store.get_settings()
        self.max_concurrent_checks=self._effective_concurrency(initial_settings)
        if isolated_probes:
            from .probe_pool import ProbePool
            # Threads/processes are created lazily. Keeping the fixed upper
            # bound permits increasing the scheduler width without restarting.
            self.pool=ProbePool(providers,max_workers=64)
            self.pool.configure(store.get_settings())
        else:
            self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=64,thread_name_prefix='probe')
        self.states={}
        self.pending={}
        self.recoveries={}
        self.discarded_probes=set()
        self.due={}
        self.status_last_started={}
        self.status_last_finished={}
        self.check_intervals=deque(maxlen=2048)
        self.rate_window=deque()
        self.request_latencies=deque(maxlen=2048)
        self.probe_started_times=deque(maxlen=4096)
        self.started_at=time.monotonic()
        self.thread=None
        self.closed=False
        self.auth_generation=0
        self.record_stop_tasks={}
        self.record_stop_errors=set()
        self.record_stop_retry={}
        self.ffmpeg_prepared=None
        self.ffmpeg_prepare_thread=None
        self.maintenance_wake=threading.Event()
        self.maintenance_thread=None
        self.maintenance_status={'disk':self.maintenance.disk_status(),'last_cleanup':{}}
        self.disk_checked_at=0
        self.douyin_blocked_until=0
        self.douyin_recovery_rid=None
        self.douyin_rejections={}
        self.douyin_network_failures={}
        self.douyin_recovery_successes=0
        self.round_queue=[]
        self.round_active=False
        self.round_number=0
        self.round_finished_at=None
        self.last_probe_finished_at=None
        self.last_probe_submitted_at=None
        self.round_started_at=None
        self.last_round_duration_seconds=None
        self.probe_successes=0
        self.probe_failures=0
        self.probe_timeouts=0
        self.last_dispatch_was_recovery=False
        for room in store.list_rooms(): self._state(room)
    def _state(self,room):
        rid=room['id']
        if rid not in self.states:
            previous=self.store.get_runtime(rid)
            migrated=previous.get('runtime_version',1)<2
            created_time=start_time_priority(previous.get('started_at_source',''))==0
            if created_time: self.store.clear_untrusted_start(room)
            actual='' if migrated or created_time else previous.get('live_started_at','')
            observed=previous.get('observed_started_at','') or (previous.get('live_started_at','') if migrated else '')
            self.states[rid]=dict(live_status='checking' if room['monitor_enabled'] else 'disabled',record_status='recorded' if previous.get('duration_seconds',0)>0 else 'idle',last_checked='',live_started_at=actual,observed_started_at=observed,started_at_source='' if migrated or created_time else previous.get('started_at_source',''),live_session_id=previous.get('live_session_id',''),avatar_url=previous.get('avatar_url',''),live_ended_at=previous.get('live_ended_at',''),duration_seconds=previous.get('duration_seconds',0),last_error='',resolved_stream_url='',stream_variants=[],resolved_room_input='',confirmed_live=previous.get('confirmed_live',None),retry_at=0,record_path=previous.get('record_path',''),record_headers={},source_identity=self._identity(room),source_room=dict(room),error_code='',error_diagnostic='',failure_count=0)
            self.states[rid]['platform_ended_at']=previous.get('platform_ended_at','')
            task=self.store.get_record_task(rid) if hasattr(self.store,'get_record_task') else {}
            self.states[rid].update(last_media_at=task.get('last_media_write_at') or previous.get('last_media_at',''),
                                    record_task_id=task.get('task_id') or previous.get('record_task_id',''),
                                    record_recovery_count=previous.get('record_recovery_count',0),
                                    record_bytes_written=task.get('bytes_written',0),record_segment_count=task.get('segment_count',0))
            if task.get('last_media_write_at'):
                self.states[rid].update(record_path=task.get('path',previous.get('record_path','')),
                                        duration_seconds=task.get('duration_seconds',previous.get('duration_seconds',0)))
            if task.get('state') in ('starting','recording','recovering') and room['record_enabled'] and room['monitor_enabled']:
                self.states[rid]['record_status']='recovering'
                self.store.append_event('record_rebuilt',room,'已重建录制任务；确认房间状态并重新获取直播源后续录')
            # A prior process's cached state is history, not a fresh reading.
            self.states[rid].update(last_success_at=previous.get('last_success_at',''),last_attempt_at='',
                                    last_probe_latency_seconds=None,_last_success_monotonic=None,
                                    _persisted_success_at=previous.get('last_success_at',''))
            self.states[rid].update(last_check_finished_at='',last_actual_check_interval_seconds=None,
                                    detection_uncertain=True,offline_checks=0)
        return self.states[rid]
    @staticmethod
    def _actual_time(value):
        # Treat provider metadata as a time only when it is timezone-aware and
        # plausible. The observation clock is never used as an actual fallback.
        if not isinstance(value,str) or not value: return ''
        try:
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None or parsed<datetime(2000,1,1,tzinfo=timezone.utc) or parsed>datetime.now(timezone.utc)+timedelta(minutes=5): return ''
            return parsed.astimezone().isoformat(timespec='seconds')
        except ValueError: return ''
    def start(self):
        if self.thread: return
        self.thread=threading.Thread(target=self._loop,name='monitor',daemon=True)
        self.thread.start()
        self.maintenance_thread=threading.Thread(target=self._maintenance_loop,name='file-maintenance',daemon=True)
        self.maintenance_thread.start()
    def _maintenance_loop(self):
        next_health=0
        while not self.stop_event.is_set():
            try:
                handler=getattr(self,'log_handler',None)
                if handler: handler.drain_maintenance(self.maintenance)
                result=self.maintenance.cleanup()
                self.maintenance_status['last_cleanup']=dict(result,checked_at=now_iso())
                if result.get('deleted'):
                    self.store.append_event('files_cleaned',None,'按保留设置清理 '+str(result['recordings_deleted'])+' 个录制文件、'+str(result['logs_deleted'])+' 个日志文件')
                if time.monotonic()>=next_health:
                    from .diagnostics import system_health
                    health=system_health()
                    with self.lock:
                        health.update(pending_checks=len(self.pending),record_stops=len(self.record_stop_tasks),recordings=sum((self.recorder.status(rid) or {}).get('running',False) for rid in self.states))
                        if hasattr(self.pool,'stats'): health.update(self.pool.stats())
                    disk=self.maintenance.disk_status()
                    health['disk_free_bytes']=disk['free_bytes']
                    logging.getLogger('livewatch').info('runtime_health %s',' '.join(str(k)+'='+str(v) for k,v in health.items()))
                    next_health=time.monotonic()+1800
            except Exception as exc:
                self.store.append_event('error',None,'自动清理未完成：'+type(exc).__name__)
            # Handle rotation registrations promptly, without scanning folders.
            self.maintenance_wake.wait(60); self.maintenance_wake.clear()
    def _loop(self):
        while not self.stop_event.is_set():
            try: self._tick()
            except Exception as exc:
                self.store.append_event('error',None,'监控调度异常：'+safe_message(exc))
            settings=self.store.get_settings()
            if settings.get('status_only_mode',True):
                with self.lock:
                    now=time.monotonic()
                    wake_times=[self._status_due_at(rid,settings) for rid,state in self.states.items()
                                if state['source_room']['monitor_enabled'] and rid not in self.pending and rid not in self.recoveries]
                    wake_times.extend(rescue['next_at'] for rid,rescue in self.recoveries.items() if rid not in self.pending)
                    future=[at-now for at in wake_times if at>now]
                self.wake.wait(min(.2,min(future)) if future else .2); self.wake.clear()
                continue
            until_next=self._next_dispatch_at(settings)-time.monotonic()
            self.wake.wait(min(.4,until_next) if until_next>0 else .4); self.wake.clear()
    def _tick(self):
        with self.lock:
            if self.stop_event.is_set(): return
            self._drain_record_stops()
            rooms=self.store.list_rooms(); known={r['id'] for r in rooms}
            for rid in list(self.states):
                if rid not in known:
                    self._stop_record(rid,None)
                    self.states.pop(rid,None); self.due.pop(rid,None); self.status_last_started.pop(rid,None); self.status_last_finished.pop(rid,None)
                    self.douyin_rejections.pop(rid,None); self.douyin_network_failures.pop(rid,None)
                    self._cancel_recovery(rid)
            settings=self.store.get_settings()
            if time.monotonic()-self.disk_checked_at>=5:
                self.maintenance_status['disk']=self.maintenance.disk_status(settings)
                self.disk_checked_at=time.monotonic()
            self.max_concurrent_checks=self._effective_concurrency(settings)
            authorized=cookie_is_authorized(settings.get('douyin_cookie',''))
            eligible={}
            for room in rooms:
                rid=room['id']; state=self._state(room)
                self._inspect_record(room,state)
                if not room['monitor_enabled']:
                    self._cancel_recovery(rid)
                    if rid in self.pending: self.discarded_probes.add(self.pending[rid][0])
                    state['live_status']='disabled'
                    self._stop_record(rid,room)
                    continue
                if (not settings.get('status_only_mode',True) and state.get('_offline_recheck_at') is not None
                        and time.monotonic()>=state['_offline_recheck_at']):
                    self._enqueue_checks([rid])
                if not room['record_enabled']: self._stop_record(rid,room)
                elif state['confirmed_live'] is True and state.get('resolved_stream_url') and state['live_status']=='live':
                    self._start_record(room,state)
                if room['record_enabled'] and state['confirmed_live'] is True and state.get('record_recheck_at') is not None and time.monotonic()>=state['record_recheck_at']:
                    self._enqueue_checks([rid])
                    state.pop('record_recheck_at',None)
                if room['platform']=='douyin' and getattr(self.providers,'requires_auth',False):
                    if not authorized:
                        self._cancel_recovery(rid)
                        state.update(live_status='error',error_code='auth_required',error_diagnostic='',last_error='请先在软件中登录抖音并完成授权')
                        continue
                eligible[rid]=room
            now=time.monotonic()
            self._expire_recoveries(now)
            if settings.get('status_only_mode',True):
                self._tick_status(eligible,settings,now)
                return
            self.round_queue=[rid for rid in self.round_queue if rid in eligible]
            independent=settings.get('monitor_schedule_mode','independent')=='independent'
            if self.recoveries:
                self._retry_recoveries(eligible,settings,now)
                if not independent: return
            # Recorder supervision above continues during requests and round waits.
            if self.pending and not independent: return
            now=time.monotonic()
            self._complete_round(now)
            if not self.round_active:
                if not eligible or now<self._next_round_at(settings): return
                self._enqueue_checks(list(eligible))
            if now<self._next_dispatch_at(settings): return
            batch=[]
            for rid in list(self.round_queue):
                if len(self.pending)>=self.max_concurrent_checks: break
                if not self._request_budget_available(settings,time.monotonic()): break
                if independent and time.monotonic()<self._next_dispatch_at(settings): break
                # Configuration/auth changes may queue a fresh check while an
                # obsolete request still owns its slot. Never overlap a room.
                if rid in self.pending or rid in self.recoveries: continue
                room=eligible[rid]
                # Restrictions defer this room to a later round, without holding
                # healthy or custom sources behind it in the queue.
                if now<self.due.get(rid,0):
                    self.round_queue.remove(rid)
                    continue
                if self._adaptive_room(room):
                    if now<self.douyin_blocked_until:
                        self.round_queue.remove(rid)
                        continue
                    if self.douyin_recovery_rid or not self._platform_slot_available(settings): continue
                    if self.douyin_blocked_until: self.douyin_recovery_rid=rid
                self.round_queue.remove(rid)
                self.due[rid]=0
                submitted_at=time.monotonic()
                timeout=self._probe_timeout(settings,room)
                probe_settings=self._probe_settings(room,settings,submitted_at+timeout)
                future=self.pool.submit(self.providers.check,dict(room),probe_settings)
                self.pending[rid]=(future,self._identity(room),self.auth_generation,submitted_at)
                self._record_submission(room,submitted_at)
                self.last_probe_submitted_at=submitted_at
                self.last_dispatch_was_recovery=False
                batch.append((rid,future))
            # Register after reserving the entire batch: already-completed
            # futures run callbacks synchronously and must not finish a round
            # or start its pause before the other members have been reserved.
            for rid,future in batch:
                future.add_done_callback(lambda f,r=rid:self._finished(r,f))
            self._complete_round(time.monotonic())
    def _next_dispatch_at(self,settings):
        if settings.get('status_only_mode',True): return 0
        last=self.last_probe_submitted_at if settings.get('monitor_schedule_mode','independent')=='independent' else self.last_probe_finished_at
        return 0 if last is None else last+settings['room_check_delay_seconds']

    def _status_due_at(self,rid,settings):
        started=self.status_last_started.get(rid)
        # Fixed delay starts after completion, including timeout/failed checks.
        # An explicit refresh removes started; no pending room can be reentered.
        finished=self.status_last_finished.get(rid,started)
        regular=0 if started is None else finished+settings.get('status_poll_interval_seconds',8)
        state=self.states.get(rid,{})
        if state.get('_offline_recheck_at') is not None:
            regular=state['_offline_recheck_at']
        elif state.get('_error_recheck_at') is not None:
            regular=state['_error_recheck_at']
        return max(regular,self.due.get(rid,0))

    def _request_budget_available(self,settings,now):
        while self.rate_window and now-self.rate_window[0]>=1:
            self.rate_window.popleft()
        return len(self.rate_window)<max(1,min(100,int(settings.get('monitor_max_requests_per_second',20))))

    def _record_submission(self,room,submitted_at):
        state=self._state(room)
        previous=state.get('_last_attempt_monotonic')
        if previous is not None:
            interval=max(0,submitted_at-previous)
            state['last_actual_check_interval_seconds']=round(interval,3)
            self.check_intervals.append(interval)
        state['_last_attempt_monotonic']=submitted_at
        self.status_last_started[room['id']]=submitted_at
        self.probe_started_times.append(submitted_at)
        self.rate_window.append(submitted_at)
        state['last_attempt_at']=now_iso()

    def _probe_settings(self,room,settings,deadline):
        state=self._state(room)
        refresh=bool(state.pop('_source_refresh_requested',False))
        state['source_refreshing']=refresh
        return dict(settings,_probe_deadline=deadline,
                    _status_only=bool(settings.get('status_only_mode',True)) and not refresh)

    def _tick_status(self,eligible,settings,now):
        """Each room owns one completion-to-start timer; no round barrier.

        A late request is followed by at most one new request. Failed rooms and
        platform cooldowns cannot delay another source's next regular check.
        """
        self.round_queue=[]
        self.round_active=False
        if self.recoveries:
            self._retry_recoveries(eligible,settings,now)
        batch=[]
        # Offline confirmation controls irreversible session closure; ready
        # confirmations get the next available budget before another regular
        # cycle. A not-yet-due confirmation does not block any other room.
        ordered=sorted(eligible,key=lambda rid:(0 if self.states[rid].get('offline_checks') else 1,
                                              self._status_due_at(rid,settings),eligible[rid]['sort_order']))
        for rid in ordered:
            if len(self.pending)>=self.max_concurrent_checks: break
            if not self._request_budget_available(settings,time.monotonic()): break
            if rid in self.pending or rid in self.recoveries: continue
            if now<self._status_due_at(rid,settings): continue
            room=eligible[rid]
            if self._adaptive_room(room):
                if now<self.douyin_blocked_until or self.douyin_recovery_rid or not self._platform_slot_available(settings): continue
                if self.douyin_blocked_until: self.douyin_recovery_rid=rid
            submitted_at=time.monotonic()
            probe_settings=self._probe_settings(room,settings,submitted_at+self._probe_timeout(settings,room))
            future=self.pool.submit(self.providers.check,dict(room),probe_settings)
            self.pending[rid]=(future,self._identity(room),self.auth_generation,submitted_at)
            self._record_submission(room,submitted_at)
            self.last_probe_submitted_at=submitted_at
            self.last_dispatch_was_recovery=False
            batch.append((rid,future))
        for rid,future in batch:
            future.add_done_callback(lambda f,r=rid:self._finished(r,f))

    @staticmethod
    def _effective_concurrency(settings):
        """Return the bounded scheduler width for the current profile.

        ``status_check_concurrency`` applies only to the status-only profile;
        the legacy batch setting remains the cap for metadata/recording work.
        """
        if settings.get('status_only_mode',True):
            return max(1,min(64,int(settings.get('status_check_concurrency',32))))
        return max(1,min(16,int(settings.get('monitor_batch_size',3))))

    @staticmethod
    def _probe_timeout(settings,room):
        if settings.get('status_only_mode',True):
            # Status-only probes have no reason to wait for a page or media
            # body.  A short absolute deadline is what keeps a dead endpoint
            # from making the next 5-second wave stale.
            return max(2,min(15,float(settings.get('status_request_timeout',4))))
        return max(5,min(30,float(settings.get('request_timeout',12))))
    def _platform_slot_available(self,settings):
        # Submitted source identities survive deletion or edits, so an obsolete
        # official request keeps consuming the platform's real request budget.
        active=sum(identity[0]=='douyin' and not identity[3] and not identity[4] for _,identity,*_ in self.pending.values())
        limit=settings.get('status_douyin_concurrency',32) if settings.get('status_only_mode',True) else settings.get('monitor_douyin_concurrency',2)
        return active<min(self._effective_concurrency(settings),limit)
    def _fresh_checks_waiting(self,eligible,settings,now):
        if settings.get('status_only_mode',True):
            return any(rid not in self.pending and rid not in self.recoveries
                       and now>=self._status_due_at(rid,settings)
                       and (not self._adaptive_room(room) or
                            (now>=self.douyin_blocked_until and not self.douyin_recovery_rid and self._platform_slot_available(settings)))
                       for rid,room in eligible.items())
        for rid in self.round_queue:
            room=eligible.get(rid)
            if room is None or rid in self.pending or rid in self.recoveries or now<self.due.get(rid,0): continue
            if not self._adaptive_room(room) or (now>=self.douyin_blocked_until and not self.douyin_recovery_rid and self._platform_slot_available(settings)):
                return True
        return False
    def _next_round_at(self,settings):
        if self.round_finished_at is None: return 0
        interval=(settings.get('status_poll_interval_seconds',8)
                  if settings.get('status_only_mode',True)
                  else settings['monitor_interval_minutes']*60)
        return self.round_finished_at+interval
    def _complete_round(self,now):
        if self.round_active and not self.round_queue and not self.pending and not self.recoveries:
            self.round_active=False
            self.round_finished_at=now
            if self.round_started_at is not None:
                self.last_round_duration_seconds=max(0,now-self.round_started_at)
    def _enqueue_checks(self,ids,include_pending=False):
        ids=list(dict.fromkeys(rid for rid in ids if include_pending or rid not in self.pending and rid not in self.recoveries))
        if not ids: return
        if self.store.get_settings().get('status_only_mode',True):
            for rid in ids: self.status_last_started.pop(rid,None)
            return
        if not self.round_active:
            self.round_active=True
            self.round_number+=1
            self.round_started_at=time.monotonic()
        self.round_queue=ids+[rid for rid in self.round_queue if rid not in ids]
    @staticmethod
    def _identity(room):
        return tuple(str(room.get(k,'')) for k in ['platform','room_input','input_type','stream_url','status_url','request_headers'])
    def _adaptive_room(self,room):
        return bool(getattr(self.providers,'adaptive_polling',False) and room['platform']=='douyin' and not room.get('status_url') and not room.get('stream_url'))
    def _finished(self,rid,future):
        with self.lock:
            submitted=self.pending.get(rid)
            if submitted is None or submitted[0] is not future: return
            self._expire_recoveries(time.monotonic())
            self.pending.pop(rid)
            # Even stale, failed or deleted-room requests consume a real slot.
            self.last_probe_finished_at=time.monotonic()
            if rid in self.states:
                self.status_last_finished[rid]=self.last_probe_finished_at
                self.states[rid]['last_check_finished_at']=now_iso()
                self.states[rid]['source_refreshing']=False
            if len(submitted)>3:
                latency=max(0,self.last_probe_finished_at-submitted[3])
                self.request_latencies.append(latency)
                if rid in self.states: self.states[rid]['last_probe_latency_seconds']=round(latency,3)
            try:
                observed=future.result()
                if getattr(observed,'live',None) is None:
                    self.probe_failures+=1
                    if any(kind in str(getattr(observed,'diagnostic','')) for kind in ('network=timeout','network=deadline')):
                        self.probe_timeouts+=1
                else:
                    self.probe_successes+=1
            except Exception:
                self.probe_failures+=1
            try:
                self._accept_finished(rid,future,submitted)
            finally:
                self._complete_round(time.monotonic())
                self.wake.set()
    def _accept_finished(self,rid,future,submitted):
        with self.lock:
            recovering=self.douyin_recovery_rid==rid
            if recovering: self.douyin_recovery_rid=None
            if future in self.discarded_probes:
                self.discarded_probes.discard(future)
                return
            if self.stop_event.is_set(): return
            try: room=self.store.get_room(rid)
            except ValueError: return
            if not room['monitor_enabled'] or submitted is None or submitted[1]!=self._identity(room) or (len(submitted)>2 and submitted[2]!=self.auth_generation): return
            state=self._state(room)
            try: result=future.result()
            except Exception as exc:
                code='network_error' if isinstance(exc,(OSError,TimeoutError)) else ''
                self._handle_probe_error(room,state,'检查失败：'+safe_message(exc),code); return
            # Nickname and avatar are room metadata, also useful while offline.
            if result.nickname and result.nickname!=room['nickname']:
                room=self.store.update_room(rid,{'nickname':str(result.nickname)[:160]})
            avatar=getattr(result,'avatar_url','')
            if isinstance(avatar,str) and avatar.startswith(('http://','https://')):
                state['avatar_url']=avatar
            state['source_room']=dict(room)
            if result.live is None:
                self._handle_probe_error(room,state,result.message or '无法确认直播状态，请检查来源配置',getattr(result,'error_code',''),getattr(result,'retry_after',0),getattr(result,'diagnostic',''))
                self._persist(rid,state)
                return
            state['last_success_at']=now_iso()
            state['_last_success_monotonic']=time.monotonic()
            state['detection_uncertain']=False
            state.pop('_error_recheck_at',None)
            self.due[rid]=0
            rescue=self.recoveries.pop(rid,None)
            if rescue:
                self.store.append_event('probe_rescued',room,'短暂连接异常已恢复，补查 '+str(rescue['attempts'])+' 次，保留原直播场次。')
            at=now_iso(); old=state['confirmed_live']
            if self._adaptive_room(room):
                self.douyin_rejections.pop(rid,None)
                self.douyin_network_failures.pop(rid,None)
                if recovering:
                    self.douyin_recovery_successes+=1
                    if self.douyin_recovery_successes>=2:
                        self.douyin_blocked_until=0
                        self.douyin_rejections.clear()
                        self.douyin_network_failures.clear()
                        self.store.append_event('monitor_recovered',room,'连续两次试探检查成功，恢复设定的每批检查数量。')
                    else:
                        self.douyin_blocked_until=max(self.douyin_blocked_until,time.monotonic()+5)
                if getattr(result,'retry_after',0):
                    self.due[rid]=max(self.due.get(rid,0),time.monotonic()+self._retry_delay(result.retry_after))
            # Ending a live session stops recording, so require consecutive
            # explicit offline observations for every source and profile.
            # Errors reset this count and never close the existing session.
            if old is True and result.live is False:
                state['offline_checks']=state.get('offline_checks',0)+1
                if state['offline_checks']<max(2,getattr(self.providers,'offline_confirmations',2)):
                    state['last_checked']=at
                    state['status_note']='已收到一次下播状态，1 秒后快速复核'
                    state['_offline_recheck_at']=time.monotonic()+1
                    if not self.store.get_settings().get('status_only_mode',True):
                        self.due[rid]=state['_offline_recheck_at']
                        self._enqueue_checks([rid])
                    self._persist(rid,state)
                    return
            state['offline_checks']=0
            state.pop('_offline_recheck_at',None)
            state['status_note']=''
            state.pop('record_needs_source',None)
            state['last_checked']=at
            if state.get('failure_count',0):
                self.store.append_event('probe_recovered',room,'检查已恢复，之前连续 '+str(state['failure_count'])+' 次检查失败。')
            state['failure_count']=0; state['error_code']=''; state['error_diagnostic']=''
            if state['record_status']!='failed': state['last_error']=''
            state['confirmed_live']=bool(result.live)
            state['live_status']='live' if result.live else 'waiting'
            state['resolved_stream_url']=result.stream_url or ''
            state['record_headers']=dict(result.headers or {})
            state['stream_variants']=list(getattr(result,'stream_variants',[]) or [])
            resolved=getattr(result,'resolved_room_input','')
            if resolved: state['resolved_room_input']=resolved
            actual=self._actual_time(getattr(result,'live_started_at',''))
            platform_id=str(getattr(result,'live_session_id','') or '')[:160]
            source=str(getattr(result,'started_at_source','') or '')[:160] if actual else ''
            if start_time_priority(source)==0:
                actual=''; source=''
            if result.live:
                state['platform_ended_at']=''
                previous_id=state['live_session_id']
                # A changed platform room/session ID proves a new broadcast even
                # when the app was closed throughout the offline interval.
                changed=old is True and bool(previous_id and platform_id and previous_id!=platform_id)
                if not changed and old is True and not (previous_id and platform_id) and actual and state['live_started_at'] and start_time_priority(source) >= 3 and start_time_priority(state['started_at_source']) >= 3:
                    changed=actual!=state['live_started_at']
                if changed:
                    self._stop_record(rid,room)
                    self.store.close_session(room,at,reason='observed_session_changed')
                    self.store.append_event('live_session_changed',room,'检测到新的直播场次；上一场结束时刻未由平台提供')
                if old is not True or changed:
                    if (old is False or changed) and hasattr(self.store,'promote_room'): self.store.promote_room(rid)
                    state.update(live_started_at=actual,observed_started_at=at,started_at_source=source,live_session_id=platform_id,live_ended_at='',retry_at=0)
                    self.store.open_session(room,at,actual,platform_id,source)
                    self.store.append_event('live_started',room,'监测到开播'+('，已取得平台开播时间' if actual else '，平台尚未提供真实开播时间'))
                    self._sound()
                else:
                    # An API response may temporarily omit metadata. Preserve a
                    # confirmed real start for the same known broadcast only.
                    if actual and not (state['live_started_at'] and start_time_priority(state['started_at_source']) > start_time_priority(source)):
                        state['live_started_at']=actual
                        state['started_at_source']=source
                    if platform_id: state['live_session_id']=platform_id
                    if not state['observed_started_at']: state['observed_started_at']=at
                    self.store.open_session(room,at if self.store.get_open_session(rid) else state['observed_started_at'],state['live_started_at'],state['live_session_id'],state['started_at_source'])
                if room['record_enabled']: self._start_record(room,state)
            else:
                ended=self._actual_time(getattr(result,'live_ended_at',''))
                if ended: state['platform_ended_at']=ended
                self._stop_record(rid,room)
                if state['duration_seconds']>0: state['record_status']='recorded'
                if old is True:
                    state['live_ended_at']=at
                    self.store.close_session(room,at)
                    self.store.append_event('live_ended',room,'监测到下播（本机检测时间）')
            self._persist(rid,state)
    def _persist(self,rid,state):
        payload={k:state[k] for k in ['confirmed_live','live_started_at','observed_started_at','started_at_source','live_session_id','avatar_url','live_ended_at','duration_seconds','record_path']}
        payload['runtime_version']=2
        payload['platform_ended_at']=state.get('platform_ended_at','')
        payload.update({key:state.get(key) for key in ('last_media_at','record_task_id','record_recovery_count')})
        now=time.monotonic()
        # Persist freshness on a state change, a minute checkpoint, or graceful
        # exit. Thirty status reads per second need not become thirty commits.
        if (self.closed or payload!=state.get('_persisted_runtime_payload')
                or now-state.get('_last_success_checkpoint_monotonic',0)>=60):
            state['_persisted_runtime_payload']=dict(payload)
            state['_persisted_success_at']=state.get('last_success_at','')
            state['_last_success_checkpoint_monotonic']=now
        payload['last_success_at']=state.get('_persisted_success_at','')
        self.store.save_runtime(rid,payload)
    @staticmethod
    def _transient_error(error_code,diagnostic):
        facts=dict(token.split('=',1) for token in sanitize_probe_diagnostic(diagnostic).split())
        if any(key in facts for key in ('retry_after','page_retry_after')): return False
        if facts.get('http') in ('401','403','429','444') or facts.get('page_http') in ('401','403','429','444'): return False
        if facts.get('page_body') in ('non_json','verification') or facts.get('body')=='verification': return False
        if error_code in ('network_error','platform_unavailable'): return True
        if error_code=='http_error': return facts.get('http','').startswith('5')
        return error_code=='anonymous_restricted' and (facts.get('body') in ('empty','missing_room') or facts.get('platform_status')=='30003')
    def _handle_probe_error(self,room,state,message,error_code='',retry_after=0,diagnostic=''):
        rid=room['id']; now=time.monotonic()
        state['detection_uncertain']=True
        state['offline_checks']=0
        state.pop('_offline_recheck_at',None)
        rescue=self.recoveries.get(rid)
        if state['live_status']!='error' and self._transient_error(error_code,diagnostic):
            if rescue is None:
                rescue=dict(deadline=now+10,next_at=now+1,attempts=0,identity=self._identity(room),auth_generation=self.auth_generation)
                self.recoveries[rid]=rescue
                detail=sanitize_probe_diagnostic(diagnostic)
                self.store.append_event('probe_retrying',room,'检测异常，正在快速恢复 [code='+('network_error' if error_code=='network_error' else 'transient')+(' '+detail if detail else '')+']')
            else:
                rescue['next_at']=now+2
            rescue['error']=(message,error_code,retry_after,sanitize_probe_diagnostic(diagnostic))
            state['status_note']='连接暂时不稳，正在 10 秒内自动补查；当前保留上次确认的状态'
            state['offline_checks']=0
            return
        self.recoveries.pop(rid,None)
        state['status_note']=''
        self._probe_error(room,state,message,error_code,retry_after,diagnostic)
    def _expire_recoveries(self,now):
        for rid,rescue in list(self.recoveries.items()):
            try: room=self.store.get_room(rid)
            except ValueError:
                self._cancel_recovery(rid); continue
            if not room['monitor_enabled'] or rescue['identity']!=self._identity(room) or rescue['auth_generation']!=self.auth_generation:
                self._cancel_recovery(rid); continue
            if now<rescue['deadline']: continue
            self.recoveries.pop(rid,None)
            pending=self.pending.get(rid)
            if pending: self.discarded_probes.add(pending[0])
            self._state(room)['status_note']=''
            self._probe_error(room,self._state(room),*rescue['error'])
            self._persist(rid,self._state(room))
    def _cancel_recovery(self,rid):
        if self.recoveries.pop(rid,None):
            pending=self.pending.get(rid)
            if pending: self.discarded_probes.add(pending[0])
            if rid in self.states: self.states[rid]['status_note']=''
    def _retry_recoveries(self,eligible,settings,now):
        batch=[]
        independent=settings.get('status_only_mode',True) or settings.get('monitor_schedule_mode','independent')=='independent'
        # Fast failing recoveries can finish before the next dispatch interval.
        # Alternate their dispatch opportunities with ready fresh rooms too,
        # rather than relying only on the number of in-flight recoveries.
        if independent and self.last_dispatch_was_recovery and self._fresh_checks_waiting(eligible,settings,now): return
        for rid,rescue in list(self.recoveries.items()):
            if len(self.pending)>=self.max_concurrent_checks: break
            if not self._request_budget_available(settings,time.monotonic()): break
            if independent and time.monotonic()<self._next_dispatch_at(settings): break
            # Leave capacity for fresh rooms during a wave of transient errors.
            if independent and self.max_concurrent_checks>1 and any(key in self.recoveries for key in self.pending) and self._fresh_checks_waiting(eligible,settings,now): break
            if rid not in eligible or rid in self.pending or now<rescue['next_at']: continue
            room=eligible[rid]
            if self._adaptive_room(room):
                if now<self.douyin_blocked_until or self.douyin_recovery_rid or not self._platform_slot_available(settings): continue
                if self.douyin_blocked_until: self.douyin_recovery_rid=rid
            submitted_at=time.monotonic()
            retry_settings=self._probe_settings(room,settings,min(rescue['deadline'],submitted_at+2.5))
            future=self.pool.submit(self.providers.check,dict(room),retry_settings)
            self.pending[rid]=(future,self._identity(room),self.auth_generation,submitted_at)
            self._record_submission(room,submitted_at)
            self.last_probe_submitted_at=submitted_at
            self.last_dispatch_was_recovery=True
            rescue['attempts']+=1
            batch.append((rid,future))
        for rid,future in batch: future.add_done_callback(lambda f,r=rid:self._finished(r,f))
    @staticmethod
    def _retry_delay(value):
        return min(86400,max(0,value)) if type(value) is int or type(value) is float and math.isfinite(value) else 0
    @staticmethod
    def _diagnostic_reason(value):
        # Timing/size observations change on every retry; keep the latest facts
        # in state without turning an unchanged failure into repetitive logs.
        return ' '.join(token for token in value.split() if token.partition('=')[0] not in ('elapsed_ms','bytes','page_elapsed_ms','page_bytes'))
    def _probe_error(self,room,state,message,error_code='',retry_after=0,diagnostic=''):
        diagnostic=sanitize_probe_diagnostic(diagnostic)
        source=room.get('status_url') or room.get('stream_url')
        anonymous_source=not source or _douyin_target(source)
        if room['platform']=='douyin' and anonymous_source and error_code in ('anonymous_restricted','verification_required','rate_limited'):
            message=monitor_error_message(error_code,diagnostic)
        message=safe_message(message,list(room.get('request_headers',{}).values())+[self.store.get_settings().get('douyin_cookie','')])
        if (state['last_error'],state.get('error_code',''),self._diagnostic_reason(state.get('error_diagnostic','')))!=(message,error_code,self._diagnostic_reason(diagnostic)):
            known_codes={'invalid_input','auth_required','verification_required','anonymous_restricted','rate_limited','account_rate_limited','http_error','network_error','target_mismatch','account_not_found','platform_unavailable','auth_changed','status_source_required'}
            logged_code=error_code if error_code in known_codes else 'unknown'
            self.store.append_event('probe_failed',room,message+' [code='+logged_code+(' '+diagnostic if diagnostic else '')+']')
        state.update(last_checked=now_iso(),live_status='error',last_error=message,error_code=error_code,error_diagnostic=diagnostic,failure_count=state.get('failure_count',0)+1)
        state['detection_uncertain']=True
        state['offline_checks']=0
        state.pop('_offline_recheck_at',None)
        if self._adaptive_room(room):
            now=time.monotonic()
            restricted=error_code in ('anonymous_restricted','rate_limited','verification_required')
            network_failure=error_code=='network_error'
            if self.store.get_settings().get('status_only_mode',True) and not restricted:
                # A broken local connection is not evidence that every room is
                # rate limited. Keep retrying this room on its bounded cadence.
                facts=dict(token.split('=',1) for token in diagnostic.split())
                # Network ProbeError uses a legacy advisory 30-second retry;
                # only an actual server Retry-After may postpone fast recovery.
                explicit=max(int(facts.get('retry_after','0')),int(facts.get('page_retry_after','0')),
                             0 if network_failure else self._retry_delay(retry_after))
                state['_error_recheck_at']=now+min(5,self.store.get_settings().get('status_poll_interval_seconds',8))
                self.due[room['id']]=now+explicit
                state['status_note']='检测尚未恢复，继续独立重试'
                return
            # Respect server Retry-After; otherwise retry a first restriction
            # promptly and progressively back off repeated restrictions.
            base=8 if restricted else 10
            delay=min(300,base*2**min(state['failure_count']-1,4))
            delay=max(delay,self._retry_delay(retry_after))
            jitter=int(hashlib.sha256(room['id'].encode()).hexdigest()[:4],16)%7
            self.due[room['id']]=now+delay+jitter
            state['status_note']='已降低重试频率，等待自动复查'
            if restricted:
                # One room may spend ten seconds rescuing before failure is
                # recorded. A 15-second window misses every third serial room.
                self.douyin_rejections={key:at for key,at in self.douyin_rejections.items() if now-at<120}
                self.douyin_rejections[room['id']]=now
            if network_failure:
                self.douyin_network_failures={key:at for key,at in self.douyin_network_failures.items() if now-at<60}
                self.douyin_network_failures[room['id']]=now
            widespread=restricted and len(self.douyin_rejections)>=3 or network_failure and len(self.douyin_network_failures)>=3
            if widespread or self.douyin_blocked_until:
                cooldown=max(30 if network_failure else min(120,delay),self._retry_delay(retry_after))
                if not self.douyin_blocked_until:
                    reason='多个直播间连续网络连接失败' if network_failure else '多个直播间连续受到平台访问限制'
                    self.store.append_event('monitor_backoff',room,reason+'，暂缓抖音检查，稍后以单房间试探恢复。')
                self.douyin_recovery_successes=0
                self.douyin_blocked_until=max(self.douyin_blocked_until,now+cooldown)
            return
        facts=dict(token.split('=',1) for token in diagnostic.split())
        explicit=max(0 if error_code=='network_error' else self._retry_delay(retry_after),
                     int(facts.get('retry_after','0')),int(facts.get('page_retry_after','0')))
        self.due[room['id']]=max(self.due.get(room['id'],0),time.monotonic()+explicit) if explicit else self.due.get(room['id'],0)
        state['_error_recheck_at']=time.monotonic()+min(5,self.store.get_settings().get('status_poll_interval_seconds',8))
    def _sound(self):
        if self.store.get_settings().get('sound_enabled') and os.name=='nt':
            try:
                import winsound
                winsound.PlaySound('SystemNotification',winsound.SND_ALIAS|winsound.SND_ASYNC)
            except (RuntimeError,ImportError): pass
    def _start_record(self,room,state):
        rid=room['id']; settings=self.store.get_settings()
        if rid in self.record_stop_tasks: return
        status=self.recorder.status(rid)
        selection=(room.get('record_quality',''),room.get('record_protocol',''))
        if status and status['running']:
            if selection!=state.get('_record_selection',selection):
                self._queue_record_stop(rid,room,report_saved=True)
                state.update(record_status='recovering',record_needs_source=True,record_recheck_at=time.monotonic(),retry_at=0)
                self.store.append_event('record_source_changed',room,'录制画质或格式已更改，重新取源后创建新片段')
            return
        if state.get('record_needs_source'): return
        if time.monotonic()<state['retry_at']: return
        disk=self.maintenance.disk_status(settings)
        self.maintenance_status['disk']=disk
        if not disk['recording_allowed']:
            state['record_status']='queued'; state['last_error']=disk['error'] or '磁盘剩余空间低于保护阈值，已暂停录制；监控继续'
            state['retry_at']=time.monotonic()+5
            return
        active=sum(1 for r in self.store.list_rooms() if (self.recorder.status(r['id']) or {}).get('running'))
        if active>=settings['max_recordings']:
            state['record_status']='queued'; state['last_error']='等待录制名额（已达到最大并发数）'; return
        if not state['resolved_stream_url']:
            self._record_failure(room,state,'直播已开播，但尚未解析到可录制的直播源'); return
        source=state['resolved_stream_url']
        if any(selection):
            variants=state.get('stream_variants',[])
            chosen=next((item for item in variants if (not selection[0] or item.get('quality')==selection[0]) and (not selection[1] or item.get('protocol')==selection[1])),None)
            if not chosen:
                self._record_failure(room,state,'当前直播源未提供所选画质 / 格式，等待重新取源；可在直播源详情恢复自动选择');return
            source=chosen['url']
        # Capability discovery may wait three seconds. Reserve one warmup
        # worker; subsequent scheduler ticks start recording after it completes.
        if isinstance(self.recorder,Recorder):
            binary=self.recorder.executable(settings)
            try:
                from pathlib import Path
                info=Path(binary).stat()
                identity=(binary,info.st_size,info.st_mtime_ns)
            except OSError: identity=(binary,0,0)
            if self.ffmpeg_prepared!=identity:
                if not self.ffmpeg_prepare_thread or not self.ffmpeg_prepare_thread.is_alive():
                    def prepare():
                        try: self.recorder._http_reconnect_args(binary)
                        finally:
                            self.ffmpeg_prepared=identity
                            self.wake.set()
                    self.ffmpeg_prepare_thread=threading.Thread(target=prepare,name='ffmpeg-capabilities',daemon=True)
                    self.ffmpeg_prepare_thread.start()
                state['record_status']='starting'
                return
        try:
            self.recorder.start(room,source,state['record_headers'],settings)
            status=self.recorder.status(rid) or {}
            state.update(record_status='starting',duration_seconds=0,record_path=status.get('path',''),record_started=time.monotonic(),last_error='',record_task_id=status.get('task_id',''),_record_selection=selection)
            self.store.append_event('record_started',room,'启动后台录制，等待有效媒体写入')
        except (OSError,ValueError) as exc: self._record_failure(room,state,safe_message(exc))
    def _record_failure(self,room,state,message):
        message=safe_message(message,list(room.get('request_headers',{}).values())+[self.store.get_settings().get('douyin_cookie','')])
        failures=state.get('record_failure_count',0)+1
        delay=min(30,2**min(failures,9))
        denied=re.search(r'(?:HTTP(?:\s+error)?|Server returned|error)\s*(403|429|444)\b',message,re.I)
        if denied: delay=max(delay,60 if denied[1]=='403' else 120)
        retry_at=time.monotonic()+delay
        if state['record_status']!='failed' or state['last_error']!=message:
            self.store.append_event('record_failed',room,message)
        state.update(record_status='recovering' if failures<5 else 'failed',last_error=message,retry_at=retry_at,record_recheck_at=retry_at,record_failure_count=failures,record_needs_source=True,record_recovery_count=state.get('record_recovery_count',0)+1)
        self._save_record_state(room['id'],state,'recovering')
    def _save_record_state(self,rid,state,phase):
        if hasattr(self.store,'save_record_task'):
            self.store.save_record_task(rid,dict(task_id=state.get('record_task_id',''),state=phase,path=state.get('record_path',''),duration_seconds=state.get('duration_seconds',0),last_media_write_at=state.get('last_media_at',''),bytes_written=state.get('record_bytes_written',0),segment_count=state.get('record_segment_count',0)))
    def _record_metrics(self,state,status):
        for key,value in [('last_media_at',status.get('last_media_write_at')),('record_task_id',status.get('task_id'))]:
            if value: state[key]=value
        state['record_bytes_written']=status.get('bytes_written',state.get('record_bytes_written',0))
        state['record_segment_count']=status.get('segment_count',state.get('record_segment_count',0))
    def _inspect_record(self,room,state):
        if room['id'] in self.record_stop_tasks: return
        status=self.recorder.status(room['id'])
        if not status: return
        state['duration_seconds']=status.get('duration_seconds',0)
        state['record_path']=status.get('path','')
        self._record_metrics(state,status)
        if status['running']:
            disk=self.maintenance_status['disk']
            if not disk['recording_allowed']:
                self._queue_record_stop(room['id'],room,report_saved=True)
                state['last_error']=disk['error'] or '磁盘剩余空间低于保护阈值，已暂停录制；监控继续'
                state['retry_at']=time.monotonic()+5
                return
            if status.get('has_media') and state['record_status']!='recording':
                state['record_status']='recording'
                if state.get('record_recovery_count',0): self.store.append_event('record_recovered',room,'已重新取得直播源，新片段正在持续写入有效媒体')
            if status.get('has_media') and status.get('duration_seconds',0)>=30:
                state['record_failure_count']=0
            settings=self.store.get_settings()
            startup_timeout=settings.get('record_start_timeout_seconds',30)
            stall_timeout=settings.get('record_stall_seconds',30)
            if not status.get('has_media') and time.monotonic()-state.get('record_started',time.monotonic())>startup_timeout:
                self._queue_record_stop(room['id'],room)
                self._record_failure(room,state,f'录制进程 {startup_timeout:g} 秒未产生有效媒体，正在重新取源')
            elif status.get('has_media') and time.monotonic()-status.get('last_progress',time.monotonic())>stall_timeout:
                self._queue_record_stop(room['id'],room)
                self._record_failure(room,state,f'直播源 {stall_timeout:g} 秒无有效媒体写入，正在关闭连接并重新取源')
            return
        # A running broadcast can disconnect without the provider observing offline.
        self._queue_record_stop(room['id'],room)
        if not self.stop_event.is_set(): self._record_failure(room,state,'直播源连接结束，等待重新解析重试。'+status.get('error',''))
    def _stop_record(self,rid,room):
        if rid in self.states:
            state=self.states[rid]
            state.pop('record_recheck_at',None)
            state.pop('record_needs_source',None)
            if state['record_status'] in ('recovering','failed','queued','starting'):
                state['record_status']='stopped' if room and (not room.get('record_enabled') or not room.get('monitor_enabled')) else 'completed' if state.get('duration_seconds',0)>0 else 'idle'
                self._save_record_state(rid,state,state['record_status'])
        self._queue_record_stop(rid,room,report_saved=True)
    def _queue_record_stop(self,rid,room,report_saved=False):
        if rid in self.record_stop_tasks: return
        status=self.recorder.status(rid)
        if not status: return
        request_stop=getattr(self.recorder,'request_stop',None)
        if request_stop:
            future=request_stop(rid)
            if future:
                original=self.states.get(rid,{}).get('source_room') or room
                self.record_stop_tasks[rid]=(future,dict(original) if original else None,report_saved)
                if rid in self.states: self.states[rid]['record_status']='stopping'
                future.add_done_callback(lambda _:self.wake.set())
                self._drain_record_stops()
            return
        result=self.recorder.stop(rid) or status
        self._record_stopped(rid,room,result,report_saved)
    def _drain_record_stops(self):
        for rid,(future,room,report_saved) in list(self.record_stop_tasks.items()):
            if not future.done(): continue
            try: result=future.result()
            except Exception as exc:
                if rid not in self.record_stop_errors:
                    self.record_stop_errors.add(rid)
                    self.store.append_event('record_failed',room,'录制退出未完成：'+type(exc).__name__)
                    if rid in self.states: self.states[rid].update(record_status='failed',last_error='录制进程退出未完成，保留文件保护')
                attempts,next_at=self.record_stop_retry.get(rid,(0,time.monotonic()+2))
                self.record_stop_retry[rid]=(attempts,next_at)
                status=self.recorder.status(rid) or {}
                eligible_at=max(next_at,status.get('stop_retry_at',0))
                if attempts<2 and not status.get('stop_retry_exhausted') and time.monotonic()>=eligible_at and not self.stop_event.is_set():
                    retried=self.recorder.request_stop(rid)
                    if retried and retried is not future:
                        self.record_stop_retry[rid]=(attempts+1,time.monotonic()+2)
                        self.record_stop_tasks[rid]=(retried,room,report_saved)
                continue
            self.record_stop_tasks.pop(rid,None); self.record_stop_errors.discard(rid)
            self.record_stop_retry.pop(rid,None)
            if result: self._record_stopped(rid,room,result,report_saved)
    def _record_stopped(self,rid,room,result,report_saved):
        state=self.states.get(rid)
        same_task=not state or not state.get('record_task_id') or not result.get('task_id') or state['record_task_id']==result['task_id']
        if state and same_task and (not room or state.get('source_identity')==self._identity(room)):
            state['duration_seconds']=result.get('duration_seconds',0)
            state['record_path']=result.get('path','')
            self._record_metrics(state,result)
            if state['record_status'] not in ('failed','recovering'):
                try: current=self.store.get_room(rid)
                except ValueError: current=room or {}
                state['record_status']='stopped' if not current.get('record_enabled') or not current.get('monitor_enabled') else 'completed' if result.get('has_media') else 'idle'
            self._save_record_state(rid,state,'recovering' if state.get('record_needs_source') else state['record_status'])
            self._persist(rid,state)
        if report_saved and room and result.get('has_media'): self.store.append_event('record_stopped',room,'录制已保存')
    def room_changed(self,room_id=None):
        with self.lock:
            rechecks=[]
            rooms=self.store.list_rooms() if room_id is None else []
            if room_id:
                try: rooms=[self.store.get_room(room_id)]
                except ValueError:
                    self._stop_record(room_id,None); self.states.pop(room_id,None); self.due.pop(room_id,None); self.status_last_started.pop(room_id,None); self.status_last_finished.pop(room_id,None)
                    self._cancel_recovery(room_id)
            for room in rooms:
                is_new=room['id'] not in self.states
                state=self._state(room)
                was_disabled=state['live_status']=='disabled'
                identity=self._identity(room)
                changed=state['source_identity']!=identity
                if changed:
                    self._cancel_recovery(room['id'])
                    self._stop_record(room['id'],room)
                    self.store.close_session(state.get('source_room',room),now_iso(),reason='configuration_changed')
                    state.update(source_identity=identity,source_room=dict(room),resolved_stream_url='',stream_variants=[],resolved_room_input='',record_headers={},confirmed_live=None,live_status='checking',live_started_at='',observed_started_at='',started_at_source='',live_session_id='',avatar_url='',live_ended_at='',platform_ended_at='',offline_checks=0,status_note='',last_error='',error_code='',error_diagnostic='',failure_count=0,retry_at=0,duration_seconds=0,record_path='',record_status='idle')
                    state.update(last_success_at='',last_attempt_at='',last_probe_latency_seconds=None,_last_success_monotonic=None)
                    state.update(detection_uncertain=True,last_check_finished_at='',last_actual_check_interval_seconds=None)
                    for key in ('_offline_recheck_at','_error_recheck_at','_last_attempt_monotonic'): state.pop(key,None)
                    self.due[room['id']]=0
                    self._persist(room['id'],state)
                if not room['monitor_enabled']:
                    self._cancel_recovery(room['id'])
                    if room['id'] in self.pending: self.discarded_probes.add(self.pending[room['id']][0])
                    state['live_status']='disabled'; self._stop_record(room['id'],room)
                elif state['live_status']=='disabled': state['live_status']='checking'
                if not room['record_enabled']: self._stop_record(room['id'],room)
                elif room['monitor_enabled'] and state['confirmed_live'] is True and state['resolved_stream_url']:
                    self._start_record(room,state)
                if room['monitor_enabled'] and (is_new or changed or was_disabled):
                    if changed or was_disabled or room['id'] not in self.pending: rechecks.append(room['id'])
                elif not room['monitor_enabled']:
                    self.round_queue=[rid for rid in self.round_queue if rid!=room['id']]
            self._enqueue_checks(rechecks,include_pending=True)
            self.wake.set()
    def settings_changed(self):
        # Deadlines derive from the latest values, so edits affect an existing
        # pause or round wait without clearing restrictions or duplicating work.
        configure=getattr(self.pool,'configure',None)
        if configure: configure(self.store.get_settings())
        self.wake.set()
        self.disk_checked_at=0
        self.maintenance_wake.set()
    def auth_changed(self):
        with self.lock:
            self.auth_generation+=1
            self.douyin_blocked_until=0; self.douyin_recovery_rid=None
            self.douyin_rejections.clear(); self.douyin_network_failures.clear()
            self.douyin_recovery_successes=0
            reset=getattr(self.providers,'invalidate_auth',None)
            if reset: reset(self.store.get_settings())
            for rid,state in self.states.items():
                state['failure_count']=0; state['error_code']=''; state['error_diagnostic']=''
                self.due[rid]=0
                if rid in self.recoveries: state['status_note']=''
            self.recoveries.clear()
            self._enqueue_checks([r['id'] for r in self.store.list_rooms() if r['monitor_enabled']],include_pending=True)
            self.wake.set()

    def refresh(self,ids=None):
        with self.lock:
            wanted=set(ids) if ids is not None else {r['id'] for r in self.store.list_rooms()}
            rooms=[self.store.get_room(rid) for rid in wanted]
            self._enqueue_checks([r['id'] for r in sorted(rooms,key=lambda r:r['sort_order']) if r['monitor_enabled']])
            self.wake.set()

    def request_sources(self,room_id):
        """Queue one full source resolution within the normal probe budget."""
        with self.lock:
            room=self.store.get_room(room_id)
            if not room['monitor_enabled']:
                raise ValueError('请先开启该房间监控，再刷新可用直播源')
            state=self._state(room)
            if not state.get('source_refreshing'):
                state['_source_refresh_requested']=True
                self._enqueue_checks([room_id],include_pending=True)
            self.wake.set()
            return {'queued':True,'room_id':room_id}
    def bulk(self,action,ids=None):
        if action not in ['monitor_on','monitor_off','record_on','record_off','record_live']: raise ValueError('未知批量操作')
        with self.lock:
            rooms=self.store.list_rooms()
            wanted=set(ids) if ids is not None else {r['id'] for r in rooms}
            if wanted-{r['id'] for r in rooms}: raise ValueError('部分直播间不存在，请刷新列表')
            count=0
            for room in rooms:
                if room['id'] not in wanted: continue
                if action=='record_live' and self._state(room)['live_status']!='live': continue
                payload={'monitor_enabled':action=='monitor_on'} if action.startswith('monitor_') else {'record_enabled':action!='record_off'}
                if action=='record_live': payload['monitor_enabled']=True
                self.store.update_room(room['id'],payload); count+=1
            self.room_changed()
            return {'updated':count}
    def snapshot(self):
        with self.lock:
            settings=self.store.get_settings()
            now=time.monotonic()
            status_mode=bool(settings.get('status_only_mode',True))
            status_interval=settings.get('status_poll_interval_seconds',8) if status_mode else settings['monitor_interval_minutes']*60
            next_round=self._next_round_at(settings)
            next_room=self._next_dispatch_at(settings)
            rooms=[]
            for room in self.store.list_rooms():
                state=self._state(room)
                item=dict(room)
                item.update({k:v for k,v in state.items() if not k.startswith('_') and k not in ['confirmed_live','retry_at','record_headers','record_started','source_identity','source_room','record_recheck_at','record_failure_count','record_needs_source']})
                success=state.get('_last_success_monotonic')
                age=None if success is None else max(0,now-success)
                item['status_age_seconds']=None if age is None else round(age,2)
                # One fixed delay plus the request budget is a normal cycle;
                # freshness must not flicker expired while the next request runs.
                freshness_budget=status_interval+self._probe_timeout(settings,room)
                item['status_stale']=bool(room['monitor_enabled'] and (age is None or age>freshness_budget or state.get('detection_uncertain') or room['id'] in self.recoveries))
                item['last_confirmed_status']='live' if state['confirmed_live'] is True else 'waiting' if state['confirmed_live'] is False else 'unknown'
                item['room_status']=item['last_confirmed_status'] if room['monitor_enabled'] else 'disabled'
                item['pending_offline_confirmation']=bool(state.get('offline_checks'))
                item['source_refresh_pending']=bool(state.get('_source_refresh_requested') or state.get('source_refreshing'))
                if item['status_stale']: item['live_status']='unknown'
                item['has_request_headers']=bool(item.pop('request_headers',{}))
                rescue=self.recoveries.get(room['id'])
                item['recovering']=bool(rescue)
                item['recovery_seconds_remaining']=max(0,round(rescue['deadline']-now)) if rescue else 0
                item['checking']=room['id'] in self.pending or bool(rescue)
                earliest=(self._status_due_at(room['id'],settings) if status_mode else
                          max(self.due.get(room['id'],0),next_room,0 if self.round_active else next_round))
                if self._adaptive_room(room): earliest=max(earliest,self.douyin_blocked_until)
                item['next_check_seconds']=max(0,round(earliest-now)) if room['monitor_enabled'] else 0
                rooms.append(item)
            groups={'live':0,'waiting':1,'checking':1,'disabled':2,'error':3,'unknown':3}
            rooms.sort(key=lambda r:(groups.get(r['room_status'],3),r.get('sort_order',0)))
            authorized=cookie_is_authorized(settings.get('douyin_cookie',''))
            settings['has_douyin_cookie']=bool(settings.pop('douyin_cookie',''))
            stats={'total':len(rooms),'monitoring':sum(r['monitor_enabled'] for r in rooms),'live':sum(r['live_status']=='live' for r in rooms),'recording':sum(r['record_status']=='recording' for r in rooms),'errors':sum(r['live_status'] in ('error','unknown') or r['record_status']=='failed' for r in rooms)}
            phase='checking' if self.pending or self.recoveries else 'paused' if self.round_active and now<next_room else 'checking' if self.round_active else 'waiting' if stats['monitoring'] else 'idle'
            fresh=sum(room['monitor_enabled'] and not room['status_stale'] for room in rooms)
            stale=stats['monitoring']-fresh
            latencies=sorted(self.request_latencies)
            latency_p95=latencies[max(0,math.ceil(len(latencies)*.95)-1)] if latencies else None
            intervals=sorted(self.check_intervals)
            interval_p95=intervals[max(0,math.ceil(len(intervals)*.95)-1)] if intervals else None
            measurement_window=min(30,max(.001,now-self.started_at))
            observed_rate=sum(at>=now-measurement_window for at in self.probe_started_times)/measurement_window
            if status_mode: phase='checking' if self.pending else 'waiting' if stats['monitoring'] else 'idle'
            monitor={'round_interval_seconds':status_interval,
                     'room_check_delay_seconds':0 if status_mode else settings['room_check_delay_seconds'],
                     'check_interval_seconds':status_interval,
                     'max_concurrent_checks':self._effective_concurrency(settings),
                     'monitor_batch_size':settings.get('monitor_batch_size',3),
                     'effective_concurrency':self._effective_concurrency(settings),
                     'status_only_mode':bool(settings.get('status_only_mode',True)),
                     'target_status_interval_seconds':settings.get('status_poll_interval_seconds',8),
                     'status_request_timeout_seconds':settings.get('status_request_timeout',4),
                     'last_round_duration_seconds':self.last_round_duration_seconds,
                     'probe_successes':self.probe_successes,
                     'probe_failures':self.probe_failures,
                     'probe_timeouts':self.probe_timeouts,
                     'fresh_rooms':fresh,'stale_rooms':stale,
                     'coverage_ratio':round(fresh/stats['monitoring'],4) if stats['monitoring'] else 1,
                     'request_latency_p95_seconds':round(latency_p95,3) if latency_p95 is not None else None,
                     'configured_requests_per_second':round(stats['monitoring']/status_interval,2),
                     'observed_requests_per_second':round(observed_rate,2),
                     'request_rate_limit_per_second':settings.get('monitor_max_requests_per_second',20),
                     'actual_check_interval_p95_seconds':round(interval_p95,3) if interval_p95 is not None else None,
                     'schedule_timing':'fixed_delay' if status_mode else 'round_delay',
                     'capacity_required_concurrency':math.ceil(stats['monitoring']/status_interval*latency_p95) if latency_p95 is not None else None,
                     'schedule_mode':'independent' if status_mode else settings.get('monitor_schedule_mode','independent'),
                     'process_isolation':settings.get('monitor_process_isolation',True),
                     'douyin_concurrency':min(self._effective_concurrency(settings),settings.get('status_douyin_concurrency',32) if status_mode else settings.get('monitor_douyin_concurrency',2)),
                     'active_checks':len(self.pending),'recovering_checks':len(self.recoveries),
                     'phase':phase,'round_remaining':len(set(self.round_queue)|set(self.pending)|set(self.recoveries)),
                     'round_number':self.round_number,
                     'next_round_seconds':0 if status_mode else max(0,round(max(next_round,next_room)-now)) if not self.round_active and stats['monitoring'] else 0}
            if hasattr(self.pool,'stats'): monitor.update(self.pool.stats())
            return dict(auth={'douyin_authorized':authorized},maintenance=dict(self.maintenance_status),monitor=monitor,rooms=rooms,settings=settings,events=self.store.list_events(),stats=stats,ffmpeg_available=self.recorder.available(self.store.get_settings()),poll_interval=status_interval,native_sound_available=os.name=='nt')
    def close(self):
        if self.closed: return
        self.closed=True
        self.stop_event.set(); self.wake.set(); self.maintenance_wake.set()
        with self.lock: self.recoveries.clear()
        if self.thread: self.thread.join()
        if self.maintenance_thread: self.maintenance_thread.join()
        if self.ffmpeg_prepare_thread: self.ffmpeg_prepare_thread.join()
        self.pool.shutdown(wait=True,cancel_futures=True)
        close_errors=self.recorder.close()
        with self.lock:
            self._drain_record_stops()
            if close_errors:
                self.store.append_event('record_failed',None,'退出时有 '+str(len(close_errors))+' 个录制任务未能确认完成退出，保留文件保护；请检查日志')
            for rid,state in self.states.items(): self._persist(rid,state)
