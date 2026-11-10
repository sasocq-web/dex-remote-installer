"""Candidate internal source receipt journal, not a public attestation API.

Only a future trusted native MCP collector may issue/seal/consume. Unit callbacks
do not attest a real browser; this module alone cannot authorize resumption.
"""
import hashlib,hmac,json,os,re,secrets,time
from .feedback_native_receipts import NativeReceiptStore,fresh,wire,protected

ORIGIN='https://nuclepgovbr-my.sharepoint.com'
POLICY='existing-IPS-owner-exact-email-v1'
CONTEXT={'workspace','thread','turn','context_id','lease_id','origin','device','account_policy'}
PINS={'collect-fixed-source.js','normalize.py','registry.json','semantic_engine.py','runtime'}

def digest(raw):return hashlib.sha256(raw).hexdigest()
def hex64(x):return isinstance(x,str) and re.fullmatch('[a-f0-9]{64}',x) is not None

class SourceReceiptStore:
    def __init__(self,directory,registry,pins,clock=time.time):
        if registry.get('origin')!=ORIGIN or registry.get('account_policy')!=POLICY:
            raise ValueError('source registry origin/account')
        if set(pins)!=PINS or not all(hex64(x) for x in pins.values()):
            raise ValueError('complete immutable parser/runtime pins required')
        # Reuse private key/file and FULL-sync SQLite setup, with separate table,
        # key directory and signature domain from Android session receipts.
        self.store=NativeReceiptStore(directory,clock=clock)
        self.db=self.store.db;self.clock=clock
        self.registry=json.loads(wire(registry));self.pins=dict(pins)
        self.db.execute('CREATE TABLE IF NOT EXISTS source_receipts(nonce TEXT PRIMARY KEY,scope TEXT NOT NULL,created REAL NOT NULL,payload TEXT,used INTEGER NOT NULL DEFAULT 0,capsule_sha256 TEXT)')
        self.db.commit()
    def close(self):self.store.close()
    def sign(self,payload):
        return hmac.new(self.store.key,b'canonical-source-receipt-v1\0'+payload.encode(),hashlib.sha256).hexdigest()
    def scope(self,request,context,context_current):
        if not isinstance(request,dict) or set(request)!={'event_id','dependency_id'}:
            raise ValueError('source IDs only')
        if not hex64(request['event_id']) or not isinstance(request['dependency_id'],str):
            raise ValueError('source IDs')
        ids=self.registry['bindings'].get(request['event_id'],{}).get(request['dependency_id'])
        contract=self.registry['contracts'].get(request['event_id'])
        if (not isinstance(ids,list) or not 1<=len(ids)<=8 or len(set(ids))!=len(ids)
            or not set(ids)<=set(self.registry['sources']) or not contract
            or any(not isinstance(i,str) or re.fullmatch('[a-f0-9-]{36}',i) is None for i in ids)
            or not hex64(contract.get('contract_revision'))
            or type(contract.get('source_report_id')) is not int or contract['source_report_id']<=0):
            raise ValueError('source binding registry')
        if contract.get('dependencies',{}).get(request['dependency_id'],{}).get('source_ids')!=ids:
            raise ValueError('source dependency contract')
        if not isinstance(context,dict) or set(context)!=CONTEXT:
            raise ValueError('source native context')
        if (context['origin']!=ORIGIN or context['device']!='managed_desktop'
            or context['account_policy']!=POLICY
            or context['workspace'] not in {'system','project:project-96921b3806e0'}
            or (context['workspace']!='system' and context['thread']!='01a0ed9a-011f-7612-a25d-13e88635606d')
            or any(not isinstance(v,str) or not 1<=len(v)<=300 for v in context.values())):
            raise ValueError('source context binding')
        if not callable(context_current) or context_current(context) is not True:
            raise ValueError('source own active native context required')
        return dict(request=request,context=context,pins=self.pins,source_ids=sorted(ids),
                    source_report_id=contract['source_report_id'],contract_revision=contract['contract_revision'])
    def issue(self,request,context,context_current):
        scope=self.scope(request,context,context_current);nonce=secrets.token_hex(32)
        with self.db:self.db.execute('INSERT INTO source_receipts(nonce,scope,created) VALUES(?,?,?)',(nonce,wire(scope),self.clock()))
        return nonce
    def seal(self,nonce,capture,context_current):
        row=self.db.execute('SELECT scope,created,payload,used FROM source_receipts WHERE nonce=?',(nonce,)).fetchone();now=self.clock()
        if not row or row[2] is not None or row[3] or not fresh(row[1],now):raise ValueError('source challenge')
        scope=json.loads(row[0])
        if self.scope(scope['request'],scope['context'],context_current)!=scope:raise ValueError('source scope changed')
        if (not isinstance(capture,dict) or capture.get('schema')!='canonical-source-capture-v1'
            or capture.get('nonce')!=nonce or any(capture.get(k)!=v for k,v in scope['request'].items())):
            raise ValueError('source capture binding')
        rows=capture.get('manifest');payloads=capture.get('payloads')
        if (not isinstance(rows,list) or len(rows)!=len(scope['source_ids'])
            or {r.get('id') for r in rows}!=set(scope['source_ids'])
            or not isinstance(payloads,dict) or set(payloads)!=set(scope['source_ids'])):
            raise ValueError('source capture coverage')
        manifest=[]
        for r in rows:
            if (not fresh(r.get('observed_at'),now) or not fresh(r.get('finished_at'),now)
                or not row[1]<=r['observed_at']<=r['finished_at']<=min(now,row[1]+120)
                or type(r.get('bytes')) is not int or not 0<r['bytes']<=20000000
                or not hex64(r.get('sha256')) or not hex64(r.get('payload_sha256'))
                or r.get('revision_stable') is not True):raise ValueError('source capture facts')
            etag=r.get('etag');match=re.fullmatch(r'"\{([a-fA-F0-9-]{36})\},[1-9][0-9]*"',etag) if isinstance(etag,str) else None
            if not match or match[1].lower()!=r['id']:raise ValueError('source ETag identity')
            if digest(json.dumps(payloads[r['id']],ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())!=r['payload_sha256']:
                raise ValueError('source payload digest')
            manifest.append({k:r[k] for k in ('id','etag','sha256','payload_sha256','bytes','observed_at','finished_at')})
        raw=json.dumps(capture,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        if len(raw)>16000000:raise ValueError('source private capture capacity')
        payload=wire(dict(nonce=nonce,scope=scope,manifest=manifest,capture_sha256=digest(raw)))
        with self.db:
            n=self.db.execute('UPDATE source_receipts SET payload=? WHERE nonce=? AND payload IS NULL AND used=0',(payload,nonce)).rowcount
            if n!=1:raise ValueError('source sealing race')
        return dict(payload=payload,signature=self.sign(payload))
    def consume(self,receipt,request,context,capture,context_current):
        wanted=self.scope(request,context,context_current)
        if not isinstance(receipt,dict) or set(receipt)!={'payload','signature'}:raise ValueError('source receipt envelope')
        raw=receipt['payload']
        if not isinstance(raw,str) or len(raw)>16000 or not isinstance(receipt['signature'],str) or not hmac.compare_digest(self.sign(raw),receipt['signature']):
            raise ValueError('source receipt signature')
        value=json.loads(raw);now=self.clock()
        if value['scope']!=wanted:raise ValueError('source receipt scope')
        if not all(fresh(r['observed_at'],now) and fresh(r['finished_at'],now) for r in value['manifest']):raise ValueError('source receipt expired')
        body=json.dumps(capture,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        if len(body)>16000000 or digest(body)!=value['capture_sha256']:raise ValueError('source receipt bytes')
        with self.db:
            n=self.db.execute('UPDATE source_receipts SET used=1 WHERE nonce=? AND scope=? AND payload=? AND used=0',(value['nonce'],wire(wanted),raw)).rowcount
            if n!=1:raise ValueError('source receipt replay or unknown')
        return dict(nonce=value['nonce'],capture_sha256=value['capture_sha256'],observed_at=min(r['observed_at'] for r in value['manifest']),evidence_ref='urn:sasocq:private-source-receipt:'+value['nonce'])
    def commit_capsule(self,verified,raw,context_current):
        # Only the pinned native normalizer path may call this with its output.
        # No public JSON operation accepts capsules or a `verified` boolean.
        if not isinstance(verified,dict) or not hex64(verified.get('nonce')):
            raise ValueError('verified source receipt required')
        row=self.db.execute('SELECT payload,used,capsule_sha256 FROM source_receipts WHERE nonce=?',(verified['nonce'],)).fetchone()
        if not row or not row[0] or row[1]!=1:raise ValueError('consumed source receipt required')
        value=json.loads(row[0]);scope=value['scope'];now=self.clock()
        if (verified.get('capture_sha256')!=value['capture_sha256']
            or verified.get('evidence_ref')!='urn:sasocq:private-source-receipt:'+value['nonce']
            or self.scope(scope['request'],scope['context'],context_current)!=scope
            or not all(fresh(r['observed_at'],now) and fresh(r['finished_at'],now) for r in value['manifest'])):
            raise ValueError('capsule source binding or age')
        if not isinstance(raw,bytes) or not 0<len(raw)<=4000000:raise ValueError('capsule capacity')
        capsule=json.loads(raw)
        if (not isinstance(capsule,dict)
            or set(capsule)!={'pdc','equipment','equipment_legacy','materials','planning_dates','manifest'}
            or not isinstance(capsule['manifest'],list)):
            raise ValueError('capsule schema')
        rows=capsule['manifest'];fields=('id','etag','sha256','payload_sha256','bytes','observed_at','finished_at')
        if len(rows)!=len(value['manifest']) or sorted([{k:r[k] for k in fields} for r in rows],key=lambda r:r['id'])!=sorted(value['manifest'],key=lambda r:r['id']):
            raise ValueError('capsule manifest not from verified capture')
        sha=digest(raw)
        if row[2] is not None and row[2]!=sha:raise ValueError('capsule receipt conflict')
        directory=self.store.directory/'capsules';directory.mkdir(mode=0o700,exist_ok=True);protected(directory)
        path=directory/(value['nonce']+'.'+sha+'.json')
        try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            protected(path)
            if path.stat().st_size>4000000 or digest(path.read_bytes())!=sha:raise ValueError('capsule recovery conflict')
        else:
            with os.fdopen(fd,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent)
            finally:os.close(parent)
        with self.db:
            self.db.execute('UPDATE source_receipts SET capsule_sha256=? WHERE nonce=? AND capsule_sha256 IS NULL',(sha,value['nonce']))
            if self.db.execute('SELECT capsule_sha256 FROM source_receipts WHERE nonce=?',(value['nonce'],)).fetchone()[0]!=sha:
                raise ValueError('capsule commit race')
        return 'urn:sasocq:private-canonical:'+value['nonce']+'.'+sha
    def load_capsule(self,reference,request,context,context_current):
        match=re.fullmatch(r'urn:sasocq:private-canonical:([a-f0-9]{64})\.([a-f0-9]{64})',reference) if isinstance(reference,str) else None
        if not match:raise ValueError('private capsule reference')
        row=self.db.execute('SELECT payload,used,capsule_sha256 FROM source_receipts WHERE nonce=?',(match[1],)).fetchone()
        if not row or not row[0] or row[1]!=1 or row[2]!=match[2]:raise ValueError('private capsule journal binding')
        value=json.loads(row[0])
        now=self.clock()
        if value['scope']!=self.scope(request,context,context_current) or not all(fresh(r['observed_at'],now) and fresh(r['finished_at'],now) for r in value['manifest']):
            raise ValueError('private capsule expired or context changed')
        path=self.store.directory/'capsules'/(match[1]+'.'+match[2]+'.json');protected(path)
        if path.stat().st_size>4000000:raise ValueError('private capsule capacity')
        raw=path.read_bytes()
        if digest(raw)!=match[2]:raise ValueError('private capsule content changed')
        return raw
    def processing_committed(self,reference,job):
        # Cleanup-only verification. A stale committed capsule may release its
        # own temporary processing copy; this NEVER renews source freshness or
        # authorizes an observation/resumption under a later context.
        match=re.fullmatch(r'urn:sasocq:private-canonical:([a-f0-9]{64})\.([a-f0-9]{64})',reference) if isinstance(reference,str) else None
        if not match or job.get('source_nonce')!=match[1] or job.get('capsule_sha256')!=match[2]:return False
        row=self.db.execute('SELECT payload,used,capsule_sha256 FROM source_receipts WHERE nonce=?',(match[1],)).fetchone()
        if not row or not row[0] or row[1]!=1 or row[2]!=match[2]:return False
        value=json.loads(row[0]);scope=value['scope']
        if (scope['context']!=job.get('context') or value['capture_sha256']!=job.get('capture_sha256')
            or scope['pins']['runtime']!=job.get('pins',{}).get('image','').removeprefix('sha256:')):return False
        path=self.store.directory/'capsules'/(match[1]+'.'+match[2]+'.json');protected(path)
        return path.stat().st_size<=4000000 and digest(path.read_bytes())==match[2]
