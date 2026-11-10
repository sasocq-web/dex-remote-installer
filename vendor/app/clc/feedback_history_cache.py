"""Private durable processing artifacts. No observation admission or ACK authority.

Only a live typed consumer with a verified one-shot output can commit. All
original bytes stay private; a FULL-synchronous SQLite transaction commits the
artifact and its HMAC manifest together. Historical commit checks never refresh
source timestamps and are not freshness/eligibility checks.
"""
import asyncio,base64,hashlib,hmac,json,re
from .feedback_native_receipts import NativeReceiptStore,fresh,wire
from .feedback_history_consumer import HistoryConsumer
from .feedback_history_schema import encoded

MAX_BYTES=64000000
MAX_ITEMS=64

def sha(raw):return hashlib.sha256(raw).hexdigest()

class HistoryProcessingCache:
    def __init__(self,directory,consumer):
        if not isinstance(consumer,HistoryConsumer):raise ValueError('typed private history consumer required')
        self.consumer=consumer;self.store=NativeReceiptStore(directory,consumer.pipeline.clock);self.db=self.store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS history_processing_cache(id TEXT PRIMARY KEY,manifest TEXT NOT NULL,signature TEXT NOT NULL,artifact BLOB NOT NULL)');self.db.commit()
    def close(self):self.store.close()
    def sign(self,raw):return hmac.new(self.store.key,b'private-history-processing-cache-v1\0'+raw.encode(),hashlib.sha256).hexdigest()
    def row(self,ref):
        if type(ref) is not str or re.fullmatch(r'urn:sasocq:private-history-processing:[a-f0-9]{64}',ref) is None:raise ValueError('private processing reference')
        row=self.db.execute('SELECT manifest,signature,artifact FROM history_processing_cache WHERE id=?',(ref.split(':')[-1],)).fetchone()
        if not row or not hmac.compare_digest(self.sign(row[0]),row[1]):raise ValueError('private processing manifest signature')
        m=json.loads(row[0]);raw=bytes(row[2])
        if (m.get('schema')!='private-history-processing-commit-v1' or m.get('nonce')!=ref.split(':')[-1] or m.get('processing_only') is not True):raise ValueError('private processing manifest binding')
        if m['artifact_sha256']!=sha(raw) or m['artifact_bytes']!=len(raw):raise ValueError('private processing artifact changed')
        return m,raw
    def committed(self,ref):
        # Metadata-only, suitable for future authenticated ACK integration.
        # This does NOT authorize ACK or return an eligible observation.
        m,_=self.row(ref)
        return dict(reference=ref,artifact_sha256=m['artifact_sha256'],observed_at=m['observed_at'],processing_only=True)
    async def load(self,ref,worker,request,workspace,thread):
        c=self.consumer;c.pipeline.request(request)
        m,raw=self.row(ref);scope=m['scope'];ctx=scope['context']
        current=lambda candidate=None:c.context_get()==ctx and (candidate is None or candidate==ctx)
        if (ctx.get('workspace')!=workspace or ctx.get('thread')!=thread or not current()
            or any(request[k]!=scope[k] for k in ['event_id','dependency_id'])
            or not all(fresh(t,c.pipeline.clock()) for t in scope['source_times']+[m['db_observed_at']])):
            raise ValueError('private processing stale or context changed')
        binding=await asyncio.to_thread(c.pipeline.binding,worker,request,workspace,thread)
        if any(binding[k]!=scope[k] for k in ['source_report_id','contract_revision']):raise ValueError('private processing load current CAS')
        source=c.source.load_capsule(scope['capsule_ref'],request,ctx,current)
        if sha(source)!=scope['capsule_sha256']:raise ValueError('private processing source changed')
        return raw # PRIVATE processing artifact, never an observation.
    async def commit(self,worker,request,processed,workspace,thread):
        c=self.consumer;c.pipeline.request(request)
        if type(processed) is not dict:raise ValueError('private processing output required')
        scope=processed['scope'];ctx=scope['context']
        current=lambda candidate=None:c.context_get()==ctx and (candidate is None or candidate==ctx)
        if ctx.get('workspace')!=workspace or ctx.get('thread')!=thread or not ctx.get('turn') or not current():raise ValueError('private processing own context')
        binding=await asyncio.to_thread(c.pipeline.binding,worker,request,workspace,thread)
        def binding_matches(v):return all(v[k]==scope[k] for k in ['source_report_id','contract_revision'])
        if not binding_matches(binding) or any(request[k]!=scope[k] for k in ['event_id','dependency_id']):raise ValueError('private processing current CAS')
        source=c.source.load_capsule(scope['capsule_ref'],request,ctx,current)
        snapshot=processed['snapshot'];query=processed['query_output'];token=processed['query_token']
        nonce=processed['private_history_receipt_ref'].split(':')[-1]
        eval_token=hashlib.sha256(('history-operation-v1:evaluate-history:'+nonce).encode()).hexdigest()[:32]
        output=c.runtime.verified_output(eval_token);eval_input=c.runtime.verified_input(eval_token);query_input=processed['query_input']
        receipt=processed['receipt'];payload=receipt['payload']
        if not hmac.compare_digest(c.receipts.sign(payload),receipt['signature']):raise ValueError('private processing history signature')
        row=c.receipts.db.execute('SELECT scope,payload,used FROM history_receipts WHERE nonce=?',(nonce,)).fetchone()
        if row!=(wire(scope),payload,1):raise ValueError('private processing consumed history receipt')
        h=json.loads(payload)
        if h['nonce']!=nonce or h['snapshot_sha256']!=sha(snapshot) or h['scope']!=scope:raise ValueError('private processing history binding')
        c.receipts.scope(scope,current,lambda value:value==scope and sha(source)==scope['capsule_sha256'])
        snap=json.loads(snapshot);c.validator(snap)
        if encoded(snap)!=snapshot or snap['nonce']!=nonce or snap['db_observed_at']!=h['db_observed_at']:raise ValueError('private processing snapshot bytes/time')
        q=json.loads(query);out=json.loads(output)
        if q!={'ok':True,'stage':'history-snapshot','snapshot_sha256':sha(snapshot),'snapshot':snap}:raise ValueError('private processing query envelope')
        if (out['result']!=processed['result'] or out['snapshot_sha256']!=sha(snapshot)
            or out['capsule_sha256']!=sha(source) or out['result_sha256']!=sha(encoded(processed['result']))):raise ValueError('private processing verified output differs')
        original=min(scope['source_times']+[h['db_observed_at']])
        if h['observed_at']!=original or processed['result']['observed_at']!=original:raise ValueError('private processing original time')
        query_body=json.loads(query_input);eval_body=json.loads(eval_input)
        if (encoded(query_body)!=query_input or query_body['nonce']!=nonce or encoded(query_body['capsule'])!=source
            or any(query_body[k]!=scope[k] for k in ['event_id','dependency_id','source_report_id','contract_revision','capsule_sha256'])
            or eval_body['request']!=query_body or encoded(eval_body['snapshot'])!=snapshot):raise ValueError('private processing exact input binding')
        jobs=[]
        for stage,job_token,raw,input_raw,transport in [('history-query',token,query,query_input,c.query),('evaluate-history',eval_token,output,eval_input,c.runtime.process)]:
            job=c.journal.get(job_token)
            expected=hashlib.sha256(('history-operation-v1:'+stage+':'+nonce).encode()).hexdigest()[:32]
            if (job_token!=expected or job['stage']!=stage or job['nonce']!=nonce or job['state']!='finished'
                or job['scope']!=scope or job['input_sha256']!=sha(input_raw) or job['input_bytes']!=len(input_raw)
                or job['output_sha256']!=sha(raw) or job['output_bytes']!=len(raw)
                or job['transport']!=transport.pins()):raise ValueError('private processing signed job binding')
            jobs.append(job)
        if not current() or await asyncio.to_thread(c.pipeline.binding,worker,request,workspace,thread)!=binding:raise ValueError('private processing CAS changed before commit')
        # Re-load the signed original capsule immediately before the synchronous
        # transaction: no await or external operation between final check/commit.
        if c.source.load_capsule(scope['capsule_ref'],request,ctx,current)!=source:raise ValueError('private processing source changed before commit')
        if not all(fresh(t,c.pipeline.clock()) for t in scope['source_times']+[h['db_observed_at']]):raise ValueError('private processing expired before commit')
        artifact=encoded(dict(query_input=base64.b64encode(query_input).decode(),evaluation_input=base64.b64encode(eval_input).decode(),source=base64.b64encode(source).decode(),snapshot=base64.b64encode(snapshot).decode(),query_output=base64.b64encode(query).decode(),evaluation_output=base64.b64encode(output).decode(),receipt=receipt,jobs=jobs))
        if len(artifact)>MAX_BYTES:raise ValueError('private processing artifact capacity')
        manifest=wire(dict(schema='private-history-processing-commit-v1',scope=scope,nonce=nonce,query_token=token,eval_token=eval_token,
            observed_at=original,db_observed_at=h['db_observed_at'],artifact_sha256=sha(artifact),artifact_bytes=len(artifact),processing_only=True))
        ref='urn:sasocq:private-history-processing:'+nonce
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT manifest,signature,artifact FROM history_processing_cache WHERE id=?',(nonce,)).fetchone()
            if old:
                if old!=(manifest,self.sign(manifest),artifact):raise ValueError('private processing commit replay changed')
            else:
                count,size=self.db.execute('SELECT count(*),coalesce(sum(length(artifact)),0) FROM history_processing_cache').fetchone()
                if count>=MAX_ITEMS or size+len(artifact)>MAX_BYTES:raise ValueError('private processing retained quota')
                self.db.execute('INSERT INTO history_processing_cache VALUES(?,?,?,?)',(nonce,manifest,self.sign(manifest),artifact))
            self.db.commit()
        except BaseException:self.db.rollback();raise
        self.row(ref) # Read back the committed bytes/signature, no timestamp change.
        return ref
