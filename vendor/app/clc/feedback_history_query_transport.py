"""Private fixed query transport specialization; never a public tool."""
from .feedback_history_transport import HistoryEvaluationTransport,digest
from .feedback_source_runtime import decode,wire
from .feedback_native_receipts import fresh

class HistoryQueryTransport(HistoryEvaluationTransport):
    STAGE='history-query'
    IMAGE='sha256:38a45b1cd474393052354daf3df500efbb46ef8a435d012bf632277aef81e41c'
    HELPER_NAME='feedback_history_query_channel.py';WORKER_NAME='feedback_history_query_job.py'
    MAX_INPUT=4065536;MAX_OUTPUT=2066600;BROKER_TIMEOUT=25;DEADLINE=110
    def __init__(self,broker,helper,helper_sha256,worker_sha256,io_sha256,route_sha256,journal,context_get):
        super().__init__(broker,helper,helper_sha256,worker_sha256,journal,context_get)
        self.io_pin=io_sha256;self.route_pin=route_sha256
    def pins(self):
        value=super().pins()
        for name,pin in [('feedback_history_job.py',self.io_pin),('feedback_history_ssh.py',self.route_pin)]:
            f=self.helper.with_name(name)
            if f.is_symlink() or digest(f.read_bytes())!=pin:raise ValueError('private query transitive code pin')
        return dict(value,io=self.io_pin,route=self.route_pin,wrapper='a036f4dbc37cb95b7261a1b95c5022a706bf832e7ec973c716f40ca0fff9a941')
    async def __call__(self,image,stage,raw,operation):
        value=decode(raw,self.MAX_INPUT)
        required={'schema','event_id','dependency_id','source_report_id','contract_revision','nonce','capsule','capsule_sha256','source_provenance'}
        if (type(value) is not dict or set(value)!=required or value['schema']!='nc-history-request-v3'
            or value['nonce']!=operation['nonce'] or operation['token']!=digest(('history-operation-v1:history-query:'+operation['nonce']).encode())[:32]
            or type(value['capsule']) is not dict or digest(wire(value['capsule']))!=value['capsule_sha256']):raise ValueError('private query challenge/capsule binding')
        for key in ['event_id','dependency_id','source_report_id','contract_revision','capsule_sha256']:
            if value[key]!=operation['scope'][key] or type(value[key]) is not type(operation['scope'][key]):raise ValueError('private query original scope')
        scope=operation['scope'];p=value['source_provenance'];manifest=value['capsule'].get('manifest')
        expected_ref='urn:sasocq:private-source-receipt:'+scope['capsule_ref'].split(':')[-1].split('.')[0]
        if (type(p) is not dict or set(p)!={'schema','receipt_ref','payload_sha256','coverage'}
            or p['schema']!='system-private-source-reference-v1' or p['receipt_ref']!=expected_ref
            or p['payload_sha256']!=scope['capsule_sha256'] or type(p['coverage']) is not dict
            or type(manifest) is not list or [r['observed_at'] for r in manifest]!=scope['source_times']
            or not all(fresh(t,self.journal.clock()) for t in scope['source_times'])):raise ValueError('private query original source provenance/time')
        result=await super().__call__(image,stage,raw,operation)
        if not all(fresh(t,self.journal.clock()) for t in scope['source_times']):raise ValueError('private query source expired during processing')
        return result
