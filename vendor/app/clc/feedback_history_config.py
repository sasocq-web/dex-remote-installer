"""Fixed optional private history consumer policy; no caller-selected files."""
import json,hashlib
from pathlib import Path
from .feedback_history_schema import ReviewedHistoryValidator,CONTRACT_SHA,PRODUCER_SHA,QUERY_SHA,IMAGE
from .feedback_history_receipts import HistoryReceiptStore,hex64
from .feedback_history_jobs import HistoryOperationStore
from .feedback_history_query_transport import HistoryQueryTransport
from .feedback_history_transport import HistoryEvaluationTransport
from .feedback_history_runtime import HistoryRuntimeProtocol
from .feedback_history_consumer import HistoryConsumer

FILES={'query_helper':'feedback_history_query_channel.py','query_worker':'feedback_history_query_job.py',
       'query_io':'feedback_history_job.py','query_route':'feedback_history_ssh.py',
       'eval_helper':'feedback_history_channel.py','eval_worker':'feedback_history_job.py'}
CACHE_FILES={'cache':'feedback_history_cache.py','runtime':'feedback_history_runtime.py','consumer':'feedback_history_consumer.py'}
def cache_policy(app):
    app=Path(app);p=app/'system/canonical_sources/history-cache-policy.json'
    if not p.exists():return False
    if p.is_symlink() or not p.is_file() or p.stat().st_size>16000:raise ValueError('fixed private cache policy file')
    policy=json.loads(p.read_bytes())
    if type(policy) is not dict or set(policy)!={'schema','enabled','code_pins'} or policy['schema']!='private-history-processing-cache-policy-v1':raise ValueError('fixed private cache policy schema')
    if policy['enabled'] is False:return False
    if policy['enabled'] is not True or type(policy['code_pins']) is not dict or set(policy['code_pins'])!=set(CACHE_FILES):raise ValueError('fixed private cache policy pins')
    for key,name in CACHE_FILES.items():
        f=app/'clc'/name;pin=policy['code_pins'][key]
        if not hex64(pin) or f.is_symlink() or not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest()!=pin:raise ValueError('private processing immutable cache pin')
    return True

async def commit_history_processing(consumer,worker,request,processed,workspace,thread):
    # Allocation is lazy: configuration/recovery never creates a cache or key.
    directory=getattr(consumer,'processing_cache_directory',None)
    if directory is None:return None
    from .feedback_history_cache import HistoryProcessingCache
    cache=HistoryProcessingCache(directory,consumer)
    try:return await cache.commit(worker,request,processed,workspace,thread)
    finally:cache.close()

def build_history_consumer(app,directory,pipeline,source,broker,current):
    app=Path(app);directory=Path(directory);p=app/'system/canonical_sources/history-policy.json'
    if not p.exists():return None # Existing normalization-only installation.
    if p.is_symlink() or not p.is_file() or p.stat().st_size>16000:raise ValueError('fixed history policy file')
    policy=json.loads(p.read_bytes())
    if type(policy) is not dict or set(policy)!={'schema','enabled','code_pins','contract_sha256'} or policy['schema']!='native-private-history-consumer-v1':raise ValueError('fixed history policy schema')
    if policy['enabled'] is False:return None
    if policy['enabled'] is not True or policy['contract_sha256']!=CONTRACT_SHA or type(policy['code_pins']) is not dict or set(policy['code_pins'])!=set(FILES):raise ValueError('fixed history policy pins')
    caching=cache_policy(app) # Validate fixed code before journals/query/source work.
    pins=policy['code_pins']
    for key,name in FILES.items():
        f=app/'system'/name
        if not hex64(pins[key]) or f.is_symlink() or not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest()!=pins[key]:raise ValueError('native history immutable code pin')
    contract=app/'system/canonical_sources/history-v3-contract.json'
    if contract.is_symlink() or not contract.is_file() or contract.stat().st_size>100000:raise ValueError('native history fixed contract')
    validator=ReviewedHistoryValidator(contract.read_bytes())
    receipts=HistoryReceiptStore(directory/'history/receipts',dict(producer=PRODUCER_SHA,queries=QUERY_SHA,runtime=IMAGE[7:]),validator.snapshot_keys,validator,pipeline.clock)
    journal=None
    try:
        journal=HistoryOperationStore(directory/'history/operations',pipeline.clock)
        query=HistoryQueryTransport(broker,app/'system'/FILES['query_helper'],pins['query_helper'],pins['query_worker'],pins['query_io'],pins['query_route'],journal,current)
        evaluate=HistoryEvaluationTransport(broker,app/'system'/FILES['eval_helper'],pins['eval_helper'],pins['eval_worker'],journal,current)
        query.pins();evaluate.pins()
        runtime=HistoryRuntimeProtocol(receipts,validator,journal,evaluate,pipeline.clock)
        consumer=HistoryConsumer(pipeline,source,receipts,journal,validator,query,runtime,current)
        consumer.processing_cache_directory=directory/'history/processing-cache' if caching else None
        return consumer
    except BaseException:
        receipts.close()
        if journal is not None:journal.close()
        raise

def close_history_consumer(consumer):
    if consumer is not None:consumer.receipts.close();consumer.journal.close()
