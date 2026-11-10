"""Private System challenge/HMAC for a pinned readonly DB snapshot.

No public action accepts snapshot facts. System must bind the fixed SSH
transport, current native context and committed fresh source capsule callbacks.
This receipt alone never publishes observations or resumes a feedback item.
"""
import hashlib,hmac,json,re,secrets,time
from .feedback_native_receipts import NativeReceiptStore,fresh,wire
from .feedback_source_runtime import decode

SCOPE={'event_id','dependency_id','source_report_id','contract_revision','context','capsule_ref','capsule_sha256','source_times','pins'}
PINS={'producer','queries','runtime'}
CORE={'schema','event_id','dependency_id','source_report_id','contract_revision','nonce','capsule_sha256','db_observed_at','history','db_identity','producer_version','producer_sha256','queries_sha256'}
def hex64(v):return isinstance(v,str) and re.fullmatch('[a-f0-9]{64}',v) is not None
def digest(raw):return hashlib.sha256(raw).hexdigest()

class HistoryReceiptStore:
    def __init__(self,directory,pins,snapshot_keys,validator,clock=time.time):
        if set(pins)!=PINS or not all(hex64(v) for v in pins.values()):raise ValueError('fixed history producer/query/runtime pins required')
        if not set(snapshot_keys)>=CORE or not callable(validator):raise ValueError('fixed history schema validator required')
        self.pins=dict(pins);self.keys=set(snapshot_keys);self.validator=validator
        self.store=NativeReceiptStore(directory,clock);self.db=self.store.db;self.clock=clock
        self.db.execute('CREATE TABLE IF NOT EXISTS history_receipts(nonce TEXT PRIMARY KEY,scope TEXT NOT NULL,created REAL NOT NULL,payload TEXT,used INTEGER NOT NULL DEFAULT 0)');self.db.commit()
    def close(self):self.store.close()
    def sign(self,raw):return hmac.new(self.store.key,b'canonical-readonly-history-v1\0'+raw.encode(),hashlib.sha256).hexdigest()
    def scope(self,value,current,source_current):
        if (not isinstance(value,dict) or set(value)!=SCOPE or value['pins']!=self.pins
            or not hex64(value['event_id']) or not hex64(value['contract_revision']) or not hex64(value['capsule_sha256'])
            or type(value['source_report_id']) is not int or value['source_report_id']<=0
            or not isinstance(value['dependency_id'],str) or re.fullmatch('[a-z][a-z0-9_]{0,100}',value['dependency_id']) is None
            or not isinstance(value['context'],dict) or not isinstance(value['source_times'],list) or not 1<=len(value['source_times'])<=8
            or not all(fresh(t,self.clock()) for t in value['source_times'])):raise ValueError('private history scope')
        ref=value['capsule_ref']
        if not isinstance(ref,str) or re.fullmatch(r'urn:sasocq:private-canonical:[a-f0-9]{64}\.'+value['capsule_sha256'],ref) is None:
            raise ValueError('private history source reference')
        if not callable(current) or current(value['context']) is not True or not callable(source_current) or source_current(value) is not True:
            raise ValueError('private history context/source capsule changed')
        raw=wire(value)
        if len(raw)>8000:raise ValueError('private history scope capacity')
        return raw
    def issue(self,scope,current,source_current):
        raw=self.scope(scope,current,source_current);nonce=secrets.token_hex(32)
        with self.db:self.db.execute('INSERT INTO history_receipts(nonce,scope,created) VALUES(?,?,?)',(nonce,raw,self.clock()))
        return nonce
    def seal(self,nonce,snapshot,current,source_current,transport_current):
        row=self.db.execute('SELECT scope,created,payload,used FROM history_receipts WHERE nonce=?',(nonce,)).fetchone();now=self.clock()
        if not row or row[2] or row[3] or not 0<=now-row[1]<=60:raise ValueError('private history challenge expired/replayed')
        scope=json.loads(row[0]);self.scope(scope,current,source_current)
        value=decode(snapshot,2065536)
        if not isinstance(value,dict) or set(value)!=self.keys or value.get('schema')!='nc-private-history-snapshot-v3':raise ValueError('private history snapshot schema')
        for k in ['event_id','dependency_id','source_report_id','contract_revision','capsule_sha256']:
            if value.get(k)!=scope[k] or type(value.get(k)) is not type(scope[k]):raise ValueError('private history snapshot scope')
        at=value['db_observed_at'];identity=value['db_identity']
        if (value['nonce']!=nonce or value['producer_version']!='nc-private-history-v3'
            or value['producer_sha256']!=self.pins['producer'] or value['queries_sha256']!=self.pins['queries']
            or not fresh(at,now) or not row[1]<=at<=now or not isinstance(value['history'],dict)
            or type(value['history'].get('observed_at')) is not type(at) or value['history']['observed_at']!=at
            or not isinstance(identity,dict) or identity.get('role')!='nc_history_select_v3'
            or identity.get('session_role')!='nc_history_select_v3' or identity.get('readonly')!='on'
            or identity.get('isolation')!='repeatable read'):
            raise ValueError('private history producer/identity/time')
        for k in ['superuser','create_role','create_db','replication','bypass_rls','inherits','schema_create','other_table_writes']:
            if identity.get(k) is not False:raise ValueError('private history privileged identity')
        sha=digest(snapshot)
        # Full structural limits, exact privileges and history coverage are
        # validated by reviewed fixed code; transport callback must check actual
        # SSH target and producer pins before/after the original collection.
        if self.validator(value) is not True or not callable(transport_current) or transport_current(scope,sha) is not True:
            raise ValueError('private history transport/schema not verified')
        payload=wire(dict(nonce=nonce,scope=scope,snapshot_sha256=sha,db_observed_at=at,observed_at=min([at]+scope['source_times'])))
        with self.db:
            if self.db.execute('UPDATE history_receipts SET payload=? WHERE nonce=? AND payload IS NULL AND used=0',(payload,nonce)).rowcount!=1:
                raise ValueError('private history sealing race')
        return dict(payload=payload,signature=self.sign(payload))
    def consume(self,receipt,scope,snapshot,current,source_current):
        wanted=self.scope(scope,current,source_current)
        if not isinstance(receipt,dict) or set(receipt)!={'payload','signature'} or not isinstance(receipt['payload'],str) or len(receipt['payload'])>12000:
            raise ValueError('private history receipt envelope')
        raw=receipt['payload']
        if not isinstance(receipt['signature'],str) or not hmac.compare_digest(self.sign(raw),receipt['signature']):raise ValueError('private history receipt signature')
        value=json.loads(raw);now=self.clock()
        if (wire(value['scope'])!=wanted or not isinstance(snapshot,bytes) or len(snapshot)>2065536
            or digest(snapshot)!=value['snapshot_sha256'] or not fresh(value['db_observed_at'],now) or not fresh(value['observed_at'],now)):
            raise ValueError('private history receipt binding/age')
        with self.db:
            if self.db.execute('UPDATE history_receipts SET used=1 WHERE nonce=? AND scope=? AND payload=? AND used=0',
                (value['nonce'],wanted,raw)).rowcount!=1:raise ValueError('private history replay/unknown receipt')
        return dict(evidence_ref='urn:sasocq:private-history-receipt:'+value['nonce'],snapshot_sha256=value['snapshot_sha256'],
            observed_at=value['observed_at'],db_observed_at=value['db_observed_at'])
