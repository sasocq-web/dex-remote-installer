"""System-private durable processing handles, never source attestations.

Transitions are signed and CAS guarded. Recovery may stop/read/ack an existing
job but has no API for replaying create/start or refreshing source timestamps.
"""
import hashlib,hmac,json,re,time
from .feedback_native_receipts import NativeReceiptStore,wire

STATES={'prepared','attempted','finished','ack_pending','released','abandoned'}
def hexn(v,n):return isinstance(v,str) and re.fullmatch('[a-f0-9]{'+str(n)+'}',v) is not None
def capsule_parts(reference):
    m=re.fullmatch(r'urn:sasocq:private-canonical:([a-f0-9]{64})\.([a-f0-9]{64})',reference) if isinstance(reference,str) else None
    if not m:raise ValueError('private job capsule reference')
    return m[1],m[2]

class ProcessingJobStore:
    def __init__(self,directory,clock=time.time):
        self.store=NativeReceiptStore(directory,clock);self.db=self.store.db;self.clock=clock
        self.db.execute('CREATE TABLE IF NOT EXISTS processing_jobs(token TEXT PRIMARY KEY,payload TEXT NOT NULL,signature TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS processing_job_events(seq INTEGER PRIMARY KEY,token TEXT NOT NULL,payload TEXT NOT NULL,signature TEXT NOT NULL)');self.db.commit()
    def close(self):self.store.close()
    def sign(self,raw):return hmac.new(self.store.key,b'canonical-processing-job-v1\0'+raw.encode(),hashlib.sha256).hexdigest()
    def get(self,token):
        if not hexn(token,32):raise ValueError('private job token')
        row=self.db.execute('SELECT payload,signature FROM processing_jobs WHERE token=?',(token,)).fetchone()
        if not row or not hmac.compare_digest(self.sign(row[0]),row[1]):raise ValueError('private job record signature')
        value=json.loads(row[0])
        if value.get('token')!=token or value.get('state') not in STATES:raise ValueError('private job record binding')
        return value
    def prepare(self,token,context,pins,stage,raw):
        if (not hexn(token,32) or stage!='normalize' or not isinstance(context,dict)
            or not isinstance(raw,bytes) or len(raw)>16065536 or set(pins)!={'image','helper','worker'}
            or not pins['image'].startswith('sha256:') or not hexn(pins['image'][7:],64)
            or not hexn(pins['helper'],64) or not hexn(pins['worker'],64)):
            raise ValueError('private job preparation binding')
        scope=hashlib.sha256(json.dumps(context,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        nonce=None;capture_sha=None
        try:
            envelope=json.loads(raw);ref=envelope.get('source_provenance',{}).get('receipt_ref')
            m=re.fullmatch(r'urn:sasocq:private-source-receipt:([a-f0-9]{64})',ref) if isinstance(ref,str) else None
            if m:nonce=m[1]
            if hexn(envelope.get('capture_sha256'),64):capture_sha=envelope['capture_sha256']
        except (ValueError,AttributeError,UnicodeError):pass
        value=dict(token=token,context=context,scope=scope,pins=pins,stage=stage,source_nonce=nonce,
            input_sha256=hashlib.sha256(raw).hexdigest(),input_bytes=len(raw),capture_sha256=capture_sha,state='prepared',version=0,
            prepared_at=self.clock(),output_sha256=None,output_bytes=None,capsule_sha256=None,consumer_ref=None)
        payload=wire(value);sig=self.sign(payload)
        with self.db:
            self.db.execute('INSERT INTO processing_jobs VALUES(?,?,?)',(token,payload,sig))
            self.db.execute('INSERT INTO processing_job_events(token,payload,signature) VALUES(?,?,?)',(token,payload,sig))
        return value
    def change(self,token,expected,changes):
        old=self.get(token)
        if old['state'] not in expected:raise ValueError('private job phase CAS')
        if set(changes)-{'state','output_sha256','output_bytes','capsule_sha256','consumer_ref'}:raise ValueError('private job immutable fields')
        value=dict(old,**changes,version=old['version']+1)
        if value['state'] not in STATES:raise ValueError('private job phase')
        if (old['state'],value['state']) not in {('prepared','attempted'),('attempted','finished'),('finished','ack_pending'),('ack_pending','released'),('prepared','abandoned'),('attempted','abandoned')}:
            raise ValueError('private job phase order')
        raw=wire(value);sig=self.sign(raw)
        with self.db:
            n=self.db.execute('UPDATE processing_jobs SET payload=?,signature=? WHERE token=? AND payload=?',
                (raw,sig,token,wire(old))).rowcount
            if n!=1:raise ValueError('private job transition race')
            self.db.execute('INSERT INTO processing_job_events(token,payload,signature) VALUES(?,?,?)',(token,raw,sig))
        return value
    def attempted(self,token):return self.change(token,{'prepared'},dict(state='attempted'))
    def finished(self,token,sha,n,capsha=None):
        if not hexn(sha,64) or type(n) is not int or not 0<n<=4065536 or (capsha is not None and not hexn(capsha,64)):
            raise ValueError('private processing output descriptor')
        return self.change(token,{'attempted'},dict(state='finished',output_sha256=sha,output_bytes=n,capsule_sha256=capsha))
    def pending(self):
        return [self.get(r[0]) for r in self.db.execute('SELECT token FROM processing_jobs').fetchall() if self.get(r[0])['state'] not in {'released','abandoned'}]
    def find(self,reference):
        nonce,sha=capsule_parts(reference)
        rows=[r for r in self.pending() if r['source_nonce']==nonce and r['capsule_sha256']==sha]
        if len(rows)!=1:raise ValueError('private consumer processing job not unique')
        return rows[0]
    def commit(self,token,reference,verified_commit):
        row=self.get(token);nonce,sha=capsule_parts(reference)
        if (row['source_nonce']!=nonce or row['capsule_sha256']!=sha or row['state'] not in {'finished','ack_pending'}
            or not callable(verified_commit) or verified_commit(reference,row) is not True):
            raise ValueError('private capsule durable commit not verified')
        if row['state']=='ack_pending':
            if row['consumer_ref']!=reference:raise ValueError('private consumer conflict')
            return row
        return self.change(token,{'finished'},dict(state='ack_pending',consumer_ref=reference))
    def released(self,token):return self.change(token,{'ack_pending'},dict(state='released'))
    def abandoned(self,token):
        row=self.get(token)
        if row['output_sha256'] is not None or row['capsule_sha256'] is not None or row['consumer_ref'] is not None:raise ValueError('private job accepted/output cannot be abandoned')
        return self.change(token,{'prepared','attempted'},dict(state='abandoned'))
    async def reconcile(self,pins,existing_job,verified_commit,cancel_compatible=None):
        # existing_job is a fixed System broker adapter accepting only persisted
        # token/scope and status/cancel/ack. No public caller or launch capability.
        outcomes=[]
        for row in self.pending():
            token=row['token']
            if row['pins']!=pins:
                # Only an explicitly reviewed same-helper cancel-only mapping
                # may retire an unaccepted historical attempt across code pins.
                if (row['state'] not in {'prepared','attempted'} or row['output_sha256'] is not None or row['capsule_sha256'] is not None or row['consumer_ref'] is not None
                    or not callable(cancel_compatible) or cancel_compatible(row,pins) is not True):
                    outcomes.append(dict(token=token,state='pin_mismatch'));continue
                cleanup=await existing_job(row,'cancel')
                if cleanup.get('inactive') is not True:outcomes.append(dict(token=token,state='cleanup_pending'));continue
                self.abandoned(token);outcomes.append(dict(token=token,state='abandoned_preserved'));continue
            cleanup=await existing_job(row,'cancel')
            if cleanup.get('inactive') is not True:outcomes.append(dict(token=token,state='cleanup_pending'));continue
            if row['state'] not in {'finished','ack_pending'}:
                self.abandoned(token);outcomes.append(dict(token=token,state='abandoned_preserved'));continue
            if not row['source_nonce'] or not row['capsule_sha256']:
                outcomes.append(dict(token=token,state='preserved_unaccepted'));continue
            reference='urn:sasocq:private-canonical:'+row['source_nonce']+'.'+row['capsule_sha256']
            try:row=self.commit(token,reference,verified_commit)
            except ValueError:outcomes.append(dict(token=token,state='preserved_unverified_commit'));continue
            result=await existing_job(row,'ack')
            if result.get('released') is True:self.released(token);state='released'
            else:state='ack_pending'
            outcomes.append(dict(token=token,state=state))
        return outcomes
