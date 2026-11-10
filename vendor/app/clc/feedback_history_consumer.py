"""Private native consumer of a committed source capsule, never caller facts.

Query/evaluation is processing only. Observation publication, authenticated
cache commit/ACK and scheduler admission are intentionally separate consumers.
"""
import asyncio,json,hashlib
from .feedback_source_receipts import SourceReceiptStore
from .feedback_source_pipeline import SourcePipeline
from .feedback_source_runtime import decode,wire
from .feedback_history_receipts import HistoryReceiptStore
from .feedback_history_jobs import HistoryOperationStore
from .feedback_history_schema import ReviewedHistoryValidator,PRODUCER_SHA,QUERY_SHA,IMAGE
from .feedback_history_runtime import HistoryRuntimeProtocol
from .feedback_history_query_transport import HistoryQueryTransport
from .feedback_history_transport import HistoryEvaluationTransport

def digest(raw):return hashlib.sha256(raw).hexdigest()

class HistoryConsumer:
    def __init__(self,pipeline,source,receipts,journal,validator,query,runtime,current):
        if (not isinstance(pipeline,SourcePipeline) or not isinstance(source,SourceReceiptStore)
            or not isinstance(receipts,HistoryReceiptStore) or not isinstance(journal,HistoryOperationStore)
            or not isinstance(validator,ReviewedHistoryValidator) or not isinstance(query,HistoryQueryTransport)
            or not isinstance(runtime,HistoryRuntimeProtocol) or query.journal is not journal or runtime.journal is not journal
            or not isinstance(runtime.process,HistoryEvaluationTransport)
            or runtime.store is not receipts or receipts.validator is not validator or runtime.validator is not validator
            or not callable(current)):raise ValueError('verified native history consumer required')
        if receipts.pins!=dict(producer=PRODUCER_SHA,queries=QUERY_SHA,runtime=IMAGE[7:]):raise ValueError('native history receipt policy differs')
        if source.registry!=pipeline.registry or source.pins!=pipeline.pins or source.store.directory!=pipeline.directory:
            raise ValueError('native history source receipt store differs')
        self.pipeline=pipeline;self.source=source;self.receipts=receipts;self.journal=journal;self.validator=validator
        self.query=query;self.runtime=runtime;self.context_get=current
    async def recover(self):
        async def cancel(row):
            if row['stage']=='history-query':return await self.query.cancel_existing(row)
            if row['stage']=='evaluate-history':return await self.runtime.process.cancel_existing(row)
            return {'inactive':False}
        return await self.journal.reconcile(self.receipts.pins,cancel)
    async def execute(self,worker,request,normalized,workspace,thread):
        self.pipeline.request(request)
        context=self.context_get()
        if (type(context) is not dict or context.get('workspace')!=workspace or context.get('thread')!=thread
            or not context.get('turn')):raise ValueError('native history own current turn required')
        current=lambda candidate=None:self.context_get()==context and (candidate is None or candidate==context)
        binding=await asyncio.to_thread(self.pipeline.binding,worker,request,workspace,thread)
        fixed=self.validator.policy['fixed_registry'].get(request['event_id'])
        if (not fixed or request['dependency_id'] not in fixed['dependencies']
            or fixed['source_report_id']!=binding['source_report_id'] or fixed['contract_revision']!=binding['contract_revision']
            or any(normalized.get(k)!=request[k] for k in ['event_id','dependency_id'])
            or any(normalized.get(k)!=binding[k] for k in ['source_report_id','contract_revision'])):raise ValueError('native history current binding differs')
        reference=normalized.get('private_capsule_ref')
        capsule_raw=self.source.load_capsule(reference,request,context,current)
        capsule=decode(capsule_raw,4000000)
        if wire(capsule)!=capsule_raw:raise ValueError('native source capsule encoding differs')
        scope=dict(**request,source_report_id=binding['source_report_id'],contract_revision=binding['contract_revision'],context=context,
            capsule_ref=reference,capsule_sha256=digest(capsule_raw),source_times=[r['observed_at'] for r in capsule['manifest']],pins=self.receipts.pins)
        def source_current(candidate):
            return candidate==scope and current() and self.source.load_capsule(reference,request,context,current)==capsule_raw
        async def check_binding():
            if not current() or await asyncio.to_thread(self.pipeline.binding,worker,request,workspace,thread)!=binding:
                raise ValueError('native history contract/context changed')
        await check_binding()
        nonce=self.receipts.issue(scope,current,source_current) # durable BEFORE query
        body=dict(schema='nc-history-request-v3',**request,source_report_id=scope['source_report_id'],contract_revision=scope['contract_revision'],nonce=nonce,
            capsule=capsule,capsule_sha256=scope['capsule_sha256'],source_provenance=dict(schema='system-private-source-reference-v1',
                receipt_ref='urn:sasocq:private-source-receipt:'+reference.split(':')[-1].split('.')[0],payload_sha256=scope['capsule_sha256'],coverage={}))
        raw=wire(body)
        if len(raw)>4065536:raise ValueError('native query private input capacity')
        token=self.journal.prepare('history-query',nonce,scope,raw)
        self.journal.change(token,'attempted') # BEFORE broker/SSH/worker mutation
        returned=await self.query(self.query.IMAGE,'history-query',raw,self.journal.get(token))
        self.journal.change(token,'finished',returned)
        await check_binding()
        out=decode(returned,2066600)
        if type(out) is not dict or set(out)!={'ok','stage','snapshot_sha256','snapshot'} or out['ok'] is not True or out['stage']!='history-snapshot':
            raise ValueError('native private producer envelope')
        snapshot=wire(out['snapshot']);sha=digest(snapshot)
        if sha!=out['snapshot_sha256']:raise ValueError('native private snapshot hash')
        def transport_current(candidate,snapshot_sha):
            row=self.journal.get(token)
            return (candidate==scope and snapshot_sha==sha and row['state']=='finished' and row['scope']==scope
                and row['nonce']==nonce and row['input_sha256']==digest(raw) and row['output_sha256']==digest(returned)
                and row['transport']==self.query.pins() and current())
        receipt=self.receipts.seal(nonce,snapshot,current,source_current,transport_current)
        await check_binding()
        result=await self.runtime.evaluate(scope,capsule_raw,snapshot,receipt,current,source_current)
        await check_binding()
        # These private data are reserved for durable cache/commit verification;
        # never pass them directly to a native tool response or scheduler.
        return dict(result=result,receipt=receipt,scope=scope,snapshot=snapshot,query_token=token,query_output=returned,query_input=raw,
            private_history_receipt_ref='urn:sasocq:private-history-receipt:'+nonce)
