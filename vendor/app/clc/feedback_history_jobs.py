"""Durable private history operations. Crash recovery never executes them again."""
import hashlib,hmac,json,re,time
from .feedback_native_receipts import NativeReceiptStore,wire
from .feedback_history_receipts import SCOPE,PINS,hex64

STAGES={'history-query','evaluate-history'}
STATES={'prepared','attempted','finished','abandoned'}

class HistoryOperationStore:
    def __init__(self,directory,clock=time.time):
        self.store=NativeReceiptStore(directory,clock);self.db=self.store.db;self.clock=clock
        self.db.execute('CREATE TABLE IF NOT EXISTS history_operations(token TEXT PRIMARY KEY,stage TEXT NOT NULL,nonce TEXT NOT NULL,payload TEXT NOT NULL,signature TEXT NOT NULL,UNIQUE(stage,nonce))')
        self.db.execute('CREATE TABLE IF NOT EXISTS history_operation_events(seq INTEGER PRIMARY KEY,token TEXT NOT NULL,payload TEXT NOT NULL,signature TEXT NOT NULL)');self.db.commit()
    def close(self):self.store.close()
    def sign(self,raw):return hmac.new(self.store.key,b'canonical-history-operation-v1\0'+raw.encode(),hashlib.sha256).hexdigest()
    def get(self,token):
        if type(token) is not str or re.fullmatch('[a-f0-9]{32}',token) is None:raise ValueError('history operation token')
        row=self.db.execute('SELECT payload,signature,stage,nonce FROM history_operations WHERE token=?',(token,)).fetchone()
        if not row or not hmac.compare_digest(self.sign(row[0]),row[1]):raise ValueError('history operation signature')
        value=json.loads(row[0])
        if value['token']!=token or value['stage']!=row[2] or value['nonce']!=row[3] or value['state'] not in STATES:raise ValueError('history operation binding')
        return value
    def prepare(self,stage,nonce,scope,raw):
        if (stage not in STAGES or not hex64(nonce) or type(scope) is not dict or set(scope)!=SCOPE
            or type(scope['pins']) is not dict or set(scope['pins'])!=PINS or not all(hex64(v) for v in scope['pins'].values())
            or type(raw) is not bytes or not 0<len(raw)<=8100000 or len(wire(scope))>8000):raise ValueError('history operation preparation')
        token=hashlib.sha256(('history-operation-v1:'+stage+':'+nonce).encode()).hexdigest()[:32]
        value=dict(token=token,stage=stage,nonce=nonce,scope=scope,input_sha256=hashlib.sha256(raw).hexdigest(),input_bytes=len(raw),
            prepared_at=self.clock(),state='prepared',version=0,output_sha256=None,output_bytes=None,transport=None)
        payload=wire(value);signature=self.sign(payload)
        with self.db:
            if self.db.execute('SELECT 1 FROM history_operations WHERE stage=? AND nonce=?',(stage,nonce)).fetchone():raise ValueError('history operation already recorded; never replay')
            self.db.execute('INSERT INTO history_operations VALUES(?,?,?,?,?)',(token,stage,nonce,payload,signature))
            self.db.execute('INSERT INTO history_operation_events(token,payload,signature) VALUES(?,?,?)',(token,payload,signature))
        return token
    def bind_transport(self,token,pins):
        old=self.get(token)
        keys={'image','helper','worker','primitive'}
        if old['stage']=='history-query':keys|={'io','route','wrapper'}
        if (old['stage'] not in STAGES or old['state'] not in {'prepared','attempted'} or type(pins) is not dict
            or set(pins)!=keys or not isinstance(pins['image'],str)
            or not pins['image'].startswith('sha256:') or not hex64(pins['image'][7:])
            or not all(hex64(pins[k]) for k in keys-{'image'})):raise ValueError('history transport pins')
        if old.get('transport') is not None:
            if old['transport']!=pins:raise ValueError('history transport binding changed')
            return old
        value=dict(old,transport=dict(pins),version=old['version']+1);payload=wire(value);sig=self.sign(payload)
        with self.db:
            if self.db.execute('UPDATE history_operations SET payload=?,signature=? WHERE token=? AND payload=?',(payload,sig,token,wire(old))).rowcount!=1:raise ValueError('history transport binding race')
            self.db.execute('INSERT INTO history_operation_events(token,payload,signature) VALUES(?,?,?)',(token,payload,sig))
        return value
    def change(self,token,state,output=None):
        old=self.get(token)
        if (old['state'],state) not in {('prepared','attempted'),('attempted','finished'),('prepared','abandoned'),('attempted','abandoned'),('finished','abandoned')}:
            raise ValueError('history operation phase CAS')
        value=dict(old,state=state,version=old['version']+1)
        if output is not None:
            if state!='finished' or type(output) is not bytes or not 0<len(output)<=4000000:raise ValueError('history operation output bound')
            value.update(output_sha256=hashlib.sha256(output).hexdigest(),output_bytes=len(output))
        elif state=='finished':raise ValueError('history operation output required')
        payload=wire(value);sig=self.sign(payload)
        with self.db:
            if self.db.execute('UPDATE history_operations SET payload=?,signature=? WHERE token=? AND payload=?',(payload,sig,token,wire(old))).rowcount!=1:raise ValueError('history operation concurrent mutation')
            self.db.execute('INSERT INTO history_operation_events(token,payload,signature) VALUES(?,?,?)',(token,payload,sig))
        return value
    async def reconcile(self,pins,cancel_existing):
        # The trusted adapter may inspect/cancel ONLY an exact persisted scope
        # and token. There is deliberately no create/query/start/replay action.
        out=[]
        for token, in self.db.execute('SELECT token FROM history_operations').fetchall():
            row=self.get(token)
            if row['state']=='abandoned':continue
            if row['scope']['pins']!=pins:out.append(dict(token=token,state='pin_mismatch'));continue
            result=await cancel_existing(row)
            if type(result) is not dict or result.get('inactive') is not True:
                out.append(dict(token=token,state='cleanup_pending'));continue
            self.change(token,'abandoned');out.append(dict(token=token,state='abandoned_without_replay'))
        return out
