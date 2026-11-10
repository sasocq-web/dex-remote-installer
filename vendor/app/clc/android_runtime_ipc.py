"""Fixed, UID-authenticated local Android channel; no shell/host interface.

One runtime object is injected by the daemon. This module never constructs a
Lifecycle, starts Android, opens TCP, or creates a second collector.
"""
import asyncio, contextlib, fcntl, json, os, re, socket, stat, struct, time
from pathlib import Path

LIMIT=2_000_000
FIELDS={
    'emulator_view':{'workspace','thread','token','arguments'},
    'emulator_status':{'workspace','thread'},
    'state':set(),
    'quiesce':set(),
    'resume_admission':set(),
    'probe':{'workspace','thread','arguments','turn_id'},
    'execute':{'workspace','thread','arguments','turn_id'},
    'observe':{'event'},
    'pulse':{'active_keys'},
    'viewer':{'workspace','thread','token','connected'},
}

def checked(value):
    if type(value) is not dict or type(value.get('operation')) is not str:
        raise ValueError('runtime_request_invalid')
    op=value['operation']
    if op not in FIELDS or set(value)!={'operation'}|FIELDS[op]:
        raise ValueError('runtime_operation_or_fields_invalid')
    if op in {'execute','viewer','probe','emulator_view','emulator_status'}:
        if (type(value['workspace']) is not str or not re.fullmatch(r'(?:system|project:[A-Za-z0-9_.:-]{1,200})',value['workspace'])
            or type(value['thread']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',value['thread'])
            or value['thread']=='scheduled-whatsapp'):
            raise ValueError('runtime_conversation_invalid')
    if op in {'execute','probe'}:
        if type(value['arguments']) is not dict or type(value['turn_id']) is not str or len(value['turn_id'])>200:
            raise ValueError('runtime_execute_invalid')
    if op=='emulator_view':
        if (type(value['arguments']) is not dict or type(value['token']) is not str
            or not re.fullmatch(r'[a-f0-9]{32}',value['token'])):
            raise ValueError('runtime_viewer_invalid')
    if op=='probe':
        args=value['arguments']
        if (set(args)!={'event_id','dependency_id'}
            or type(args['event_id']) is not str or not re.fullmatch(r'[a-f0-9]{64}',args['event_id'])
            or type(args['dependency_id']) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,100}',args['dependency_id'])):
            raise ValueError('runtime_probe_invalid')
    if op=='viewer':
        if (type(value['connected']) is not bool or type(value['token']) is not str
            or not re.fullmatch(r'[a-f0-9]{32}',value['token'])):
            raise ValueError('runtime_viewer_invalid')
    if op=='pulse':
        keys=value['active_keys']
        if (type(keys) is not list or len(keys)>1024
            or any(type(k) is not list or len(k)!=2 or not all(type(v) is str and 0<len(v)<=210 for v in k) for k in keys)):
            raise ValueError('runtime_pulse_invalid')
    if op=='observe':
        e=value['event']
        if type(e) is not dict or e.get('kind')!='notification' or type(e.get('notification')) is not dict:
            raise ValueError('runtime_event_invalid')
        note=e['notification']
        if note.get('method') not in {'turn/started','turn/completed'} or type(note.get('params')) is not dict:
            raise ValueError('runtime_event_invalid')
    return value

class Server:
    def __init__(self,directory,runtime,uid=1000,release_id=None):
        self.directory=Path(directory);self.runtime=runtime;self.uid=uid;self.release_id=release_id
        self.path=self.directory/'android-runtime.sock'
        self.server=None;self.singleton=None;self.slots=asyncio.Semaphore(8)
        self.active_keys=set();self.last_pulse=0;self.controller=None

    def state(self):
        return {'release_id':self.release_id,'ready':bool(self.controller and self.controller.started),'protocol':1,'owner':self.runtime.owner,
                'background_owner':(self.runtime.background_owner or (('system','scheduled-whatsapp') if self.controller and self.controller.cloud_busy else None)),
                'emulator_owners':([list(k) for k in self.runtime.pool.owners()] if hasattr(self.runtime.pool,'owners') else [[r.workspace,r.thread] for r in self.runtime.pool.reservations.list()]) if hasattr(self.runtime,'pool') else [],
                'cleanup_pending':self.runtime.cleanup_failed or (any(r.phase=='stopping' for r in self.runtime.pool.reservations.list()) if hasattr(self.runtime,'pool') else False),
                'observed_at':time.time()}

    async def dispatch(self,data):
        value=checked(data);op=value['operation']
        if op=='state':return self.state()
        if op=='emulator_status':return self.runtime.pool.status((value['workspace'],value['thread']))
        if op in {'quiesce','resume_admission'}:
            if self.controller is None:raise ValueError('runtime_controller_unavailable')
            if op=='quiesce':return await self.controller.quiesce()
            await self.controller.resume_admission();return self.state()
        if op=='probe':
            if self.runtime.feedback_worker is None:raise ValueError('runtime_feedback_unavailable')
            return await self.runtime.collect_feedback_probe(self.runtime.feedback_worker(),value['arguments'],value['workspace'],value['thread'],value['turn_id'])
        if op=='execute':
            # Existing runtime validates the exact operation/fields before IO.
            if (self.controller and not self.controller.admitted()
                and value['arguments'].get('operation') not in {'status','stop','yield'}
                and self.runtime.owner!=(value['workspace'],value['thread'])):
                raise ValueError('runtime_quiescing')
            self.runtime.command(value['arguments'])
            result=await self.runtime.execute(value['workspace'],value['thread'],value['arguments'],turn_active=True,turn_id=value['turn_id'])
            return {'value':result,'state':self.state()}
        if op=='emulator_view':
            return await self.runtime.pool.viewer_command((value['workspace'],value['thread']),value['token'],value['arguments'])
        if op=='observe':
            await self.runtime.observe(value['event']);return self.state()
        if op=='pulse':
            self.active_keys={tuple(k) for k in value['active_keys']};self.last_pulse=time.monotonic()
            return self.state()
        if op=='viewer':
            await self.runtime.viewer((value['workspace'],value['thread']),value['token'],value['connected'])
            return self.state()

    async def handle(self,reader,writer):
        try:
            peer=writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
            if struct.unpack('3i',peer)[1]!=self.uid:return
            async with asyncio.timeout(1):await self.slots.acquire()
            try:
                async with asyncio.timeout(5):raw=await reader.readline()
                if not raw.endswith(b'\n') or len(raw)>LIMIT:raise ValueError('runtime_message_limit')
                value=await self.dispatch(json.loads(raw))
                response={'ok':True,'result':value}
            except Exception as exc:
                reason=str(exc)
                if not re.fullmatch(r'[a-z][a-z0-9_]{0,95}',reason):reason='runtime_operation_failed'
                response={'ok':False,'error':reason}
            finally:self.slots.release()
            raw=json.dumps(response,separators=(',',':')).encode()+b'\n'
            if len(raw)>LIMIT:raw=b'{"ok":false,"error":"runtime_response_limit"}\n'
            writer.write(raw);await writer.drain()
        except (Exception,asyncio.CancelledError):pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):await writer.wait_closed()

    async def start(self):
        info=self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('runtime_directory_not_private')
        fd=os.open(self.directory/'runtime-singleton.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:os.close(fd);raise
        self.singleton=fd
        try:
            if self.path.exists() or self.path.is_symlink():
                info=self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():
                    raise ValueError('runtime_socket_conflict')
                self.path.unlink()
            self.server=await asyncio.start_unix_server(self.handle,str(self.path),limit=LIMIT+1)
            self.path.chmod(0o600)
        except BaseException:
            os.close(fd);self.singleton=None;raise

    async def close(self):
        if self.server:
            self.server.close();await self.server.wait_closed();self.server=None
            self.path.unlink(missing_ok=True)
        if self.singleton is not None:os.close(self.singleton);self.singleton=None

async def call(path,value,uid=None):
    checked(value)
    uid=os.getuid() if uid is None else uid
    info=Path(path).lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=uid or info.st_mode&0o077:
        raise ValueError('runtime_server_not_private')
    raw=json.dumps(value,separators=(',',':')).encode()+b'\n'
    if len(raw)>LIMIT:raise ValueError('runtime_message_limit')
    # Never retry an uncertain request; the state/intent must be reconciled.
    reader,writer=await asyncio.open_unix_connection(str(path),limit=LIMIT+1)
    try:
        peer=writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
        if struct.unpack('3i',peer)[1]!=uid:raise ValueError('runtime_server_uid_invalid')
        writer.write(raw);await writer.drain()
        async with asyncio.timeout(420 if value.get('operation')=='execute' and 'purpose' in value.get('arguments',{}) else 180):reply=await reader.readline()
        if not reply.endswith(b'\n') or len(reply)>LIMIT:raise ValueError('runtime_response_invalid')
        result=json.loads(reply)
        if result.get('ok') is not True:raise ValueError(result.get('error','runtime_operation_failed'))
        return result['result']
    finally:
        writer.close()
        with contextlib.suppress(Exception):await writer.wait_closed()
