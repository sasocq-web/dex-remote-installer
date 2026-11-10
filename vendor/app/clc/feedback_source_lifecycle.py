"""Native IDs-only source collection; fixed private System configuration.

Normalization produces a private capsule, not an observation or eligibility.
Evaluation/cache/scheduler integration remains a separate guarded consumer.
"""
import asyncio,hashlib,json,re
from pathlib import Path
from .feedback_source_pipeline import SourcePipeline
from .feedback_source_receipts import SourceReceiptStore,PINS,hex64
from .feedback_source_jobs import ProcessingJobStore
from .feedback_source_transport import SourceProcessTransport
from .feedback_source_runtime import SourceRuntimeProtocol,IMAGE
from .control_plane import request as broker_request
from .feedback_history_config import build_history_consumer,close_history_consumer,commit_history_processing

SOURCE_TOOL_SPEC={
    'type':'function','name':'observe_feedback_sources','deferLoading':False,
    'description':'Coleta fontes canônicas registradas usando somente a sessão Playwright já aberta pela própria conversa e turno. Aceita apenas IDs fixos de evento/dependência; não abre navegador, não autentica, não aceita URLs/fatos/recibos. Retorna referências privadas de normalização e, quando configurado, de processamento de histórico somente leitura; não comprova elegibilidade, implementação ou entrega. Requer proprietário/residente corretos e contexto atual.',
    'inputSchema':{'type':'object','additionalProperties':False,'properties':{
        'event_id':{'type':'string','pattern':'^[a-f0-9]{64}$'},
        'dependency_id':{'type':'string','pattern':'^[a-z][a-z0-9_]{0,100}$'}},'required':['event_id','dependency_id']}}

