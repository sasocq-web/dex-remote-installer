"""Prepared persistent admission closure. Not registered or installed.
Fixed paths, original immutable lease, no timer-based reopening, no broker submit.
Terminal lookup is read-only; absent/uncertain/old-shape proof stays closed.
"""
import copy,hashlib,json,math,os,re,socket,stat,struct,subprocess,time
from pathlib import Path

BASE=Path("/home/codex/.local/share/codex-linux-control/rollout")
LEASE=BASE/"projects-storage-transaction-lease.json"
HOLD="projects-storage-backend-hold.json"
RELEASE="projects-storage-backend-release.json"
NO_HOLD_RELEASE="projects-storage-backend-no-hold-release.json"
REVOCATION="projects-storage-backend-revocation.json"
LOCKS={"update":"/home/codex/.local/share/codex-linux-control/rollout/update-transaction.lock",
       "account":"/home/codex/.local/share/codex-linux-control/onedrive-account.lock"}
class Refused(RuntimeError):pass
def refuse(reason):raise Refused("storage_closure_"+reason)
def encoded(value):return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def stamp(s):return (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
def directory(path,uid):
 fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY)
 try:
  for part in path.parts[1:]:
   nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
   st=os.fstat(fd)
   if st.st_uid not in (0,uid) or st.st_mode&0o022:refuse("parent_untrusted")
  st=os.fstat(fd)
  if st.st_uid!=uid or stat.S_IMODE(st.st_mode)!=0o700:refuse("directory_untrusted")
  return fd
 except BaseException:os.close(fd);raise
def read_at(fd,name,uid,limit=32768):
 f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
 try:
  s=os.fstat(f)
  if (not stat.S_ISREG(s.st_mode) or s.st_uid!=uid or s.st_nlink!=1
      or stat.S_IMODE(s.st_mode)!=0o600 or not 1<=s.st_size<=limit):refuse("record_metadata")
  raw=os.read(f,limit+1)
  if len(raw)>limit or stamp(os.fstat(f))!=stamp(s) or stamp(os.stat(name,dir_fd=fd,follow_symlinks=False))!=stamp(s):refuse("record_raced")
  return raw
 finally:os.close(f)

def validate_lease(raw,boot,release):
 l=json.loads(raw)
 keys={"schema","transaction_id","request_id","generation","boot_id","active_release","phase",
       "owner_pid","owner_start","descriptors","plan_sha256","created_mono","created_boottime",
       "admission_deadline_mono","execution_deadline_mono","execution_deadline_boottime"}
 if type(l) is not dict or set(l)!=keys or type(l["schema"]) is not int or l["schema"]!=2:refuse("lease_schema")
 if l["boot_id"]!=boot or l["active_release"]!=release or l["phase"]!="execution-intent":refuse("lease_context")
 if any(type(l[k]) is not str or not re.fullmatch("[a-f0-9]{32}",l[k]) for k in ("transaction_id","request_id","generation")):refuse("lease_identity")
 if type(l["plan_sha256"]) is not str or not re.fullmatch("[a-f0-9]{64}",l["plan_sha256"]):refuse("plan_identity")
 if any(type(l[k]) is not int or l[k]<=1 for k in ("owner_pid","owner_start")):refuse("owner_identity")
 if (type(l["descriptors"]) is not dict or set(l["descriptors"])!=set(LOCKS)
     or any(type(x) is not int or x<0 for x in l["descriptors"].values()) or len(set(l["descriptors"].values()))!=2):refuse("descriptor_identity")
 for k in ("created_mono","created_boottime","admission_deadline_mono","execution_deadline_mono","execution_deadline_boottime"):
  if type(l[k]) not in (int,float) or not math.isfinite(l[k]):refuse("deadline_type")
 delta=l["execution_deadline_mono"]-l["created_mono"]
 if (not 0<l["admission_deadline_mono"]-l["created_mono"]<=120 or not 0<delta<=3600
     or not math.isclose(delta,l["execution_deadline_boottime"]-l["created_boottime"],rel_tol=0,abs_tol=1e-6)
     or l["admission_deadline_mono"]>l["execution_deadline_mono"]):refuse("deadline_budget")
 return l

