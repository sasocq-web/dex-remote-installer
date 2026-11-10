"""Update-only safety policy. No system mutation, secrets or model output."""
import json,os,time,uuid,hashlib,tempfile
from pathlib import Path
from urllib.parse import urlsplit
ACTIVE={"applying","verifying","rollback"}
def proof_scope(proof):
 if not all(proof.get(k)=="pass" for k in ("browser","inference","thread_start")):return None
 if proof.get("public")=="pass":return "authenticated-public"
 boundary=proof.get("public_boundary") or {}
 if not isinstance(boundary,dict):return None
 login=urlsplit(str(boundary.get("login_origin") or ""))
 if (proof.get("public")=="authentication-required" and boundary.get("origin")=="https://dex.sasocq.com"
     and boundary.get("kind")=="cloudflare-access" and login.scheme=="https" and not login.username
     and not login.password and not login.path and not login.query and not login.fragment
     and (login.hostname or "").endswith(".cloudflareaccess.com")):
  return "local-and-protected-edge"
 return None
def compatible_proof(proof,baseline):
 scope=proof_scope(proof)
 if scope=="authenticated-public":return True
 previous=baseline.get("proof") or {}
 return bool(scope=="local-and-protected-edge" and proof_scope(previous)==scope
             and proof.get("public_boundary")==previous.get("public_boundary"))
def read(path):
 try:
  v=json.loads(path.read_text())
  return v if isinstance(v,dict) else {}
 except FileNotFoundError:return {}
def atomic(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 fd,name=tempfile.mkstemp(prefix="."+path.name+".",suffix=".tmp",dir=path.parent)
 tmp=Path(name)
 try:
  with os.fdopen(fd,"w") as f:
   json.dump(value,f,sort_keys=True);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  tmp.unlink(missing_ok=True)
 fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
def verdict(transaction,signals,current,now=None):
 now=time.time() if now is None else now
 if transaction.get("phase") not in ACTIVE:return "observe-only"
 if current!=transaction.get("candidate") or current==transaction.get("previous"):return "identity-mismatch"
 if not transaction.get("baseline_verified") or not transaction.get("id"):return "unproven-baseline"
 if transaction.get("rollback_attempted"):return "terminal-no-retry"
 if signals.get("transaction_id")!=transaction["id"] or signals.get("release")!=current:return "stale-evidence"
 if not isinstance(signals.get("checked_at"),(int,float)) or not transaction["started_at"]<=signals["checked_at"]<=now or now-signals["checked_at"]>120:return "stale-evidence"
 if compatible_proof(signals,transaction.get("baseline") or {}):return "accept"
 if signals.get("local_failure_confirmations",0)>=3 and signals.get("failure_class")=="local-regression":return "rollback"
 return "hold"
def begin(previous,candidate,baseline):
 if previous==candidate or baseline.get("release")!=previous or baseline.get("status")!="verified":raise ValueError("baseline_not_verified")
 now=time.time()
 return {"schema":1,"id":uuid.uuid4().hex,"previous":previous,"candidate":candidate,"started_at":now,"deadline":now+600,"phase":"applying","baseline_verified":True,"rollback_attempted":False,"baseline":baseline,"environment_pins":environment_pins()}
def maintenance(transaction,current):
 return transaction.get("phase") in ACTIVE and current in (transaction.get("candidate"),transaction.get("previous"))


def environment_pins():
    fixed=[Path("/usr/local/sbin/sasocq-ensure-gnome-codex"),Path("/usr/lib/codex-linux-control/native-root-bundle-bootstrap"),Path("/usr/lib/systemd/user/codex-linux-control-backend.service")]
    directories=[Path("/home/codex/.config/systemd/user/codex-linux-control-backend.service.d"),Path("/etc/systemd/user/codex-linux-control-backend.service.d")]
    for directory in directories:
        fixed.extend(sorted(directory.glob("*.conf")))
    return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in fixed}