class SourceLifecycle:
    def __init__(self,contexts,worker_factory,app,directory,broker=broker_request):
        self.contexts=contexts;self.worker_factory=worker_factory;self.app=Path(app);self.directory=Path(directory)
        self.broker=broker;self.lock=asyncio.Lock()
        self.recovery_task=None;self.recovery_outcomes=[];self.recovery_error=None
    async def recover(self):
        if not (self.directory/'processing-jobs/receipts.sqlite3').is_file() and not (self.directory/'history/operations/receipts.sqlite3').is_file():return []
        async with self.lock:
            pipeline,helper,policy=self.policy()
            journal=ProcessingJobStore(self.directory/'processing-jobs')
            receipts=SourceReceiptStore(pipeline.directory,pipeline.registry,pipeline.pins)
            history=None
            try:
                # Cleanup uses only the original signed journal scope; it does
                # not acquire/inspect a browser or attest a new session.
                transport=SourceProcessTransport(self.broker,helper,policy['helper_sha256'],policy['worker_sha256'],
                    lambda:None,journal=journal,verified_commit=receipts.processing_committed)
                outcomes=await transport.reconcile_jobs()
                if (self.directory/'history/operations/receipts.sqlite3').is_file():
                    history=build_history_consumer(self.app,self.directory,pipeline,receipts,self.broker,lambda:None)
                    if history is not None:outcomes+=await history.recover()
                return outcomes
            finally:close_history_consumer(history);receipts.close();journal.close()
    async def start(self):
        async def run():
            try:self.recovery_outcomes=await self.recover()
            except Exception as exc:self.recovery_error=type(exc).__name__
        self.recovery_task=asyncio.create_task(run(),name='clc-source-private-recovery')
    async def stop(self):
        if self.recovery_task:
            self.recovery_task.cancel()
            try:await self.recovery_task
            except asyncio.CancelledError:pass
            self.recovery_task=None
    def policy(self):
        bundle=self.app/'system/canonical_sources';path=bundle/'source-policy.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size>16000:raise ValueError('source policy unavailable')
        policy=json.loads(path.read_bytes())
        if (not isinstance(policy,dict) or set(policy)!={'schema','enabled','parser_pins','helper_sha256','worker_sha256'}
            or policy['schema']!='canonical-source-native-policy-v1' or policy['enabled'] is not True
            or set(policy['parser_pins'])!=PINS or not all(hex64(p) for p in policy['parser_pins'].values())
            or policy['parser_pins']['runtime']!=IMAGE.removeprefix('sha256:')
            or not hex64(policy['helper_sha256']) or not hex64(policy['worker_sha256'])):
            raise ValueError('source native policy disabled or invalid')
        helper=self.app/'system/feedback_source_channel.py';worker=self.app/'system/feedback_source_job.py'
        for path,pin in [(helper,policy['helper_sha256']),(worker,policy['worker_sha256'])]:
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=pin:raise ValueError('source immutable processor pin')
        pipeline=SourcePipeline(bundle,policy['parser_pins'],self.directory/'receipts')
        return pipeline,helper,policy
    @staticmethod
    def arguments(value):
        if (not isinstance(value,dict) or set(value)!={'event_id','dependency_id'} or not hex64(value['event_id'])
            or not isinstance(value['dependency_id'],str) or re.fullmatch('[a-z][a-z0-9_]{0,100}',value['dependency_id']) is None):
            raise ValueError('source accepts only fixed event/dependency IDs')
    async def execute(self,workspace,thread,args):
        self.arguments(args)
        if self.lock.locked():raise ValueError('source native collection already active')
        async with self.lock:
            pipeline,helper,policy=self.policy();pipeline.request(args)
            worker=self.worker_factory()
            if worker is None:raise ValueError('source feedback guard unavailable')
            # Own active managed context is mandatory BEFORE journal setup,
            # worker processes, MCP calls or source nonce allocation.
            with self.contexts.acquire(workspace,thread) as (current,mcp):
                turn=current()['turn']
                journal=ProcessingJobStore(self.directory/'processing-jobs')
                receipts=SourceReceiptStore(pipeline.directory,pipeline.registry,pipeline.pins)
                history=None
                try:
                    history=build_history_consumer(self.app,self.directory,pipeline,receipts,self.broker,current)
                    transport=SourceProcessTransport(self.broker,helper,policy['helper_sha256'],policy['worker_sha256'],
                        current,journal=journal,verified_commit=receipts.processing_committed)
                    outcomes=await transport.reconcile_jobs()
                    if history is not None:outcomes+=await history.recover()
                    if any(r['state'] in {'pin_mismatch','cleanup_pending','ack_pending'} for r in outcomes):
                        raise ValueError('source private job reconciliation pending')
                    runtime=SourceRuntimeProtocol(transport)
                    result=await pipeline.collect(worker,args,workspace,thread,turn,current,mcp,runtime.normalize,runtime.acknowledge)
                    if history is not None:
                        processed=await history.execute(worker,args,result,workspace,thread)
                        commit_ref=await commit_history_processing(history,worker,args,processed,workspace,thread)
                        public={k:result[k] for k in ['event_id','dependency_id','source_report_id','contract_revision','private_capsule_ref']}|{
                            'observed_at':processed['result']['observed_at'],'private_history_receipt_ref':processed['private_history_receipt_ref'],
                            'schema':'canonical-source-history-processed-v1','stage':'history_processed','eligibility':'not_evaluated'}
                        if commit_ref is not None:public['private_processing_commit_ref']=commit_ref
                        return public
                    return {k:result[k] for k in ['event_id','dependency_id','source_report_id','contract_revision','observed_at','private_capsule_ref']}|{
                        'schema':'canonical-source-normalized-v1','stage':'normalized','eligibility':'not_evaluated'}
                finally:close_history_consumer(history);receipts.close();journal.close()
    async def handle_server_request(self,workspace,message):
        params=message.get('params') or {}
        if message.get('method')!='item/tool/call' or params.get('tool')!=SOURCE_TOOL_SPEC['name']:return None
        try:
            value=await self.execute(workspace,str(params.get('threadId') or ''),params.get('arguments'))
            return dict(success=True,contentItems=[dict(type='inputText',text=json.dumps(value,ensure_ascii=False))])
        except Exception as exc:
            # Private capture/DB/MCP output and raw exception messages never
            # enter model-facing results. No new session/login is attempted.
            return dict(success=False,contentItems=[dict(type='inputText',text='Canonical source collection refused or unavailable; error class: '+type(exc).__name__)])
