"""Private v3 offline evaluator protocol, not an observation producer.

Only the System lifecycle may supply a committed source capsule and signed
history receipt. The transport remains an internal fixed-image callback.
No native tool or scheduler is registered by this development module.
"""
import hashlib
import json
from .feedback_native_receipts import fresh
from .feedback_source_runtime import decode,CAPSULE_FIELDS,TRUST
from .feedback_history_schema import ReviewedHistoryValidator,IMAGE,encoded
from .feedback_history_receipts import HistoryReceiptStore
from .feedback_history_jobs import HistoryOperationStore

def sha(raw):return hashlib.sha256(raw).hexdigest()

class HistoryRuntimeProtocol:
    def __init__(self, store, validator, journal, process, clock):
        if not isinstance(store,HistoryReceiptStore) or not isinstance(validator,ReviewedHistoryValidator) or not isinstance(journal,HistoryOperationStore) or not callable(process):
            raise ValueError('fixed private history runtime required')
        if store.validator is not validator or store.keys != validator.snapshot_keys or store.pins != dict(producer=validator.policy['snapshot']['producer_sha256'],queries=validator.policy['query_hash'],runtime=IMAGE.split(':')[1]):
            raise ValueError('history runtime/store policy differs')
        self.store=store;self.validator=validator;self.journal=journal;self.process=process;self.clock=clock;self._verified=None

    def current(self,scope,current,source_current):
        self.store.scope(scope,current,source_current)

    def verified_output(self,token):
        if self._verified is None or self._verified[0]!=token:raise ValueError("no verified private runtime output")
        raw=self._verified[2];row=self.journal.get(token)
        if row["state"]!="finished" or row["output_sha256"]!=sha(raw):raise ValueError("verified runtime journal changed")
        return raw

    def verified_input(self,token):
        self.verified_output(token)
        raw=self._verified[1];row=self.journal.get(token)
        if row['input_sha256']!=sha(raw):raise ValueError('verified runtime input changed')
        return raw

    async def evaluate(self,scope,capsule_raw,snapshot_raw,receipt,current,source_current):
        self._verified=None
        self.current(scope,current,source_current)
        capsule=decode(capsule_raw,4000000);snapshot=decode(snapshot_raw,2065536)
        if type(capsule) is not dict or set(capsule)!=CAPSULE_FIELDS or sha(capsule_raw)!=scope['capsule_sha256'] or encoded(capsule)!=capsule_raw:
            raise ValueError('history source capsule changed/encoding differs')
        self.validator(snapshot)
        for key in ['event_id','dependency_id','source_report_id','contract_revision','capsule_sha256']:
            if snapshot[key]!=scope[key] or type(snapshot[key]) is not type(scope[key]): raise ValueError('history scope binding')
        rows=capsule['manifest'];binding=self.validator.policy['fixed_registry'][scope['event_id']]['dependencies'][scope['dependency_id']]
        if (type(rows) is not list or len(rows)!=len(binding['source_ids']) or {r['id'] for r in rows}!=set(binding['source_ids'])
            or [r['observed_at'] for r in rows]!=scope['source_times']): raise ValueError('history original source manifest')
        if encoded(snapshot)!=snapshot_raw: raise ValueError('history snapshot encoding differs')
        # One-shot HMAC consumption occurs before the external worker operation.
        # Crash/failed operation cannot replay this receipt or refresh its dates.
        verified=self.store.consume(receipt,scope,snapshot_raw,current,source_current)
        original=min(scope['source_times']+[snapshot['db_observed_at']])
        if verified['observed_at']!=original: raise ValueError('history original times')
        request={k:scope[k] for k in ['event_id','dependency_id','source_report_id','contract_revision','capsule_sha256']}
        nonce=scope['capsule_ref'].split(':')[-1].split('.')[0]
        request.update(schema='nc-history-request-v3',nonce=snapshot['nonce'],capsule=capsule,
            source_provenance=dict(schema='system-private-source-reference-v1',receipt_ref='urn:sasocq:private-source-receipt:'+nonce,payload_sha256=scope['capsule_sha256'],coverage={}))
        envelope=dict(schema='nc-history-evaluation-v3',request=request,snapshot=snapshot,snapshot_sha256=verified['snapshot_sha256'],
            history_provenance=dict(schema='system-private-history-reference-v1',receipt_ref=verified['evidence_ref'],payload_sha256=verified['snapshot_sha256']))
        body=encoded(envelope)
        if len(body)>8100000:raise ValueError('history evaluation capacity')
        token=self.journal.prepare('evaluate-history',snapshot['nonce'],scope,body)
        self.journal.change(token,'attempted') # durable BEFORE any worker start
        raw=await self.process(IMAGE,'evaluate-history',body,self.journal.get(token))
        self.journal.change(token,'finished',raw)
        self.current(scope,current,source_current)
        if not all(fresh(t,self.clock()) for t in scope['source_times']+[snapshot['db_observed_at']]): raise ValueError('history expired during processing')
        out=decode(raw,4000000)
        if (type(out) is not dict or set(out)!={'ok','stage','trust_state','capsule_sha256','snapshot_sha256','result_sha256','result'}
            or out['ok'] is not True or out['stage']!='evaluate-history' or out['trust_state']!=TRUST
            or out['capsule_sha256']!=scope['capsule_sha256'] or out['snapshot_sha256']!=verified['snapshot_sha256']): raise ValueError('history runtime envelope')
        result=out['result']
        if type(result) is not dict or set(result)!={'event_id','dependency_id','source_report_id','contract_revision','observed_at','semantic_reason','diagnostic'} or sha(encoded(result))!=out['result_sha256']:
            raise ValueError('history runtime digest/schema')
        for key in ['event_id','dependency_id','source_report_id','contract_revision']:
            if result[key]!=scope[key] or type(result[key]) is not type(scope[key]):raise ValueError('history runtime CAS')
        if (type(result['observed_at']) not in (int,float) or result['observed_at']!=original
            or result['semantic_reason'] not in ('semantic_match','specific_source_conditions_unresolved') or type(result['diagnostic']) is not dict):
            raise ValueError('history runtime date/reason')
        self._verified=(token,body,raw) # Private bounded artifact for durable commit, never public.
        return result # private processing result only; never marks delivered.