class NativeWitness:
 def __init__(self):
  if os.geteuid()!=1000:refuse("not_backend_identity")
 def boot(self):return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
 def mono(self):return time.monotonic()
 def boottime(self):return time.clock_gettime(time.CLOCK_BOOTTIME)
 def lease(self):
  fd=directory(BASE,1000)
  try:return read_at(fd,LEASE.name,1000,16384)
  finally:os.close(fd)
 def require(self,raw,release):
  l=validate_lease(raw,self.boot(),release)
  if self.lease()!=raw:refuse("lease_changed")
  p=Path("/proc")/str(l["owner_pid"])
  def snapshot():
   fields=(p/"stat").read_text().rsplit(")",1)[1].split()
   exe=(p/"exe").stat();python=Path("/usr/bin/python3").stat()
   if (exe.st_dev,exe.st_ino)!=(python.st_dev,python.st_ino) or exe.st_uid!=0 or exe.st_mode&0o022:refuse("owner_executable")
   return (p.stat().st_uid,int(fields[19]),(p/"cmdline").read_bytes(),(p/"cgroup").read_text().strip())
  expected=b"\0".join([b"/usr/bin/python3",b"/usr/lib/codex-linux-control/rolling-proxy",b"--listen-port",b"8787",b"--backend-port",b"8788",b""])
  group="0::/user.slice/user-1000.slice/user@1000.service/codex.slice/codex-control.slice/codex-linux-control.service"
  before=snapshot()
  if before!=(1000,l["owner_start"],expected,group):refuse("owner_kernel_identity")
  with socket.socket(socket.AF_UNIX) as sock:
   sock.settimeout(2);sock.connect("/run/user/1000/codex-linux-control-rollout.sock")
   pid,uid,_=struct.unpack("3i",sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
   if (pid,uid)!=(l["owner_pid"],1000):refuse("owner_socket_identity")
  for name,path in LOCKS.items():
   fd=l["descriptors"][name];f=p/"fd"/str(fd);a=os.stat(f);b=os.stat(path,follow_symlinks=False)
   if (not stat.S_ISREG(b.st_mode) or b.st_uid!=1000 or stat.S_IMODE(b.st_mode)!=0o600
       or b.st_nlink!=1 or (a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino)):refuse("foreign_lock_identity")
   info=(p/"fdinfo"/str(fd)).read_text()
   token=f"{l['owner_pid']} {os.major(b.st_dev):02x}:{os.minor(b.st_dev):02x}:{b.st_ino} 0 EOF"
   if not any("FLOCK  ADVISORY  WRITE" in line and token in line for line in info.splitlines()):refuse("foreign_lock_not_exclusive")
  if self.lease()!=raw or snapshot()!=before:refuse("owner_raced")
  return l

 def require_revoker(self,raw,release,revoker):
  l=validate_lease(raw,self.boot(),release)
  if (type(revoker) is not dict or set(revoker)!={'owner_pid','owner_start','descriptors'}
      or any(type(revoker[k]) is not int or revoker[k]<=1 for k in ('owner_pid','owner_start'))
      or type(revoker['descriptors']) is not dict or set(revoker['descriptors'])!=set(LOCKS)
      or any(type(v) is not int or v<0 for v in revoker['descriptors'].values())
      or len(set(revoker['descriptors'].values()))!=2):refuse('revoker_shape')
  original={k:l[k] for k in revoker}
  if original==revoker:
   self.require(raw,release);return False
  def ended():
   try:fields=(Path('/proc')/str(l['owner_pid'])/'stat').read_text().rsplit(')',1)[1].split()
   except FileNotFoundError:return True
   return int(fields[19])!=l['owner_start'] or fields[0]=='Z'
  if not ended():refuse('original_owner_still_live')
  if self.lease()!=raw:refuse('revoker_lease_changed')
  self._require_revoker_actor(revoker)
  if self.lease()!=raw or not ended():refuse('revoker_race')
  return True
 def _require_revoker_actor(self,revoker):
  p=Path('/proc')/str(revoker['owner_pid'])
  def snapshot():
   fields=(p/'stat').read_text().rsplit(')',1)[1].split()
   exe=(p/'exe').stat();trusted=Path('/usr/bin/python3').stat()
   if (exe.st_dev,exe.st_ino)!=(trusted.st_dev,trusted.st_ino) or exe.st_uid!=0 or exe.st_mode&0o022:refuse('revoker_executable')
   return (p.stat().st_uid,int(fields[19]),(p/'cmdline').read_bytes(),(p/'cgroup').read_text().strip())
  cmd=b'\0'.join([b'/usr/bin/python3',b'/usr/lib/codex-linux-control/rolling-proxy',b'--listen-port',b'8787',b'--backend-port',b'8788',b''])
  group='0::/user.slice/user-1000.slice/user@1000.service/codex.slice/codex-control.slice/codex-linux-control.service'
  before=snapshot()
  if before!=(1000,revoker['owner_start'],cmd,group):refuse('revoker_kernel_identity')
  with socket.socket(socket.AF_UNIX) as s:
   s.settimeout(2);s.connect('/run/user/1000/codex-linux-control-rollout.sock')
   pid,uid,_=struct.unpack('3i',s.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
   if (pid,uid)!=(revoker['owner_pid'],1000):refuse('revoker_socket')
  for name,path in LOCKS.items():
   fd=revoker['descriptors'][name];a=os.stat(p/'fd'/str(fd));b=os.stat(path,follow_symlinks=False)
   if (not stat.S_ISREG(b.st_mode) or b.st_uid!=1000 or stat.S_IMODE(b.st_mode)!=0o600 or b.st_nlink!=1
       or (a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino)):refuse('revoker_lock_identity')
   token=f"{revoker['owner_pid']} {os.major(b.st_dev):02x}:{os.minor(b.st_dev):02x}:{b.st_ino} 0 EOF"
   if not any('FLOCK  ADVISORY  WRITE' in line and token in line for line in (p/'fdinfo'/str(fd)).read_text().splitlines()):refuse('revoker_lock_not_exclusive')
  if snapshot()!=before:refuse('revoker_kernel_race')

class OriginalTerminalReader:
 """Read ONLY the original receipt. No UUID, admission, replay or state mutation."""
 def _lookup(self,l):
  path=Path("/run/sasocq-control/broker.sock");meta=path.lstat()
  if not stat.S_ISSOCK(meta.st_mode) or meta.st_uid!=0:refuse("receipt_socket")
  with socket.socket(socket.AF_UNIX) as s:
   s.settimeout(3);s.connect(str(path))
   _,uid,_=struct.unpack("3i",s.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
   if uid!=0:refuse("receipt_peer")
   request={"request_id":l["request_id"],"action":"broker.receipt","params":{"request_id":l["request_id"],"generation":l["generation"]}}
   s.sendall(encoded(request)+b"\n");raw=b""
   while b"\n" not in raw:
    block=s.recv(min(65536,2097153-len(raw)))
    if not block or len(raw)+len(block)>2097152:refuse("receipt_frame")
    raw+=block
  return raw.split(b"\n",1)[0]
 def verify(self,l,hold_sha,lease_sha,allow_refused=False):
  raw=self._lookup(l);value=json.loads(raw)
  if type(value) is dict and value.get('error')=='original_receipt_preserved':
   return self.verify_recovered(value,l,hold_sha,lease_sha)
  if allow_refused and type(value) is dict and value.get("error")=="original_payload_not_released":
   return self.verify_refused(value,l,hold_sha,lease_sha)
  if type(value) is not dict:refuse("receipt_envelope")
  receipt=value.get("receipt",{})
  if (type(receipt) is not dict or value.get("ok") is not True or type(receipt.get("schema")) is not int or receipt["schema"]!=2
      or type(receipt.get("uid")) is not int or receipt["uid"]!=1000 or receipt.get("state")!="completed"
      or receipt.get("request_id")!=l["request_id"] or receipt.get("generation")!=l["generation"]
      or receipt.get("boot_id")!=l["boot_id"] or type(receipt.get("deadline_mono")) not in (int,float)
      or receipt["deadline_mono"]!=l["admission_deadline_mono"]
      or receipt.get("ok") is not True or type(receipt.get("returncode")) is not int or receipt["returncode"]!=0):refuse("original_terminal_required")
  scope={"request_id":l["request_id"],"params":{"operation":"projects-storage-transaction",
       "transaction_id":l["transaction_id"],"plan_sha256":l["plan_sha256"]},
       "admission":{"schema":1,"boot_id":l["boot_id"],"generation":l["generation"],"deadline_mono":l["admission_deadline_mono"]}}
  expected_effect={"kind":"storage_long_read_only_v1","transaction_id":l["transaction_id"],
       "plan_sha256":l["plan_sha256"],"scope_sha256":sha(encoded(scope)),
       "lease_sha256":sha(encoded(l))}
  # Lease digest binds exact publication bytes, not a reserialized equivalent.
  expected_effect["lease_sha256"]=lease_sha
  if receipt.get("effect_contract")!=expected_effect:refuse("terminal_effect_contract")
  result=value.get("result")
  out=result.get("output") if type(result) is dict else None
  expected={"status":"coordinator_storage_long_terminal","transaction_id":l["transaction_id"],
      "plan_sha256":l["plan_sha256"],"request_id":l["request_id"],"generation":l["generation"],
      "boot_id":l["boot_id"],"safe_to_reopen":True}
  if type(out) is not dict or any(type(out.get(k)) is not type(v) or out[k]!=v for k,v in expected.items()):refuse("terminal_contract")
  if (out.get("migration_state")!="not-started" or out.get("review_only") is not True
      or out.get("activation_authorized") is not False or type(out.get("freed_bytes")) is not int or out["freed_bytes"]!=0):refuse("terminal_consistency")
  unit=receipt.get("unit")
  if type(unit) is not str or not re.fullmatch(r"sasocq-broker-job-[a-f0-9]{32}\.service",unit):refuse("terminal_unit")
  self._unit_terminal(unit)
  return {"schema":1,"hold_sha256":hold_sha,"request_id":l["request_id"],"generation":l["generation"],
          "receipt_sha256":sha(raw),"migration_state":out["migration_state"]}
 def _unit_terminal(self,unit):
  r=subprocess.run(["systemctl","show",unit,"-p","ActiveState","-p","MainPID","-p","ControlPID"],capture_output=True,text=True,timeout=3)
  props=dict(line.split("=",1) for line in r.stdout.splitlines() if "=" in line)
  if props.get("ActiveState") not in ("inactive","failed") or props.get("MainPID")!="0" or props.get("ControlPID")!="0":refuse("original_unit_not_terminal")
  return True


 def verify_recovered(self,value,l,hold_sha,lease_sha):
  """Typed native read-only disposition; the original receipt stays uncertain."""
  r=value.get('receipt');e=value.get('recovered_result')
  if (value.get('ok') is not False or value.get('error')!='original_receipt_preserved'
      or type(r) is not dict or type(e) is not dict or type(r.get('schema')) is not int or r['schema']!=2
      or type(r.get('uid')) is not int or r['uid']!=1000 or r.get('state') not in ('uncertain','completed')
      or any(r.get(k)!=l[k] for k in ('request_id','generation','boot_id'))
      or type(r.get('deadline_mono')) not in (int,float) or r['deadline_mono']!=l['admission_deadline_mono']):
   refuse('original_storage_recovery_required')
  scope={'request_id':l['request_id'],'params':{'operation':'projects-storage-transaction',
       'transaction_id':l['transaction_id'],'plan_sha256':l['plan_sha256']},
       'admission':{'schema':1,'boot_id':l['boot_id'],'generation':l['generation'],'deadline_mono':l['admission_deadline_mono']}}
  effect=dict(kind='storage_long_read_only_v1',transaction_id=l['transaction_id'],plan_sha256=l['plan_sha256'],
       scope_sha256=sha(encoded(scope)),lease_sha256=lease_sha)
  fields=('uid','request_id','generation','fingerprint','boot_id','unit','invocation_id','worker_cgroup')
  expected=dict(schema=1,kind='original_storage_read_only_stopped_v1',diagnostic_failed=True,
      execution_outcome_known=False,recovered_without_replay=True,original_receipt_preserved=True,
      scope={k:r.get(k) for k in fields},effect_contract=effect,deadline_mono=l['admission_deadline_mono'],
      callback_sha256=STORAGE_READONLY_CALLBACK_SHA256,safe_to_reopen=True,
      migration_state='not-started',review_only=True,activation_authorized=False,freed_bytes=0)
  if (set(e)!=set(expected)|{'entry_sha256','terminal_kind'} or any(type(e.get(k)) is not type(v) or e[k]!=v for k,v in expected.items())
      or e.get('terminal_kind') not in ('retained_original_unit','original_unit_absent_after_native_entry')
      or type(e.get('entry_sha256')) is not str or not re.fullmatch('[a-f0-9]{64}',e['entry_sha256'])
      or r.get('effect_contract')!=effect
      or type(r.get('fingerprint')) is not str or not re.fullmatch('[a-f0-9]{64}',r['fingerprint'])
      or type(r.get('invocation_id')) is not str or not re.fullmatch('[a-f0-9]{32}',r['invocation_id'])
      or type(r.get('unit')) is not str or not re.fullmatch(r'sasocq-broker-job-[a-f0-9]{32}\.service',r['unit'])
      or r.get('worker_cgroup')!='/system.slice/'+r['unit']):refuse('storage_recovery_scope_changed')
  self._unit_terminal(r['unit'])
  return dict(schema=1,hold_sha256=hold_sha,request_id=l['request_id'],generation=l['generation'],
      receipt_sha256=sha(encoded(value)),migration_state='not-started',
      native_state=r['state'],proof_kind='original_storage_read_only_stopped_v1')

 def verify_refused(self,value,l,hold_sha,lease_sha):
  """No success fabrication: native refusal + exact pre-payload proof + ended unit."""
  r=value.get('receipt');e=value.get('pre_payload_proof')
  if (type(r) is not dict or type(e) is not dict or value.get('ok') is not False
      or value.get('error')!='original_payload_not_released'
      or type(r.get('schema')) is not int or r['schema']!=2
      or type(r.get('uid')) is not int or r['uid']!=1000 or r.get('state')!='refused'
      or any(r.get(k)!=l[k] for k in ('request_id','generation','boot_id'))
      or type(r.get('deadline_mono')) not in (int,float) or r['deadline_mono']!=l['admission_deadline_mono']
      or e.get('kind')!='original_payload_not_released_v1'):refuse('original_refused_proof_required')
  p=e.get('proof')
  keys={'uid','request_id','unit','fingerprint','generation','boot_id','state','launch_invocation_id','storage_long_scope_sha256'}
  scope={'request_id':l['request_id'],'params':{'operation':'projects-storage-transaction',
       'transaction_id':l['transaction_id'],'plan_sha256':l['plan_sha256']},
       'admission':{'schema':1,'boot_id':l['boot_id'],'generation':l['generation'],'deadline_mono':l['admission_deadline_mono']}}
  if (type(p) is not dict or set(p)!=keys or p['state']!='payload_not_released'
      or type(p['uid']) is not int or p['uid']!=1000
      or any(p.get(k)!=r.get(k) for k in ('uid','request_id','generation','boot_id','fingerprint','unit'))
      or p['storage_long_scope_sha256']!=sha(encoded(scope))
      or e.get('proof_sha256')!=sha(encoded(p))
      or r.get('invocation_id')!=p['launch_invocation_id']
      or (p['launch_invocation_id'] is not None and
          (type(p['launch_invocation_id']) is not str or not re.fullmatch('[a-f0-9]{32}',p['launch_invocation_id'])))
      or type(r.get('fingerprint')) is not str or not re.fullmatch('[a-f0-9]{64}',r['fingerprint'])
      or type(r.get('unit')) is not str or not re.fullmatch(r'sasocq-broker-job-[a-f0-9]{32}\.service',r['unit'])):
   refuse('original_refused_scope_changed')
  self._unit_terminal(r['unit'])
  return {'schema':1,'hold_sha256':hold_sha,'request_id':l['request_id'],'generation':l['generation'],
      'receipt_sha256':sha(encoded(value)),'migration_state':'not-started',
      'native_state':'refused','proof_kind':'original_payload_not_released_v1'}



class OriginalRunningReader(OriginalTerminalReader):
 """Receipt must bind the read-only operation; a prepared marker is never evidence."""
 def verify(self,l,lease_sha):
  value=json.loads(self._lookup(l));r=value.get('receipt') if type(value) is dict else None
  scope={'request_id':l['request_id'],'params':{'operation':'projects-storage-transaction',
         'transaction_id':l['transaction_id'],'plan_sha256':l['plan_sha256']},
         'admission':{'schema':1,'boot_id':l['boot_id'],'generation':l['generation'],
                      'deadline_mono':l['admission_deadline_mono']}}
  effect={'kind':'storage_long_read_only_v1','transaction_id':l['transaction_id'],
          'plan_sha256':l['plan_sha256'],'scope_sha256':sha(encoded(scope)),'lease_sha256':lease_sha}
  if (type(r) is not dict or type(r.get('schema')) is not int or r['schema']!=2
      or type(r.get('uid')) is not int or r['uid']!=1000 or r.get('state')!='running'
      or any(r.get(k)!=l[k] for k in ('request_id','generation','boot_id'))
      or type(r.get('deadline_mono')) not in (int,float) or r['deadline_mono']!=l['admission_deadline_mono']
      or r.get('effect_contract')!=effect
      or type(r.get('invocation_id')) is not str or not re.fullmatch('[a-f0-9]{32}',r['invocation_id'])
      or type(r.get('unit')) is not str or not re.fullmatch(r'sasocq-broker-job-[a-f0-9]{32}\.service',r['unit'])
      or r.get('worker_cgroup')!='/system.slice/'+r['unit']):refuse('original_running_receipt_required')
  self._unit_running(r)
  return {'receipt_sha256':sha(encoded(value)),'unit':r['unit'],'read_only':True}
 def _unit_running(self,r):
  result=subprocess.run(['systemctl','show',r['unit'],'-p','ActiveState','-p','SubState','-p','MainPID','-p','ControlPID','-p','InvocationID'],capture_output=True,text=True,timeout=3)
  props=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
  if (result.returncode!=0 or props.get('ActiveState')!='active' or props.get('SubState')!='running'
      or props.get('InvocationID')!=r['invocation_id'] or props.get('ControlPID')!='0'
      or not str(props.get('MainPID','')).isdigit() or int(props['MainPID'])<=1):refuse('original_job_not_running')
  proc=Path('/proc')/props['MainPID']
  def identity():
   exe=(proc/'exe').stat();trusted=Path('/usr/bin/python3').stat()
   if (exe.st_dev,exe.st_ino)!=(trusted.st_dev,trusted.st_ino) or exe.st_uid!=0 or exe.st_mode&0o022:refuse('running_executable')
   start=int((proc/'stat').read_text().rsplit(')',1)[1].split()[19]);cmd=(proc/'cmdline').read_bytes().split(b'\0')
   if (proc.stat().st_uid!=0 or (proc/'cgroup').read_text().strip()!='0::'+r['worker_cgroup']
       or len(cmd)!=10 or cmd[:3]!=[b'/usr/bin/python3',b'-m',b'sasocq_control.broker_job']
       or cmd[3]!=b'--socket' or cmd[5]!=b'--nonce' or cmd[7]!=b'--broker-pid' or cmd[9]!=b''):refuse('running_kernel_identity')
   return start,cmd
  before=identity()
  if identity()!=before:refuse('running_kernel_race')
  return before

class PersistentClosure:
 """Caller MUST hold the existing backend turn-admission lock for close/release."""
 def __init__(self,base=BASE,uid=1000,witness=None,terminal=None,crash=None):
  self.fd=directory(Path(base),uid);self.uid=uid;self.witness=witness or NativeWitness()
  self.terminal=terminal or OriginalTerminalReader();self.running=OriginalRunningReader();self.crash=crash or (lambda point:None)
 def close(self):os.close(self.fd)
 def _read(self,name):return read_at(self.fd,name,self.uid)
 def _optional(self,name):
  try:return self._read(name)
  except FileNotFoundError:return None
 def _write_once(self,name,raw):
  f=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.fd)
  try:
   view=memoryview(raw)
   while view:
    n=os.write(f,view)
    if n<=0:refuse("short_write")
    view=view[n:]
   os.fsync(f);self.crash(name+"_file_fsync")
  finally:os.close(f)
  os.fsync(self.fd);self.crash(name+"_directory_fsync")
 def _hold(self):
  revoked=self._optional(REVOCATION)
  if revoked is not None:self._checked_revocation(revoked)
  raw=self._optional(HOLD);released=self._optional(RELEASE)
  if raw is None:
   nohold=self._optional(NO_HOLD_RELEASE)
   if nohold is not None:self._checked_no_hold(nohold)
   if released is not None:refuse("release_without_original_hold")
   return None,None,None
  h=json.loads(raw)
  if (type(h) is not dict or set(h)!={"schema","lease_hex","lease_sha256","release_id"}
      or type(h["schema"]) is not int or h["schema"]!=1 or type(h["lease_hex"]) is not str
      or len(h["lease_hex"])>32768):refuse("hold_shape")
  lease=bytes.fromhex(h["lease_hex"])
  if sha(lease)!=h["lease_sha256"]:refuse("hold_digest")
  l=validate_lease(lease,self.witness.boot(),h["release_id"])
  if released is not None:
   proof=json.loads(released)
   if (type(proof) is not dict or (set(proof)!={"schema","hold_sha256","request_id","generation","receipt_sha256","migration_state"} and not recovered_release_extension(proof))
       or type(proof["schema"]) is not int or proof["schema"]!=1 or proof["hold_sha256"]!=sha(raw)
       or proof["request_id"]!=l["request_id"] or proof["generation"]!=l["generation"]
       or not re.fullmatch("[a-f0-9]{64}",str(proof["receipt_sha256"]))
       or proof["migration_state"] not in ("not-started","validated","rolled-back")):refuse("release_proof_shape")
   f=os.open(RELEASE,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self.fd)
   try:os.fsync(f)
   finally:os.close(f)
   os.fsync(self.fd)
  return raw,lease,released
 def blocks(self):
  try:
   raw,lease,released=self._hold()
   return raw is not None and released is None
  except Exception:return True
 def generic_unquiesce_allowed(self):return not self.blocks()
 def acquire(self,locator,release,active_turns,android_quiesced):
  if (type(locator) is not dict or set(locator)!={"request_id","generation","boot_id","lease_sha256"}
      or type(active_turns) is not int or active_turns!=0 or android_quiesced is not True):refuse("turn_or_android_admission")
  if self._optional(REVOCATION) is not None:refuse("original_execution_revoked")
  if self._optional(NO_HOLD_RELEASE) is not None:refuse("original_job_already_released")
  raw=self.witness.lease();l=self.witness.require(raw,release)
  if locator!={k:l[k] for k in ("request_id","generation","boot_id")}|{"lease_sha256":sha(raw)}:refuse("locator_scope")
  e=self.witness
  if not l["created_mono"]<=e.mono()<l["admission_deadline_mono"] or not l["created_boottime"]<=e.boottime()<l["execution_deadline_boottime"]:refuse("original_entry_deadline")
  # Both receipt AND current original unit are authenticated while the caller
  # holds the backend turn lock. Never close for merely prepared/queued work.
  self.running.verify(l,sha(raw))
  previous=self._optional(HOLD)
  hold=encoded({"schema":1,"lease_hex":raw.hex(),"lease_sha256":sha(raw),"release_id":release})
  if previous is not None:
   if previous!=hold or self._optional(RELEASE) is not None:refuse("original_hold_exists")
   os.fsync(self.fd) # recover original publication, never create a new identity
  else:self._write_once(HOLD,hold)
  self.witness.require(raw,release)
  return self.health(release)
 def health(self,release):
  try:
   held,raw,released=self._hold()
   if held is None or released is not None:return None
   l=self.witness.require(raw,release);e=self.witness
   if not l["created_mono"]<=e.mono()<l["execution_deadline_mono"] or not l["created_boottime"]<=e.boottime()<l["execution_deadline_boottime"]:return None
   return dict(schema=1,request_id=l["request_id"],generation=l["generation"],boot_id=l["boot_id"],
      lease_sha256=sha(raw),execution_deadline_mono=l["execution_deadline_mono"],
      execution_deadline_boottime=l["execution_deadline_boottime"],persistent=True)
  except Exception:return None # Admission remains CLOSED; missing health cannot authorize root effects.
 def release_original(self,locator):
  held,raw,released=self._hold()
  if held is None:
   if self._optional(REVOCATION) is not None:refuse('original_execution_revoked')
   return self._release_without_hold(locator)
  l=json.loads(raw)
  if locator!={k:l[k] for k in ("request_id","generation","boot_id")}|{"lease_sha256":sha(raw)}:refuse("release_scope")
  if released is not None:return {"released":True,"redispatched":False}
  # Reader must provide authenticated original root receipt AND terminal kernel unit.
  proof=self.terminal.verify(l,sha(held),sha(raw))
  expected={"schema","hold_sha256","request_id","generation","receipt_sha256","migration_state"}
  if (type(proof) is not dict or (set(proof)!=expected and not recovered_release_extension(proof)) or type(proof["schema"]) is not int or proof["schema"]!=1
      or proof["hold_sha256"]!=sha(held) or proof["request_id"]!=l["request_id"] or proof["generation"]!=l["generation"]
      or not re.fullmatch("[a-f0-9]{64}",str(proof["receipt_sha256"]))
      or proof["migration_state"] not in ("not-started","validated","rolled-back")):refuse("terminal_verifier_contract")
  if self._read(HOLD)!=held:refuse("hold_changed_before_release")
  self.crash("terminal_verified_before_release")
  self._write_once(RELEASE,encoded(proof))
  if self.blocks():refuse("release_readback_required")
  return {"released":True,"redispatched":False}

 def _checked_no_hold(self,raw):
  value=json.loads(raw)
  if type(value) is not dict or set(value)!={'schema','lease_hex','proof'} or type(value['schema']) is not int or value['schema']!=1:refuse('no_hold_record_shape')
  lease=bytes.fromhex(value['lease_hex']);l=json.loads(lease)
  validate_lease(lease,self.witness.boot(),l['active_release'])
  expected_sha=sha(b'no-hold:'+lease)
  proof=value['proof']
  base={'schema','hold_sha256','request_id','generation','receipt_sha256','migration_state'}
  refused_shape=type(proof) is dict and set(proof)==base|{'native_state','proof_kind'} and proof['native_state']=='refused' and proof['proof_kind']=='original_payload_not_released_v1'
  if (type(proof) is not dict or (set(proof)!=base and not refused_shape and not recovered_release_extension(proof))
      or type(proof['schema']) is not int or proof['schema']!=1 or proof['hold_sha256']!=expected_sha
      or proof['request_id']!=l['request_id'] or proof['generation']!=l['generation']
      or type(proof['receipt_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',proof['receipt_sha256'])
      or proof['migration_state']!='not-started'):refuse('no_hold_terminal_shape')
  f=os.open(NO_HOLD_RELEASE,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self.fd)
  try:os.fsync(f)
  finally:os.close(f)
  os.fsync(self.fd)
  return value,l
 def _release_without_hold(self,locator):
  # No absence-based inference: a real original terminal receipt and unit are
  # mandatory even when quiesce was refused before publishing any hold.
  raw=self.witness.lease();l=json.loads(raw)
  validate_lease(raw,self.witness.boot(),l['active_release'])
  if locator!={k:l[k] for k in ('request_id','generation','boot_id')}|{'lease_sha256':sha(raw)}:refuse('release_scope')
  existing=self._optional(NO_HOLD_RELEASE)
  if existing is not None:
   value,original=self._checked_no_hold(existing)
   if bytes.fromhex(value['lease_hex'])!=raw:refuse('no_hold_scope_changed')
   return {'released':True,'redispatched':False,'hold_created':False}
  proof=self.terminal.verify(l,sha(b'no-hold:'+raw),sha(raw),allow_refused=True)
  value={'schema':1,'lease_hex':raw.hex(),'proof':proof}
  # Same turn-admission lock serializes this with acquire; acquire additionally
  # refuses terminal units, so a delayed POST cannot publish a late hold.
  if self._optional(HOLD) is not None:refuse('hold_appeared_before_no_hold_release')
  self._write_once(NO_HOLD_RELEASE,encoded(value))
  self._checked_no_hold(self._read(NO_HOLD_RELEASE))
  return {'released':True,'redispatched':False,'hold_created':False}

 def _checked_revocation(self,raw):
  value=json.loads(raw)
  keys={'schema','kind','lease_hex','lease_sha256','revoker','original_owner_ended','execution_authorized','receipt_state_changed'}
  if (type(value) is not dict or set(value)!=keys or type(value['schema']) is not int or value['schema']!=1
      or value['kind']!='storage_execution_revoked_before_hold_v1' or value['execution_authorized'] is not False
      or value['receipt_state_changed'] is not False or type(value['original_owner_ended']) is not bool
      or type(value['lease_hex']) is not str or len(value['lease_hex'])>32768):refuse('revocation_shape')
  lease=bytes.fromhex(value['lease_hex']);l=json.loads(lease)
  validate_lease(lease,self.witness.boot(),l['active_release'])
  if sha(lease)!=value['lease_sha256'] or self.witness.lease()!=lease:refuse('revocation_lease_changed')
  r=value['revoker']
  if (type(r) is not dict or set(r)!={'owner_pid','owner_start','descriptors'}
      or any(type(r[k]) is not int or r[k]<=1 for k in ('owner_pid','owner_start'))
      or type(r['descriptors']) is not dict or set(r['descriptors'])!=set(LOCKS)
      or any(type(v) is not int or v<0 for v in r['descriptors'].values())
      or len(set(r['descriptors'].values()))!=2):refuse('revocation_actor_shape')
  if self._optional(HOLD) is not None or self._optional(RELEASE) is not None or self._optional(NO_HOLD_RELEASE) is not None:refuse('revocation_conflict')
  f=os.open(REVOCATION,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self.fd)
  try:os.fsync(f)
  finally:os.close(f)
  os.fsync(self.fd)
  return value,l
 def revocation_health(self,release):
  try:
   raw=self._optional(REVOCATION)
   if raw is None:return None
   value,l=self._checked_revocation(raw)
   if l['active_release']!=release:return None
   return dict(schema=1,kind=value['kind'],request_id=l['request_id'],generation=l['generation'],
      boot_id=l['boot_id'],plan_sha256=l['plan_sha256'],lease_sha256=value['lease_sha256'],
      revocation_sha256=sha(raw),execution_authorized=False,receipt_state_changed=False)
  except Exception:return None
 def revoke_unstarted(self,locator,release,active_turns,android_quiesced):
  # A new denial of future execution authority, NEVER a completed/no-effects
  # receipt. Existing HOLD is never reopened by this operation.
  if (type(locator) is not dict or set(locator)!={'request_id','generation','boot_id','lease_sha256','revoker'}
      or type(active_turns) is not int or active_turns!=0 or android_quiesced is not True):refuse('revocation_admission')
  if any(self._optional(n) is not None for n in (HOLD,RELEASE,NO_HOLD_RELEASE)):refuse('revocation_existing_hold_or_release')
  raw=self.witness.lease();l=validate_lease(raw,self.witness.boot(),release)
  expected={k:l[k] for k in ('request_id','generation','boot_id')}|{'lease_sha256':sha(raw)}
  if {k:v for k,v in locator.items() if k!='revoker'}!=expected:refuse('revocation_scope')
  existing=self._optional(REVOCATION)
  if existing is not None:
   value,original=self._checked_revocation(existing)
   if bytes.fromhex(value['lease_hex'])!=raw:refuse('revocation_original_changed')
   return self.revocation_health(release)
  ended=self.witness.require_revoker(raw,release,locator['revoker'])
  value=dict(schema=1,kind='storage_execution_revoked_before_hold_v1',lease_hex=raw.hex(),
    lease_sha256=sha(raw),revoker=locator['revoker'],original_owner_ended=ended,
    execution_authorized=False,receipt_state_changed=False)
  if any(self._optional(n) is not None for n in (HOLD,RELEASE,NO_HOLD_RELEASE)):refuse('revocation_raced')
  self._write_once(REVOCATION,encoded(value))
  self._checked_revocation(self._read(REVOCATION))
  return self.revocation_health(release)

STORAGE_READONLY_CALLBACK_SHA256='98db8610f73754792d5da11f0a945daab9fd7917b4ffe28ced1fdb2a4c049f6d'

def recovered_release_extension(proof):
 base={'schema','hold_sha256','request_id','generation','receipt_sha256','migration_state'}
 return (type(proof) is dict and set(proof)==base|{'native_state','proof_kind'}
    and proof.get('proof_kind')=='original_storage_read_only_stopped_v1'
    and proof.get('native_state') in ('uncertain','completed') and proof.get('migration_state')=='not-started')
