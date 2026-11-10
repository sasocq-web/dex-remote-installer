"""Private System receipt verifier. Transport/lease integration is deliberately external.

No public tool/HTTP action accepts facts for sealing. Only a trusted native collector
may call seal after it observes the actual device under the lifecycle lock.
"""
import hashlib,hmac,json,math,os,secrets,sqlite3,stat,time
from pathlib import Path

SCOPE={'workspace','thread','turn','lease','event_id','dependency_id','contract_revision','source_report_id','device','origin','code_sha256'}
FACTS={'authenticated','allowed','level','scope_revision','observed_at'}
IDENTITY={'schema','active_account','account_id_matches','tenant_matches','object_matches','email_matches','owner_identity_matches','identity_revision','observed_at','session_module_account_matches'}
def wire(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def fresh(at,now):return type(at) in (int,float) and math.isfinite(at) and 0<=now-at<=300

def protected(path):
    if path.is_symlink():raise ValueError('receipt symlink')
    st=path.stat()
    if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)&0o077:raise ValueError('receipt permissions')

class NativeReceiptStore:
    def __init__(self,directory,clock=time.time):
        self.directory=Path(directory);self.clock=clock
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True);protected(self.directory)
        key=self.directory/'signing.key'
        try:
            fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:pass
        else:
            with os.fdopen(fd,'wb') as f:f.write(secrets.token_bytes(32));f.flush();os.fsync(f.fileno())
        protected(key);self.key=key.read_bytes()
        if len(self.key)!=32:raise ValueError('receipt key')
        path=self.directory/'receipts.sqlite3'
        if path.exists():protected(path)
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600);os.close(fd);protected(path)
        self.db=sqlite3.connect(path);self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS receipts(nonce TEXT PRIMARY KEY,scope TEXT NOT NULL,created REAL NOT NULL,payload TEXT,used INTEGER NOT NULL DEFAULT 0)');self.db.commit()
    def close(self):self.db.close()
    @staticmethod
    def scope(value):
        if not isinstance(value,dict) or set(value)!=SCOPE:raise ValueError('receipt scope')
        if any(not isinstance(value[k],str) or not 1<=len(value[k])<=300 for k in SCOPE-{'source_report_id'}):raise ValueError('receipt scope values')
        if type(value['source_report_id']) is not int or value['source_report_id']<=0:raise ValueError('receipt report')
        if value['device'] not in {'android','real_browser'} or value['origin']!='https://nc.sasocq.com':raise ValueError('receipt origin/device')
        return wire(value)
    def issue(self,scope):
        raw=self.scope(scope);nonce=secrets.token_hex(32)
        self.db.execute('INSERT INTO receipts(nonce,scope,created) VALUES(?,?,?)',(nonce,raw,self.clock()));self.db.commit()
        return nonce
    def seal(self,nonce,facts,collected_at):
        row=self.db.execute('SELECT scope,created,payload,used FROM receipts WHERE nonce=?',(nonce,)).fetchone()
        now=self.clock()
        if not row or row[2] is not None or row[3] or not fresh(row[1],now) or not fresh(collected_at,now):raise ValueError('receipt challenge')
        if not isinstance(facts,dict) or set(facts) not in (FACTS,FACTS|{'identity'}):raise ValueError('receipt facts')
        if type(facts['authenticated']) is not bool or type(facts['allowed']) is not bool or type(facts['level']) is not int or not 0<=facts['level']<=9:raise ValueError('receipt facts values')
        if not isinstance(facts['scope_revision'],str) or len(facts['scope_revision'])!=64 or any(c not in '0123456789abcdef' for c in facts['scope_revision']):raise ValueError('receipt scope revision')
        if not fresh(facts['observed_at'],now) or facts['observed_at']<row[1] or collected_at<row[1]:raise ValueError('receipt collection age')
        identity=facts.get('identity')
        if identity is not None:
            if not isinstance(identity,dict) or set(identity)!=IDENTITY:raise ValueError('receipt identity fields')
            flags=IDENTITY-{'schema','identity_revision','observed_at'}
            revision=identity['identity_revision']
            if (identity['schema']!='nc-owner-identity-v1' or any(type(identity[k]) is not bool for k in flags)
                or not isinstance(revision,str) or len(revision)!=64 or any(c not in '0123456789abcdef' for c in revision)
                or not fresh(identity['observed_at'],now) or identity['observed_at']<row[1]):raise ValueError('receipt identity values')
        payload=wire({'nonce':nonce,'scope':json.loads(row[0]),'collected_at':collected_at,'result':facts})
        with self.db:
            changed=self.db.execute('UPDATE receipts SET payload=? WHERE nonce=? AND payload IS NULL AND used=0',(payload,nonce)).rowcount
            if changed!=1:raise ValueError('receipt sealing race')
        return {'payload':payload,'signature':hmac.new(self.key,payload.encode(),hashlib.sha256).hexdigest()}
    def consume(self,receipt,expected,lease_is_current):
        # Callback is owned by the native lifecycle, never deserialized from a tool.
        wanted=self.scope(expected)
        if not callable(lease_is_current) or not lease_is_current(expected):raise ValueError('native lease no longer current')
        if not isinstance(receipt,dict) or set(receipt)!={'payload','signature'}:raise ValueError('receipt envelope')
        payload,signature=receipt['payload'],receipt['signature']
        if not isinstance(payload,str) or len(payload)>6000 or not isinstance(signature,str):raise ValueError('receipt size')
        if not hmac.compare_digest(hmac.new(self.key,payload.encode(),hashlib.sha256).hexdigest(),signature):raise ValueError('receipt signature')
        value=json.loads(payload)
        if wire(value['scope'])!=wanted:raise ValueError('receipt binding')
        now=self.clock()
        if not fresh(value['collected_at'],now) or not fresh(value['result']['observed_at'],now):raise ValueError('receipt expired')
        stamps=[value['collected_at'],value['result']['observed_at']]
        if isinstance(value['result'].get('identity'),dict):
            stamps.append(value['result']['identity']['observed_at'])
        if not all(fresh(t,now) for t in stamps):raise ValueError('receipt identity expired')
        with self.db:
            count=self.db.execute('UPDATE receipts SET used=1 WHERE nonce=? AND scope=? AND payload=? AND used=0',(value['nonce'],wanted,payload)).rowcount
            if count!=1:raise ValueError('receipt replay or unknown')
        return {'collected_at':min(stamps),
                'result':value['result'],'revision':hashlib.sha256(payload.encode()).hexdigest(),
                'evidence_ref':'urn:sasocq:native-receipt:'+value['nonce']}
