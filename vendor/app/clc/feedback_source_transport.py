"""Internal broker transport; raw bodies never become native tool output.

No transport is configured in production. System must bind the helper hash and
private context supplier; source provenance is verified before invoking this.
"""
import asyncio,base64,hashlib,json,re,secrets,time
from pathlib import Path
from .feedback_source_runtime import IMAGE

# Reviewed immutable compatibility is CANCEL ONLY, never start/ACK. The helper
# bytes and image are identical; current worker is still fully pin-verified.
CANCEL_ONLY_COMPATIBILITY={(
    '64c05d1c9078e35addfa839ac689c0c1ba20f756975939bde5f03c7feb6b0d7a',
    '030979455bcfd5657c04d63155dedc3e8bc811f6dd779fa91fc584eee21a6a1c',
    '63054eb3f0b269003e40f6fe583ca604f19f658c1b7ad69f8d5b2ae57b35363c',
    'sha256:43e3c5577ecb6dd92c79770f2b22bf52bf866ed98460e70bfa93ef79fdd3e0ee')}
def cancel_compatible(row,pins):
    old=row['pins']
    return (old.get('image')==pins['image'] and old.get('helper')==pins['helper']
        and (old.get('worker'),pins['worker'],pins['helper'],pins['image']) in CANCEL_ONLY_COMPATIBILITY)

