"""Private, bounded completion evidence. Never stores command/output or replays work."""
import hashlib,json,os,re
from pathlib import Path
from .bounded_protocol import atomic,completion
ROOT=Path('/var/lib/sasocq-control/request-receipts')
FIELDS=('uid','request_id','generation','fingerprint','boot_id','unit','invocation_id','worker_cgroup')
def compact(answer,limit=1500000,*,request=None):
    raw=json.dumps(answer,ensure_ascii=False).encode()+b'\n'
    if len(raw)<=limit:return answer
    if not completion(answer, request=request):return {'ok':False,'error':'worker_result_limit'}
    result={'returncode':answer['result']['returncode'],
        'output':{'omitted':True,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}}
    original=answer['result']
    if (original.get('command_lifecycle')=='terminal_verified'
        and type(original.get('command_returncode')) is int
        and 0<=original['command_returncode']<=255
        and isinstance(original.get('command_unit'),str)
        and re.fullmatch('sasocq-admin-command-[a-f0-9]{32}[.]service',original['command_unit'])):
        result.update(command_returncode=original['command_returncode'],
                      command_lifecycle='terminal_verified',command_unit=original['command_unit'])
    return {'ok':answer['ok'],'result':result}
def scope(root=ROOT,request_id=None):
    def load(path):
        if path.is_symlink() or path.stat().st_size>65536:raise ValueError('outcome_file_invalid')
        return json.loads(path.read_bytes())
    cp=load(root/'active-dispatch.json')
    if request_id is not None and cp.get('request_id')!=request_id:raise ValueError('outcome_identity_changed')
    uid=cp['uid'];rid=cp['request_id']
    if type(uid) is not int or not re.fullmatch('[a-f0-9-]{32,36}',rid):raise ValueError('outcome_identity_invalid')
    row=load(root/(str(uid)+'-'+rid+'.json'))
    if not re.fullmatch('[a-f0-9]{64}',row.get('fingerprint','')):raise ValueError('outcome_fingerprint_invalid')
    if any(row.get(k)!=v for k,v in cp.items()):raise ValueError('outcome_checkpoint_changed')
    if row.get('worker_cgroup')!='/system.slice/'+row.get('unit',''):raise ValueError('outcome_unit_scope_invalid')
    if row.get('state')!='running':raise ValueError('outcome_not_running')
    if row.get('invocation_id')!=os.environ.get('INVOCATION_ID'):raise ValueError('outcome_invocation_changed')
    if row.get('boot_id')!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():raise ValueError('outcome_boot_changed')
    groups=Path('/proc/self/cgroup').read_text().splitlines()
    if not any(x.split(':',2)[-1]==row.get('worker_cgroup') for x in groups):raise ValueError('outcome_cgroup_changed')
    return row

def record_child(unit,root=ROOT):
    if not re.fullmatch('sasocq-admin-command-[a-f0-9]{32}[.]service',unit):raise ValueError('child_identity_invalid')
    row=scope(root)
    target=root/'worker-children';target.mkdir(mode=0o700,exist_ok=True)
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    value={k:row[k] for k in FIELDS};value['child_unit']=unit
    atomic(target/(row['fingerprint']+'.json'),value,no_replace=True)

def read_only_device_contract(request):
    long_contract=storage_long_read_only_contract(request)
    if long_contract is not None:return long_contract
    p=request.get('params',{});a=p.get('argv') if isinstance(p,dict) else None
    if (request.get('action')=='host-admin' and p.get('operation')=='exec'
        and type(a) is list and len(a)==3 and a[0] in {'smartctl','/usr/sbin/smartctl'} and a[1]=='-a'
        and type(a[2]) is str and re.fullmatch('/dev/disk/by-id/[A-Za-z0-9._-]{1,200}',a[2])):
        return {'kind':'smart_read_only_v1','device':a[2]}
    return None

def record(request,answer,root=ROOT):
    row=scope(root,request.get('request_id'))
    proof={k:row[k] for k in FIELDS}
    proof.update(schema=1,validated_completion=completion(answer, request=request))
    if proof['validated_completion']:
        proof.update(ok=answer['ok'],returncode=answer['result']['returncode'])
        result=answer['result']
        if (result.get('command_lifecycle')=='terminal_verified'
            and type(result.get('command_returncode')) is int
            and 0 <= result['command_returncode'] <= 255
            and isinstance(result.get('command_unit'),str)
            and re.fullmatch('sasocq-admin-command-[a-f0-9]{32}[.]service',result['command_unit'])):
            proof.update(command_returncode=result['command_returncode'],
                         command_lifecycle='terminal_verified',command_unit=result['command_unit'])
    else:
        reason=answer.get('error','unclassified_result')
        proof['reason']=reason if reason in {'handler_without_final_completion','worker_result_limit','worker_admission_expired'} else 'unclassified_result'
    if answer.get('exception_type') in {'RuntimeError','TimeoutError','TimeoutExpired','ValueError','TypeError','OSError','PermissionError','FileNotFoundError','OtherError'}:proof['exception_type']=answer['exception_type']
    target=root/'worker-outcomes'
    target.mkdir(mode=0o700,exist_ok=True)
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    atomic(target/(row['fingerprint']+'.json'),proof,no_replace=True)
    return proof

