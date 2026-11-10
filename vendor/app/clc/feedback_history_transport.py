"""Private chunked broker transport for the fixed offline history evaluator.

No public tool registration. Journal and current managed context are mandatory;
recovery can only stop the exact persisted host job, never create or replay it.
"""
import asyncio,base64,hashlib,json,time
from pathlib import Path
from .feedback_history_jobs import HistoryOperationStore
from .feedback_history_schema import IMAGE

PRIMITIVE_SHA='030979455bcfd5657c04d63155dedc3e8bc811f6dd779fa91fc584eee21a6a1c'
def digest(raw):return hashlib.sha256(raw).hexdigest()
def wire(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

class HistoryEvaluationTransport:
    STAGE='evaluate-history';IMAGE=IMAGE;MAX_INPUT=8100000;MAX_OUTPUT=4000000
    HELPER_NAME='feedback_history_channel.py';WORKER_NAME='feedback_history_job.py'
    BROKER_TIMEOUT=12;DEADLINE=55
    def __init__(self,broker,helper,helper_sha256,worker_sha256,journal,context_get):
        if not isinstance(journal,HistoryOperationStore) or not callable(context_get):raise ValueError('durable history transport required')
        self.broker=broker;self.helper=Path(helper);self.helper_pin=helper_sha256;self.worker_pin=worker_sha256
        self.journal=journal;self.context_get=context_get
    def pins(self):
        files={self.helper:self.helper_pin,self.helper.with_name(self.WORKER_NAME):self.worker_pin,self.helper.with_name('feedback_source_job.py'):PRIMITIVE_SHA}
        if self.helper.name!=self.HELPER_NAME or any(f.is_symlink() or digest(f.read_bytes())!=h for f,h in files.items()):raise ValueError('private history code pins changed')
        return dict(image=self.IMAGE,helper=self.helper_pin,worker=self.worker_pin,primitive=PRIMITIVE_SHA)
    async def call(self,row,op,check=True,**extra):
        pins=self.pins()
        if row.get('transport')!=pins:raise ValueError('private history transport was not durably pinned')
        context=row['scope']['context']
        if check and self.context_get()!=context:raise ValueError('history transport context changed')
        args=dict(op=op,token=row['token'],scope=digest(wire(row['scope'])),**extra)
        params=dict(operation='exec',argv=['runuser','-u','codex-worker','--','env','XDG_RUNTIME_DIR=/run/user/1001',
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus','/usr/bin/python3',str(self.helper)],input_text=json.dumps(args),timeout=self.BROKER_TIMEOUT)
        task=asyncio.create_task(asyncio.to_thread(self.broker,'host-admin',params,timeout=self.BROKER_TIMEOUT+6))
        try:r=await asyncio.shield(task)
        except asyncio.CancelledError:
            await task # pending mutation must settle before exact-own-unit stop
            raise
        result=r.get('result',{});out=result.get('output',{})
        if (not r.get('ok') or result.get('returncode',0) or type(out) is not dict or out.get('returncode') or out.get('truncated')
            or type(out.get('output')) is not str or len(out['output'])>65000):raise RuntimeError('private history broker failure')
        value=json.loads(out['output'])
        if type(value) is not dict or value.get('error'):raise RuntimeError('private history job unavailable')
        if check and self.context_get()!=context:raise ValueError('history transport context changed after broker')
        self.pins();return value
    async def cancel_existing(self,row):
        if row['stage']!=self.STAGE:return dict(inactive=False)
        if row.get('transport') is None:
            # No transport mutation is allowed before durable bind_transport.
            return dict(inactive=True,not_started=True)
        return await self.call(row,'cancel',check=False)
    async def __call__(self,image,stage,raw,operation):
        if (stage!=self.STAGE or image!=self.IMAGE or type(raw) is not bytes or not 0<len(raw)<=self.MAX_INPUT
            or operation!=self.journal.get(operation['token']) or operation['stage']!=stage or operation['state']!='attempted'
            or operation['input_sha256']!=digest(raw) or operation['input_bytes']!=len(raw)
            or self.context_get()!=operation['scope']['context']):raise ValueError('fixed journalled history processing required')
        row=self.journal.bind_transport(operation['token'],self.pins());deadline=time.monotonic()+self.DEADLINE
        try:
            await self.call(row,'create',bytes=len(raw),sha256=digest(raw),stage=stage)
            for offset in range(0,len(raw),36000):
                if time.monotonic()>deadline:raise TimeoutError('private history upload deadline')
                part=await self.call(row,'append',offset=offset,data=base64.b64encode(raw[offset:offset+36000]).decode())
                if part.get('offset')!=min(len(raw),offset+36000):raise ValueError('history chunk order')
            await self.call(row,'start')
            while True:
                if time.monotonic()>deadline:raise TimeoutError('private history deadline')
                result=await self.call(row,'status')
                if result.get('phase') in {'complete','rejected'}:break
                if result.get('phase')!='running':raise RuntimeError('private history job interrupted')
                await asyncio.sleep(.2)
            n=result.get('output_bytes')
            if type(n) is not int or not 0<n<=self.MAX_OUTPUT or result.get('image')!=self.IMAGE:raise ValueError('private history result descriptor')
            chunks=[]
            for offset in range(0,n,36000):
                if time.monotonic()>deadline:raise TimeoutError('private history read deadline')
                part=await self.call(row,'read',offset=offset)
                if part.get('offset')!=offset or type(part.get('data')) is not str or len(part['data'])>48000:raise ValueError('private history result chunk')
                piece=base64.b64decode(part['data'],validate=True)
                if len(piece)!=min(36000,n-offset):raise ValueError('private history result length')
                chunks.append(piece)
            out=b''.join(chunks)
            if digest(out)!=result.get('output_sha256'):raise ValueError('private history result digest')
            return out # temporary payload remains private until durable ack exists
        finally:
            try:cleanup=await self.call(row,'cancel',check=False)
            except Exception:raise RuntimeError('private history cleanup requires reconciliation') from None
            if cleanup.get('inactive') is not True:raise RuntimeError('private history cleanup pending')
