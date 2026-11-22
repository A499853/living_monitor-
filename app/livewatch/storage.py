from __future__ import annotations
import csv, json, logging, math, re, sqlite3, threading, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from .avatar import validate_avatar_url, AvatarUnavailable
from .media_options import QUALITY_PREFERENCES, validate_record_directory

def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')

def room_directory(room):
    value = room.get('room_input', '')
    if '://' in value:
        value = urlsplit(value).path.strip('/').split('/')[-1] or 'room'
    value = re.sub(r'[^\w.-]', '_', value).strip('. ')[:64] or 'room'
    return f"{room['platform']}_{value}_{room['id'][:8]}"

class Store:
    FIELDS = {'platform','room_input','nickname','monitor_enabled','record_enabled','remark','stream_url','status_url','request_headers','input_type','transfer_options','record_quality','record_protocol','record_directory'}
    # poll_interval is retained only to read older settings; monitoring uses rounds.
    # The high-frequency status profile is intentionally separate from the
    # recording/metadata cadence. Eight seconds after each completed check is
    # the default delay. A bounded 32-worker pool and global dispatch budget
    # protect large fleets; real request and queue latency still apply.
    DEFAULTS = {'poll_interval':8,'monitor_interval_minutes':0.5,'monitor_batch_size':3,'monitor_schedule_mode':'independent','monitor_process_isolation':True,'monitor_douyin_concurrency':2,'room_check_delay_seconds':1,'status_only_mode':True,'status_poll_interval_seconds':8,'status_check_concurrency':32,'status_douyin_concurrency':32,'status_request_timeout':4,'monitor_max_requests_per_second':20,'sound_enabled':True,'ffmpeg_path':'','max_recordings':4,'douyin_cookie':'','douyin_user_agent':'','request_timeout':12,'recording_retention_days':0,'log_retention_days':0,'min_free_disk_gib':2,'record_segment_seconds':1800,'record_stall_seconds':30,'record_start_timeout_seconds':30,'douyin_access_mode':'anonymous','monitor_identity_isolation':True}
    DEFAULTS.update(status_douyin_concurrency=4, monitor_douyin_requests_per_second=2,
                    copy_source_quality='below_origin_1', copy_source_protocol='flv',
                    playback_quality='below_origin_2', playback_protocol='hls', recording_directory='')
    def __init__(self, root):
        self.root = Path(root).resolve()
        for name in ['data','logs','video','time_begin_over']:
            (self.root/name).mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root/'data'/'livewatch.db', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS runtime(room_id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS recording_tasks(room_id TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,room_id TEXT,nickname TEXT,message TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,room_id TEXT,started_at TEXT,ended_at TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS owned_files(path TEXT PRIMARY KEY,kind TEXT NOT NULL,active INTEGER NOT NULL,registered_at REAL NOT NULL,finished_at REAL,process_id INTEGER,device TEXT,inode TEXT,size INTEGER,modified_ns TEXT);
        CREATE INDEX IF NOT EXISTS owned_files_active ON owned_files(active) WHERE active=1;
        CREATE INDEX IF NOT EXISTS sessions_open_room ON sessions(room_id,started_at DESC) WHERE ended_at='';''')
        # v1's started_at is a local observation. Preserve it as such; never
        # promote it to the platform's real broadcast start during migration.
        columns={row[1] for row in self.db.execute('PRAGMA table_info(sessions)')}
        for name in ('actual_started_at','platform_session_id','started_at_source','actual_ended_at','ended_at_source'):
            if name not in columns:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS sessions_pending_end ON sessions(room_id,ended_at DESC) WHERE ended_at<>'' AND actual_ended_at=''")
        room_columns={row[1] for row in self.db.execute('PRAGMA table_info(rooms)')}
        if 'sort_order' not in room_columns:
            self.db.execute('ALTER TABLE rooms ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0')
            rows=self.db.execute('SELECT id FROM rooms ORDER BY created_at,id').fetchall()
            self.db.executemany('UPDATE rooms SET sort_order=? WHERE id=?',[(index,row[0]) for index,row in enumerate(rows)])
        self.db.commit()
        self._runtime_cache={}
        self._runtime_cache_version=None
        self._runtime_cache_changes=self.db.total_changes
    def _validate(self, payload, previous=None):
        if not isinstance(payload,dict) or set(payload)-self.FIELDS-{'id','created_at','sort_order'}:
            raise ValueError('无效的直播间字段')
        room = dict(previous or dict(platform='douyin',room_input='',nickname='',monitor_enabled=True,record_enabled=False,remark='',stream_url='',status_url='',request_headers={},input_type='auto',transfer_options={}))
        room.setdefault('input_type','auto')
        room.setdefault('transfer_options',{})
        room.setdefault('record_quality','')
        room.setdefault('record_protocol','')
        room.setdefault('record_directory','')
        room.update({k:v for k,v in payload.items() if k in self.FIELDS})
        room['record_directory']=validate_record_directory(room['record_directory'])
        for key in ('record_quality','record_protocol'):
            if not isinstance(room[key],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{0,64}',room[key]):
                raise ValueError('录制画质或格式无效')
        for key,limit in [('platform',20),('room_input',2048),('nickname',160),('remark',2000),('stream_url',8192),('status_url',8192)]:
            if not isinstance(room[key],str) or len(room[key])>limit or '\x00' in room[key]:
                raise ValueError(f'{key} 内容无效或过长')
            room[key]=room[key].strip()
        if room['platform'] not in ['douyin','wechat'] or not room['room_input']:
            raise ValueError('请选择平台并输入直播间 ID 或链接')
        if room['platform']=='douyin' and not (room['stream_url'] or room['status_url']):
            from .inputs import normalize_douyin_text
            from .providers import _douyin_input
            room['room_input']=normalize_douyin_text(room['room_input'])
            _douyin_input(room['room_input'],room['input_type'])
        for key in ['monitor_enabled','record_enabled']:
            if not isinstance(room[key],bool): raise ValueError(f'{key} 必须为布尔值')
        for key in ['stream_url','status_url']:
            if room[key] and (urlsplit(room[key]).scheme not in ['http','https'] or not urlsplit(room[key]).hostname): raise ValueError(f'{key} 必须为 HTTP(S) 地址')
        if room['input_type'] not in ('auto','room','account'): raise ValueError('输入类型必须为 auto、room 或 account')
        options=room['transfer_options']
        if not isinstance(options,dict) or len(json.dumps(options,ensure_ascii=False))>32768: raise ValueError('兼容配置内容无效或过长')
        headers=room['request_headers']
        if not isinstance(headers,dict) or len(headers)>20: raise ValueError('请求头必须为对象，最多 20 项')
        for k,v in headers.items():
            if not isinstance(k,str) or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+",k) or not isinstance(v,str) or len(v)>32768 or any(c in v for c in ['\r','\n','\x00']): raise ValueError('请求头包含无效内容')
        return room
    @staticmethod
    def room_key(room):
        value=room['room_input'].strip()
        input_type=room.get('input_type','auto')
        if room['platform']=='douyin':
            from .providers import _douyin_input
            try:
                kind,identifier=_douyin_input(value,input_type)
                if kind in ('room','profile','account'): return ('douyin',kind,identifier)
                if kind=='page': return ('douyin','page',identifier)
            except ValueError:
                pass  # Custom sources may use a display identifier of their own.
        if value.startswith(('http://','https://')):
            parsed=urlsplit(value)
            value=urlunsplit((parsed.scheme.lower(),parsed.netloc.lower(),parsed.path.rstrip('/'),parsed.query,''))
        return (room['platform'],'account' if input_type=='account' else 'auto',value)
    def _list_rooms_locked(self):
        rows=self.db.execute('SELECT payload,sort_order FROM rooms ORDER BY sort_order,created_at DESC,id')
        rooms=[]
        for row in rows:
            room=json.loads(row[0]);room.setdefault('input_type','auto');room.setdefault('transfer_options',{})
            room['sort_order']=row[1];rooms.append(room)
        return rooms
    def add_room(self,payload):
        room=self._validate(payload)
        room.update(id=uuid.uuid4().hex,created_at=now_iso())
        with self.lock,self.db:
            if any(self.room_key(r)==self.room_key(room) for r in self._list_rooms_locked()): raise ValueError('该直播间已存在')
            rank=self.db.execute('SELECT COALESCE(MIN(sort_order),0)-1 FROM rooms').fetchone()[0]
            self.db.execute('INSERT INTO rooms(id,payload,created_at,sort_order) VALUES(?,?,?,?)',(room['id'],json.dumps(room,ensure_ascii=False),room['created_at'],rank))
            room['sort_order']=rank
        return room
    def add_rooms(self,payloads):
        if not isinstance(payloads,list) or len(payloads)>1000: raise ValueError('一次最多导入 1000 个直播间')
        with self.lock,self.db:
            known={self.room_key(room):room['id'] for room in self._list_rooms_locked()}
            results=[];prepared=[]
            for index,payload in enumerate(payloads,1):
                try:
                    if isinstance(payload,dict) and '_import_error' in payload: raise ValueError(str(payload['_import_error']))
                    payload=dict(payload)
                    avatar=payload.pop('_avatar_url','')
                    try: avatar=validate_avatar_url(avatar) if avatar else ''
                    except AvatarUnavailable: avatar=''
                    room=self._validate(payload)
                    key=self.room_key(room)
                    if key in known:
                        results.append({'index':index,'status':'duplicate','id':known[key],'message':'该直播间已存在，保留原配置'})
                        continue
                    room.update(id=uuid.uuid4().hex,created_at=now_iso())
                    known[key]=room['id'];prepared.append((room,avatar))
                    results.append({'index':index,'status':'added','id':room['id'],'message':'已添加'})
                except (ValueError,TypeError) as exc:
                    results.append({'index':index,'status':'failed','message':str(exc)})
            first=self.db.execute('SELECT COALESCE(MIN(sort_order),0) FROM rooms').fetchone()[0]-len(prepared)
            for offset,(room,avatar) in enumerate(prepared):
                if avatar: self.db.execute('INSERT OR REPLACE INTO runtime(room_id,payload) VALUES(?,?)',(room['id'],json.dumps({'avatar_url':avatar},ensure_ascii=False)))
                self.db.execute('INSERT INTO rooms(id,payload,created_at,sort_order) VALUES(?,?,?,?)',(room['id'],json.dumps(room,ensure_ascii=False),room['created_at'],first+offset))
        return {'added':len(prepared),'duplicates':sum(item['status']=='duplicate' for item in results),'failed':sum(item['status']=='failed' for item in results),'results':results}
    def update_room(self,room_id,payload):
        with self.lock,self.db:
            room=self._validate(payload,self.get_room(room_id))
            if any(r['id']!=room_id and self.room_key(r)==self.room_key(room) for r in self._list_rooms_locked()): raise ValueError('该直播间已存在')
            self.db.execute('UPDATE rooms SET payload=? WHERE id=?',(json.dumps(room,ensure_ascii=False),room_id))
            return room
    def get_room(self,room_id):
        with self.lock:
            row=self.db.execute('SELECT payload,sort_order FROM rooms WHERE id=?',(room_id,)).fetchone()
            if row is None: raise ValueError('直播间不存在')
            room=json.loads(row[0]);room.setdefault('input_type','auto');room.setdefault('transfer_options',{});room['sort_order']=row[1]
            return room
    def list_rooms(self):
        with self.lock: return self._list_rooms_locked()
    def reorder_rooms(self,ids):
        if not isinstance(ids,list) or not ids or len(ids)>1000 or any(not isinstance(value,str) for value in ids) or len(set(ids))!=len(ids): raise ValueError('排序列表无效')
        with self.lock,self.db:
            rooms=self._list_rooms_locked();all_ids=[room['id'] for room in rooms]
            if set(ids)-set(all_ids): raise ValueError('排序中包含不存在的直播间')
            positions=[index for index,room in enumerate(rooms) if room['id'] in set(ids)]
            for index,room_id in zip(positions,ids): all_ids[index]=room_id
            self.db.executemany('UPDATE rooms SET sort_order=? WHERE id=?',[(index,room_id) for index,room_id in enumerate(all_ids)])
            return self._list_rooms_locked()
    def promote_room(self,room_id):
        with self.lock,self.db:
            self.get_room(room_id)
            rank=self.db.execute('SELECT COALESCE(MIN(sort_order),0)-1 FROM rooms').fetchone()[0]
            self.db.execute('UPDATE rooms SET sort_order=? WHERE id=?',(rank,room_id))
            return rank
    def delete_room(self,room_id):
        with self.lock,self.db:
            self.get_room(room_id)
            self.db.execute('DELETE FROM rooms WHERE id=?',(room_id,))
            self.db.execute('DELETE FROM runtime WHERE room_id=?',(room_id,))
            self.db.execute('DELETE FROM recording_tasks WHERE room_id=?',(room_id,))
    def get_settings(self):
        with self.lock:
            result=dict(self.DEFAULTS)
            result.update({r[0]:json.loads(r[1]) for r in self.db.execute('SELECT key,value FROM settings')})
            timeout=result.get('status_request_timeout',4)
            result['status_request_timeout']=max(2,min(4,timeout)) if type(timeout) in (int,float) and math.isfinite(timeout) else 4
            return result
    def update_settings(self,payload):
        if not isinstance(payload,dict) or set(payload)-set(self.DEFAULTS): raise ValueError('无效的设置字段')
        value=dict(payload)
        for key in ('copy_source_quality','playback_quality'):
            if key in value and value[key] not in QUALITY_PREFERENCES:
                raise ValueError('默认直播源画质无效')
        for key in ('copy_source_protocol','playback_protocol'):
            if key in value and value[key] not in ('flv','hls'):
                raise ValueError('默认直播源格式必须为 FLV 或 HLS')
        if 'recording_directory' in value:
            value['recording_directory']=validate_record_directory(value['recording_directory'])
        if 'monitor_douyin_requests_per_second' in value:
            number=value['monitor_douyin_requests_per_second']
            if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not 0.2<=number<=20:
                raise ValueError('抖音每秒检查上限必须为 0.2～20 之间的数值')
        for key,lo,hi,label in [('monitor_interval_minutes',0.1,1440,'整轮检查间隔（分钟）'),('room_check_delay_seconds',0,60,'派发间隔 / 批次停顿（秒）'),('min_free_disk_gib',0,1048576,'磁盘最小剩余空间（GiB）')]:
            if key in value:
                number=value[key]
                if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not lo<=number<=hi:
                    raise ValueError(f'{label}必须为 {lo}～{hi} 之间的数值')
        for key,maximum,label in [('monitor_batch_size',16,'检查并发上限'),('monitor_douyin_concurrency',5,'抖音官方检查并发上限')]:
            if key in value:
                count=value[key]
                if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=maximum:
                    raise ValueError(f'{label}必须为 1～{maximum} 之间的整数')
        for key,lo,hi,label in [('status_poll_interval_seconds',1,60,'高频状态检查间隔'),('status_request_timeout',2,4,'高频状态请求超时'),('monitor_max_requests_per_second',1,100,'全局每秒检查上限'),('record_segment_seconds',60,86400,'录制分段时长'),('record_stall_seconds',10,300,'录制停滞期限'),('record_start_timeout_seconds',10,120,'录制启动期限')]:
            if key in value:
                number=value[key]
                if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not lo<=number<=hi:
                    raise ValueError(f'{label}必须为 {lo}～{hi} 之间的数值')
        for key,label in [('status_check_concurrency','高频状态检查并发上限'),('status_douyin_concurrency','高频抖音检查并发上限')]:
            if key in value:
                count=value[key]
                if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=64:
                    raise ValueError(label+'必须为 1～64 之间的整数')
        if 'status_only_mode' in value and not isinstance(value['status_only_mode'],bool):
            raise ValueError('高频状态模式必须为布尔值')
        if 'monitor_schedule_mode' in value and value['monitor_schedule_mode'] not in ('independent','batch'):
            raise ValueError('监控调度方式必须为 independent 或 batch')
        if 'monitor_process_isolation' in value and not isinstance(value['monitor_process_isolation'],bool):
            raise ValueError('监控进程隔离设置必须为布尔值')
        if 'monitor_identity_isolation' in value and not isinstance(value['monitor_identity_isolation'],bool):
            raise ValueError('手机观看身份隔离设置必须为布尔值')
        for key,label in [('recording_retention_days','录制保留天数'),('log_retention_days','日志保留天数')]:
            if key in value and (isinstance(value[key],bool) or not isinstance(value[key],int) or not 0<=value[key]<=3650):
                raise ValueError(label+'必须为 0～3650 之间的整数（0 表示关闭自动清理）')
        if 'douyin_access_mode' in value and value['douyin_access_mode'] not in ('anonymous','authorized'):
            raise ValueError('抖音监控模式必须为匿名监控或授权监控')
        for key,lo,hi in [('poll_interval',5,10),('max_recordings',1,32),('request_timeout',5,30)]:
            if key in value:
                if isinstance(value[key],bool) or not isinstance(value[key],int): raise ValueError(f'{key} 必须为整数')
                value[key]=max(lo,min(hi,value[key]))
        if 'sound_enabled' in value and not isinstance(value['sound_enabled'],bool): raise ValueError('提示音设置必须为布尔值')
        for key,limit in [('ffmpeg_path',4096),('douyin_cookie',32768),('douyin_user_agent',4096)]:
            if key in value and (not isinstance(value[key],str) or len(value[key])>limit or any(c in value[key] for c in ['\r','\n','\x00'])): raise ValueError(f'{key} 内容无效')
        with self.lock,self.db:
            for key,v in value.items(): self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(v,ensure_ascii=False)))
        return self.get_settings()
    def save_runtime(self,room_id,payload):
        serialized=json.dumps(payload,ensure_ascii=False,sort_keys=True)
        with self.lock:
            version=self.db.execute('PRAGMA data_version').fetchone()[0]
            # data_version changes after other connections commit. total_changes
            # also fences writes made directly on this connection (migrations,
            # imports, and tests), so a cached state cannot overwrite their work.
            if version!=self._runtime_cache_version or self.db.total_changes!=self._runtime_cache_changes:
                self._runtime_cache.clear()
            if self._runtime_cache.get(room_id)==serialized: return False
            row=self.db.execute('SELECT payload FROM runtime WHERE room_id=?',(room_id,)).fetchone()
            previous=None
            if row:
                try: previous=json.dumps(json.loads(row[0]),ensure_ascii=False,sort_keys=True)
                except (ValueError,TypeError): pass
            changed=previous!=serialized
            if changed:
                with self.db: self.db.execute('INSERT OR REPLACE INTO runtime VALUES(?,?)',(room_id,serialized))
            if len(self._runtime_cache)>=4096: self._runtime_cache.clear()
            self._runtime_cache[room_id]=serialized
            self._runtime_cache_version=version
            self._runtime_cache_changes=self.db.total_changes
            return changed
    def get_runtime(self,room_id):
        with self.lock:
            row=self.db.execute('SELECT payload FROM runtime WHERE room_id=?',(room_id,)).fetchone()
            return json.loads(row[0]) if row else {}
    def save_record_task(self,room_id,payload):
        """One durable current task per room; never store signed URLs or headers."""
        keys=('task_id','state','path','duration_seconds','last_media_write_at','bytes_written','segment_count','process_id','reason')
        value={key:payload[key] for key in keys if key in payload}
        with self.lock,self.db:
            # A finishing worker must not recreate a task for a deleted room.
            if not self.db.execute('SELECT 1 FROM rooms WHERE id=?',(room_id,)).fetchone(): return
            self.db.execute('INSERT OR REPLACE INTO recording_tasks VALUES(?,?,?)',(room_id,json.dumps(value,ensure_ascii=False),now_iso()))
    def get_record_task(self,room_id):
        with self.lock:
            row=self.db.execute('SELECT payload FROM recording_tasks WHERE room_id=?',(room_id,)).fetchone()
            return json.loads(row[0]) if row else {}
    def append_event(self,kind,room,message):
        with self.lock,self.db:
            cursor=self.db.execute('INSERT INTO events(kind,room_id,nickname,message,created_at) VALUES(?,?,?,?,?)',(kind,(room or {}).get('id',''),(room or {}).get('nickname',''),str(message)[:2000],now_iso()))
            if cursor.lastrowid%100==0: self.db.execute('DELETE FROM events WHERE id<?',(cursor.lastrowid-10000,))
            logging.getLogger('livewatch').log(logging.WARNING if 'failed' in kind or kind=='error' else logging.INFO, '%s room=%s %s',kind,(room or {}).get('id',''),str(message).replace('\n',' ')[:2000])
            return cursor.lastrowid
    def list_events(self,limit=100):
        with self.lock: return [dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?',(max(1,min(int(limit),1000)),))]
    def get_open_session(self,room_id):
        with self.lock:
            row=self.db.execute("SELECT * FROM sessions WHERE room_id=? AND ended_at='' ORDER BY started_at DESC LIMIT 1",(room_id,)).fetchone()
            return dict(row) if row else None
    def discover_ended_session(self,room,platform_session_id,actual_started_at='',actual_ended_at='',observed_at=''):
        """Remember a broadcast found offline at the first status observation.

        The application did not observe its start, so started_at stays empty.
        A matching open session belongs to normal status confirmation and must
        never be closed by a delayed history response.
        """
        if (room.get('platform')!='douyin' or not isinstance(platform_session_id,str)
                or not re.fullmatch(r'[0-9]{1,32}',platform_session_id)):
            return ''
        observed_at=observed_at or now_iso()
        def valid_time(value,optional=False):
            if optional and value=='': return None
            if not isinstance(value,str): raise ValueError('Invalid time')
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if (parsed.tzinfo is None or parsed<datetime(2000,1,1,tzinfo=timezone.utc)
                    or parsed>datetime.now(timezone.utc)+timedelta(minutes=5)):
                raise ValueError('Invalid time')
            return parsed
        try:
            started=valid_time(actual_started_at,True)
            ended=valid_time(actual_ended_at,True)
            valid_time(observed_at)
            if started is not None and ended is not None and ended<=started: return ''
        except (ValueError,TypeError): return ''
        with self.lock:
            if not self.db.execute('SELECT 1 FROM rooms WHERE id=?',(room['id'],)).fetchone(): return ''
            row=self.db.execute("""SELECT * FROM sessions WHERE room_id=? AND platform_session_id=?
                ORDER BY (ended_at='') DESC,started_at DESC,id DESC LIMIT 1""",(room['id'],platform_session_id)).fetchone()
            if row:
                sid=row['id']
                if not row['ended_at']: return sid
                if actual_started_at and not row['actual_started_at']:
                    if row['actual_ended_at'] and started>=valid_time(row['actual_ended_at']): return ''
                    with self.db:
                        self.db.execute('UPDATE sessions SET actual_started_at=?,started_at_source=? WHERE id=?',
                                        (actual_started_at,'douyin.room.start_time',sid))
                    self._csv(room,sid,'metadata_updated',observed_at,actual_started_at,row['started_at'],row['ended_at'],
                              platform_session_id,'douyin.room.start_time',row['ended_at_source'],row['actual_ended_at'])
            else:
                sid=uuid.uuid4().hex
                source='douyin.room.start_time' if actual_started_at else ''
                with self.db:
                    self.db.execute("""INSERT INTO sessions(id,room_id,started_at,ended_at,actual_started_at,
                        platform_session_id,started_at_source) VALUES(?,?,?,?,?,?,?)""",
                        (sid,room['id'],'',observed_at,actual_started_at,platform_session_id,source))
                self._csv(room,sid,'discovered_offline',observed_at,actual_started_at,'',observed_at,
                          platform_session_id,source,'observed_offline')
            if actual_ended_at: self.update_session_end(room,sid,platform_session_id,actual_ended_at)
            return sid
    def get_pending_end_sessions(self,room_id):
        """Closed broadcasts still missing the platform's actual finish time."""
        with self.lock:
            rows=self.db.execute("""SELECT * FROM sessions WHERE room_id=? AND ended_at<>''
                AND (actual_ended_at='' OR actual_started_at='') AND length(platform_session_id) BETWEEN 1 AND 32
                AND platform_session_id NOT GLOB '*[^0-9]*' ORDER BY ended_at DESC,id DESC LIMIT 100""",(room_id,)).fetchall()
            return [dict(row) for row in rows]
    def update_session_end(self,room,session_id,platform_session_id,actual_ended_at,ended_at_source='douyin.room.end_time'):
        """Attach verified platform metadata without changing observation clocks.

        A local session ID and platform session ID must both match a closed
        broadcast. A new open broadcast for this room is never modified.
        """
        if (not isinstance(actual_ended_at,str) or not isinstance(ended_at_source,str)
                or len(ended_at_source)>160 or not ended_at_source
                or not isinstance(platform_session_id,str) or not re.fullmatch(r'[0-9]{1,32}',platform_session_id)):
            return False
        try:
            ended=datetime.fromisoformat(actual_ended_at.replace('Z','+00:00'))
            if (ended.tzinfo is None or ended<datetime(2000,1,1,tzinfo=timezone.utc)
                    or ended>datetime.now(timezone.utc)+timedelta(minutes=5)): return False
        except ValueError: return False
        with self.lock:
            row=self.db.execute("SELECT * FROM sessions WHERE id=? AND room_id=? AND platform_session_id=? AND ended_at<>''",
                                (session_id,room['id'],platform_session_id)).fetchone()
            if not row: return False
            if row['actual_started_at']:
                try:
                    started=datetime.fromisoformat(row['actual_started_at'].replace('Z','+00:00'))
                    if started.tzinfo is not None and ended<=started: return False
                except ValueError: pass
            if row['actual_ended_at']==actual_ended_at and row['ended_at_source']==ended_at_source: return False
            with self.db:
                self.db.execute('UPDATE sessions SET actual_ended_at=?,ended_at_source=? WHERE id=?',
                                (actual_ended_at,ended_at_source,session_id))
            self._csv(room,session_id,'metadata_updated',now_iso(),row['actual_started_at'],row['started_at'],row['ended_at'],
                      platform_session_id,row['started_at_source'],ended_at_source,actual_ended_at)
            return True
    def clear_untrusted_start(self,room):
        """Migrate a mistaken room-creation timestamp without fabricating an end."""
        with self.lock:
            row=self.get_open_session(room['id'])
            if not row or str(row['started_at_source']).rsplit('.',1)[-1] not in ('create_time','createTime'):
                return False
            with self.db:
                self.db.execute("UPDATE sessions SET actual_started_at='',started_at_source='' WHERE id=?",(row['id'],))
            self._csv(room,row['id'],'metadata_updated',now_iso(),'',row['started_at'],'',row['platform_session_id'],'','')
            return True
    def open_session(self,room,at,actual_started_at='',platform_session_id='',started_at_source=''):
        with self.lock:
            row=self.get_open_session(room['id'])
            if row:
                actual=actual_started_at or row['actual_started_at']
                platform_id=platform_session_id or row['platform_session_id']
                source=started_at_source or row['started_at_source']
                changed=(actual,platform_id,source)!=(row['actual_started_at'],row['platform_session_id'],row['started_at_source'])
                if changed:
                    with self.db:
                        self.db.execute('UPDATE sessions SET actual_started_at=?,platform_session_id=?,started_at_source=? WHERE id=?',(actual,platform_id,source,row['id']))
                    self._csv(room,row['id'],'metadata_updated',at,actual,row['started_at'],'',platform_id,source,'')
                return row['id']
            sid=uuid.uuid4().hex
            with self.db:
                self.db.execute('INSERT INTO sessions(id,room_id,started_at,actual_started_at,platform_session_id,started_at_source) VALUES(?,?,?,?,?,?)',(sid,room['id'],at,actual_started_at,platform_session_id,started_at_source))
            self._csv(room,sid,'start',at,actual_started_at,at,'',platform_session_id,started_at_source,'')
            return sid
    def close_session(self,room,at,reason='observed_offline'):
        with self.lock:
            row=self.get_open_session(room['id'])
            if not row: return False
            with self.db: self.db.execute('UPDATE sessions SET ended_at=? WHERE id=?',(at,row['id']))
            self._csv(room,row['id'],'end',at,row['actual_started_at'],row['started_at'],at,row['platform_session_id'],row['started_at_source'],reason)
            return True
    def _csv(self,room,sid,event,at,actual_start,observed_start,observed_end,platform_id,start_source,end_source,actual_end=''):
        directory=self.root/'time_begin_over'/room_directory(room)
        directory.mkdir(parents=True,exist_ok=True)
        columns=['session_id','platform','room_id','nickname','event','observed_at','actual_started_at','observed_started_at','observed_ended_at','platform_session_id','started_at_source','ended_at_source','actual_ended_at']
        path=directory/'events.csv'
        if path.exists() and path.stat().st_size:
            with path.open(encoding='utf-8-sig',newline='') as handle:
                existing=next(csv.reader(handle),[])
            if existing!=columns:
                # Keep old logs intact, including their original header semantics.
                path=directory/'events_v3.csv'
        empty=not path.exists() or not path.stat().st_size
        if not empty:
            with path.open(encoding='utf-8-sig',newline='') as handle:
                if next(csv.reader(handle),[])!=columns:
                    raise ValueError('时间记录 CSV 表头与当前格式不匹配，请保留并重命名该文件后重试')
        with path.open('a',encoding='utf-8-sig',newline='') as handle:
            writer=csv.writer(handle)
            if empty: writer.writerow(columns)
            values=[sid,room['platform'],room['room_input'],room['nickname'],event,at,actual_start,observed_start,observed_end,platform_id,start_source,end_source,actual_end]
            writer.writerow(["'"+str(v) if str(v).startswith(('=','+','-','@')) else v for v in values])
    def close(self):
        with self.lock: self.db.close()