class SourceProcessTransport:
    def __init__(self,broker,helper,helper_sha256,worker_sha256,context_get,journal=None,verified_commit=None):
        self.broker=broker;self.helper=Path(helper);self.pin=helper_sha256;self.worker_pin=worker_sha256;self.context_get=context_get
        self.pending={}
        self.journal=journal;self.verified_commit=verified_commit
    async def reconcile_jobs(self):
        if self.journal is None or not callable(self.verified_commit):raise ValueError('private durable job recovery not configured')
        worker=self.helper.with_name('feedback_source_job.py')
        if (self.helper.is_symlink() or worker.is_symlink() or hashlib.sha256(self.helper.read_bytes()).hexdigest()!=self.pin
            or hashlib.sha256(worker.read_bytes()).hexdigest()!=self.worker_pin):raise ValueError('private recovery code pins changed')
        pins=dict(image=IMAGE,helper=self.pin,worker=self.worker_pin)
        async def existing(row,op):
            if op not in {'cancel','ack'} or (row['pins']!=pins and (op!='cancel' or not cancel_compatible(row,pins))):raise ValueError('private recovery cannot launch or ACK legacy jobs')
            args=dict(op=op,token=row['token'],scope=row['scope'])
            if op=='ack':args.update(output_sha256=row['output_sha256'],consumer_ref=row['consumer_ref'])
            params=dict(operation='exec',argv=['runuser','-u','codex-worker','--','env','XDG_RUNTIME_DIR=/run/user/1001',
                'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus','/usr/bin/python3',str(self.helper)],
                input_text=json.dumps(args),timeout=12)
            task=asyncio.create_task(asyncio.to_thread(self.broker,'host-admin',params,timeout=18))
            try:r=await asyncio.shield(task)
            except asyncio.CancelledError:await task;raise
            result=r.get('result',{});out=result.get('output',{})
            if (not r.get('ok') or result.get('returncode',0) or not isinstance(out,dict) or out.get('returncode')
                or out.get('truncated') or not isinstance(out.get('output'),str) or len(out['output'])>65000):
                raise RuntimeError('private recovery broker failure')
            value=json.loads(out['output'])
            if not isinstance(value,dict) or value.get('error'):raise RuntimeError('private recovery requires reconciliation')
            return value
        outcomes=await self.journal.reconcile(pins,existing,self.verified_commit,cancel_compatible)
        for r in outcomes:
            if r['state']=='released':
                row=self.journal.get(r['token']);self.pending.pop(row['source_nonce']+'.'+row['capsule_sha256'],None)
        return outcomes
    async def acknowledge(self,reference):
        match=re.fullmatch(r'urn:sasocq:private-canonical:([a-f0-9]{64})\.([a-f0-9]{64})',reference) if isinstance(reference,str) else None
        if not match or match[1]+'.'+match[2] not in self.pending:raise ValueError('private processing consumer not pending')
        key=match[1]+'.'+match[2];item=self.pending[key]
        if self.context_get()!=item['context']:raise ValueError('private consumer context changed')
        if self.journal is not None:self.journal.commit(item['token'],reference,self.verified_commit)
        r=await item['call']('ack',output_sha256=item['sha'],consumer_ref=reference)
        if r.get('released') is not True:raise RuntimeError('private payload release not confirmed')
        if self.journal is not None:self.journal.released(item['token'])
        self.pending.pop(key)
    async def __call__(self,stage,raw,image):
        worker=self.helper.with_name('feedback_source_job.py')
        if (stage!='normalize' or image!=IMAGE or not isinstance(raw,bytes) or len(raw)>16065536
            or self.helper.is_symlink() or hashlib.sha256(self.helper.read_bytes()).hexdigest()!=self.pin
            or worker.is_symlink() or hashlib.sha256(worker.read_bytes()).hexdigest()!=self.worker_pin):
            raise ValueError('fixed private source process required')
        context=self.context_get();scope=hashlib.sha256(json.dumps(context,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        token=secrets.token_hex(16);deadline=time.monotonic()+55;created=False
        if self.journal is not None:
            self.journal.prepare(token,context,dict(image=image,helper=self.pin,worker=self.worker_pin),stage,raw)
        async def call(op,check=True,**extra):
            if check and self.context_get()!=context:raise ValueError('source process context changed')
            args=dict(op=op,token=token,scope=scope,**extra)
            params=dict(operation='exec',argv=['runuser','-u','codex-worker','--','env','XDG_RUNTIME_DIR=/run/user/1001',
                'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus','/usr/bin/python3',str(self.helper)],
                input_text=json.dumps(args),timeout=12)
            task=asyncio.create_task(asyncio.to_thread(self.broker,'host-admin',params,timeout=18))
            try:r=await asyncio.shield(task)
            except asyncio.CancelledError:
                # Reconcile the pending mutation before stopping its exact unit.
                await task
                raise
            result=r.get('result',{});value=result.get('output',{})
            if (not r.get('ok') or result.get('returncode',0) or not isinstance(value,dict)
                or value.get('returncode') or value.get('truncated') or not isinstance(value.get('output'),str)
                or len(value['output'])>65000):raise RuntimeError('private source broker failure')
            out=json.loads(value['output'])
            if not isinstance(out,dict) or out.get('error'):raise RuntimeError('private source job failure')
            if check and self.context_get()!=context:raise ValueError('source process context changed')
            return out
        try:
            created=True
            await call('create',bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),stage=stage)
            for offset in range(0,len(raw),36000):
                if time.monotonic()>deadline:raise TimeoutError('private source transport deadline')
                r=await call('append',offset=offset,data=base64.b64encode(raw[offset:offset+36000]).decode())
                if r.get('offset')!=min(offset+36000,len(raw)):raise ValueError('private upload order')
            if self.journal is not None:self.journal.attempted(token)
            await call('start')
            while True:
                if time.monotonic()>deadline:raise TimeoutError('private source transport deadline')
                r=await call('status')
                if r.get('phase') in {'complete','rejected'}:break
                if r.get('phase')!='running':raise RuntimeError('private source job interrupted')
                await asyncio.sleep(.2)
            n=r.get('output_bytes');sha=r.get('output_sha256')
            if type(n) is not int or not 0<n<=4065536 or r.get('image')!=IMAGE:raise ValueError('private result descriptor')
            chunks=[]
            for offset in range(0,n,36000):
                part=await call('read',offset=offset)
                if part.get('offset')!=offset or not isinstance(part.get('data'),str) or len(part['data'])>48000:raise ValueError('private result chunk')
                piece=base64.b64decode(part['data'],validate=True)
                if len(piece)!=min(36000,n-offset):raise ValueError('private result length')
                chunks.append(piece)
            out=b''.join(chunks)
            if hashlib.sha256(out).hexdigest()!=sha:raise ValueError('private result digest')
            # Retain only a private opaque processing handle. The source store
            # must commit and verify the capsule before calling acknowledge.
            accepted_capsha=None
            try:
                value=json.loads(out);envelope=json.loads(raw)
                source_ref=envelope.get('source_provenance',{}).get('receipt_ref','')
                source_match=re.fullmatch(r'urn:sasocq:private-source-receipt:([a-f0-9]{64})',source_ref)
                capsha=value.get('capsule_sha256')
                if (value.get('ok') is True and value.get('stage')=='normalize' and source_match
                    and isinstance(capsha,str) and re.fullmatch('[a-f0-9]{64}',capsha)):
                    key=source_match[1]+'.'+capsha
                    if key in self.pending:raise RuntimeError('private processing consumer conflict')
                    self.pending[key]=dict(call=call,context=context,sha=sha,token=token)
                    accepted_capsha=capsha
            except (ValueError,AttributeError,TypeError):pass
            if self.journal is not None:self.journal.finished(token,sha,n,accepted_capsha)
            return out
        finally:
            if created:
                # Cleanup only the exact unit belonging to this private job;
                # preserve input/result evidence and never retry processing.
                try:cleanup=await call('cancel',check=False)
                except Exception:raise RuntimeError('private source cleanup requires reconciliation') from None
                if cleanup.get('inactive') is not True:raise RuntimeError('private source cleanup pending')
