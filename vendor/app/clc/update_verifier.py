"""Periodic proofs bound to immutable release and update transaction."""
import asyncio,json,time
from pathlib import Path
from .update_safety_policy import read,atomic,maintenance,proof_scope
from .update_inference_canary import InferenceCanary
DATA=Path.home()/".local/share/codex-linux-control/rollout"
LOADED_RELEASE_ID = Path(__file__).resolve().parents[2].name
def release_id():
 return LOADED_RELEASE_ID
def gate_closed():
 try:return maintenance(read(DATA/"update-transaction.json"),release_id())
 except (OSError,ValueError):return True
class UpdateVerifier:
 def __init__(self,bridge,events,cwd,active,thread_start_error,admission_closed):
  self.admission_closed=admission_closed;self.thread_start_error=thread_start_error;self.canary=InferenceCanary(bridge,events,cwd);self.active=active;self.last={};self.task=None;self.probing=False
  self._probe_lock=asyncio.Lock()
 async def probe(self):
  if self._probe_lock.locked():return {"status":"busy"}
  async with self._probe_lock:
   return await asyncio.wait_for(self._probe(),timeout=240)
 async def _probe(self):
  release=release_id();tx=read(DATA/"update-transaction.json")
  process=await asyncio.create_subprocess_exec("/usr/bin/node",str(Path(__file__).resolve().parents[1]/"system/dex-safety-browser.mjs"),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
  try:
   raw,_=await asyncio.wait_for(process.communicate(),145)
   browser=json.loads(raw) if process.returncode==0 else {}
  except (asyncio.TimeoutError,ValueError):
   browser={}
  finally:
   if process.returncode is None:
    try:process.kill()
    except ProcessLookupError:pass
    await process.wait()
  inference=await self.canary.run()
  if Path("/opt/codex-linux-control/current").resolve().parent.name!=release or read(DATA/"update-transaction.json").get("id")!=tx.get("id"):
   return {"status":"stale"}
  previous=read(DATA/"update-proof.json")
  startup_error=self.thread_start_error()
  protocol_error=bool(startup_error) and any(k in startup_error.lower() for k in ("dynamictools","experimental_api","invalid params","invalid request"))
  local_fail=browser.get("local")=="local-failure" or inference.get("status")=="local-failure" or protocol_error
  confirmations=(previous.get("local_failure_confirmations",0)+1) if local_fail and previous.get("release")==release and previous.get("transaction_id")==tx.get("id") else int(local_fail)
  proof={"release":release,"transaction_id":tx.get("id"),"checked_at":time.time(),"browser":browser.get("local","unknown"),"public":browser.get("public","unknown"),"inference":inference["status"],"thread_start":"pass" if not startup_error else "unknown","failure_class":"local-regression" if local_fail else "unconfirmed","local_failure_confirmations":confirmations}
  proof["public_boundary"]=browser.get("public_boundary")
  proof["validation_scope"]=proof_scope(proof)
  proof["authenticated_public_tested"]=proof.get("public")=="pass"
  proof["browser_diagnostic"]=(str(browser.get("reason") or "").split(":",1)[0] or None)
  self.last=proof;atomic(DATA/"update-proof.json",proof)
  if not tx.get("phase") in ("applying","verifying","rollback") and proof_scope(proof):
   atomic(DATA/"update-baseline-v2.json",{"release":release,"status":"verified","checked_at":time.time(),"proof":proof})
  return proof
 async def run(self):
  await asyncio.sleep(5)
  next_probe=0;last_context=None
  while True:
   try:
    tx=read(DATA/"update-transaction.json")
    updating=tx.get("phase") in ("applying","verifying","rollback") and release_id() in (tx.get("candidate"),tx.get("previous"))
    context=(tx.get("id"),updating)
    if context!=last_context:
     if updating:next_probe=0
     last_context=context
    pending=bool(read(DATA/"pending.json").get("id"))
    if time.monotonic()>=next_probe and (updating or not self.admission_closed()) and self.active()==0:
     self.probing=True
     try:result=await self.probe()
     finally:self.probing=False
     next_probe=time.monotonic()+(15 if updating else 60 if pending or not result.get("validation_scope") else 1800)
    elif pending and not updating:
     next_probe=min(next_probe,time.monotonic()+60)
   except Exception as e:
    self.last={"status":"unknown","error_type":type(e).__name__,"checked_at":time.time()}
    next_probe=time.monotonic()+60
   await asyncio.sleep(5)