def checked(row,proof,unit_state,boot):
    from .identified_units import terminal_proven
    if (any(proof.get(k)!=row.get(k) for k in FIELDS) or row.get('boot_id')!=boot
        or type(proof.get('schema')) is not int or proof.get('schema')!=1 or proof.get('validated_completion') is not True
        or not terminal_proven(unit_state,row['unit'])
        or unit_state.get('InvocationID')!=row.get('invocation_id')):
        raise ValueError('outcome_not_qualified')
    answer={'ok':proof.get('ok'),'result':{'returncode':proof.get('returncode')}}
    if not completion(answer):raise ValueError('outcome_invalid_completion')
    return dict(answer,recovered_without_replay=True,output_omitted=True)


def storage_long_read_only_contract(request):
    """Only the fixed read-only qualification operation, never migration authority."""
    import math
    if type(request) is not dict or set(request)!={'request_id','action','params','admission','_storage_long_binding'}:return None
    p=request['params'];a=request['admission'];b=request['_storage_long_binding']
    if (request['action']!='host-admin' or type(p) is not dict
        or set(p)!={'operation','transaction_id','plan_sha256'} or p['operation']!='projects-storage-transaction'
        or type(a) is not dict or set(a)!={'schema','boot_id','generation','deadline_mono'}
        or type(a['schema']) is not int or a['schema']!=1
        or type(a['deadline_mono']) not in (int,float) or not math.isfinite(a['deadline_mono'])
        or type(b) is not dict or set(b)!={'schema','pid','uid','start','scope_sha256','lease_sha256'}
        or type(b['schema']) is not int or b['schema']!=1 or type(b['uid']) is not int or b['uid']!=1000
        or any(type(b[k]) is not int or b[k]<=1 for k in ('pid','start'))):return None
    for value,pattern in [(request['request_id'],'[a-f0-9]{32}'),(p['transaction_id'],'[a-f0-9]{32}'),
                          (p['plan_sha256'],'[a-f0-9]{64}'),(a['generation'],'[a-f0-9]{32}'),
                          (a['boot_id'],'[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}'),
                          (b['lease_sha256'],'[a-f0-9]{64}')]:
        if type(value) is not str or not re.fullmatch(pattern,value):return None
    scope={k:request[k] for k in ('request_id','params','admission')}
    scope_sha=hashlib.sha256(json.dumps(scope,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if b['scope_sha256']!=scope_sha:return None
    return dict(kind='storage_long_read_only_v1',transaction_id=p['transaction_id'],plan_sha256=p['plan_sha256'],
                scope_sha256=scope_sha,lease_sha256=b['lease_sha256'])

def storage_callback_digest():
    """Pin the installed read-only handler; caller cannot supply this identity."""
    import stat
    path=Path('/usr/lib/python3/dist-packages/sasocq_control/coordinator_storage_review.py')
    for parent in reversed(path.parents):
        st=parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or st.st_mode&0o022:raise ValueError('storage_callback_parent')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_nlink!=1 or st.st_mode&0o022 or st.st_size>524288:
            raise ValueError('storage_callback_metadata')
        raw=b''
        while len(raw)<=524288:
            chunk=os.read(fd,65536)
            if not chunk:break
            raw+=chunk
        after=os.fstat(fd)
        identity=lambda s:(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if len(raw)!=st.st_size or identity(st)!=identity(after) or identity(st)!=identity(path.lstat()):
            raise ValueError('storage_callback_changed')
        return hashlib.sha256(raw).hexdigest()
    finally:os.close(fd)

def record_storage_readonly_entry(request,root=ROOT):
    """Durable native root-job proof before any persistent backend hold."""
    import stat
    row=scope(root,request.get('request_id'))
    contract=storage_long_read_only_contract(request)
    if (contract is None or row.get('effect_contract')!=contract
        or contract['plan_sha256']!='4226f2b2ed80d857c8e37eb2bdbba2b5395c328231ead9653d80a3ac2ed528bc'
        or row.get('deadline_mono')!=request['admission']['deadline_mono']):
        raise ValueError('storage_readonly_entry_scope')
    callback_sha=storage_callback_digest()
    target=root/'storage-readonly-entry'
    try:target.mkdir(mode=0o700)
    except FileExistsError:pass
    st=target.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077:raise ValueError('storage_entry_directory')
    parent=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(parent)
    finally:os.close(parent)
    value={k:row[k] for k in FIELDS}
    value.update(schema=1,kind='storage_readonly_entry_v1',effect_contract=contract,
                 deadline_mono=row['deadline_mono'],callback_sha256=callback_sha)
    atomic(target/(row['fingerprint']+'.json'),value,no_replace=True)
    return value
