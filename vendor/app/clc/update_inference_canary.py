"""Bounded inference probe through the same Dex bridge; no administrative tools."""
import asyncio,secrets,time
class InferenceCanary:
 def __init__(self,bridge,events,cwd):
  self.bridge=bridge;self.events=bridge.private_events;self.cwd=cwd;self.lock=asyncio.Lock();self.thread=""
 async def run(self):
  if self.lock.locked():return {"status":"busy"}
  async with self.lock:
   result={"status":"unknown","checked_at":time.time()}
   token="DEX_CANARY_"+secrets.token_hex(12)
   future=asyncio.get_running_loop().create_future();texts=[];tool_seen=False
   def observe(event):
    nonlocal tool_seen
    n=event.get("notification") or {};p=n.get("params") or {}
    if p.get("threadId")!=self.thread:return
    if n.get("method")=="item/completed":
     item=p.get("item") or {};kind=item.get("type")
     if kind=="agentMessage":texts.append(item.get("text",""))
     elif kind not in ("reasoning","userMessage"):tool_seen=True
    if n.get("method")=="turn/completed" and not future.done():future.set_result(p.get("turn") or {})
   self.events.add_critical_observer(observe)
   try:
    configuration=await self.bridge.request("config/read",{"includeLayers":False},timeout=10)
    servers=(configuration.get("config") or {}).get("mcp_servers") or {}
    disabled_servers={name:{"enabled":False} for name in servers}
    started=await self.bridge.request("thread/start",{"cwd":self.cwd,"approvalPolicy":"never","sandbox":"read-only","ephemeral":True,"baseInstructions":"You are a health probe. Never use any tool. Return only the requested token.","developerInstructions":"No tools, no files, no network or administrative actions.","dynamicTools":[],"config":{"mcp_servers":disabled_servers,"web_search":"disabled","features":{"apps":False,"shell_tool":False,"apply_patch_freeform":False,"multi_agent":False}},"serviceName":"dex_update_inference_canary"},timeout=30)
    self.thread=started["thread"]["id"]
    self.bridge.private_threads.add(self.thread)
    turn=await self.bridge.request("turn/start",{"threadId":self.thread,"approvalPolicy":"never","sandboxPolicy":{"type":"readOnly"},"input":[{"type":"text","text":"Return exactly: "+token,"text_elements":[]}]},timeout=30)
    tid=(turn.get("turn") or turn).get("id")
    try:done=await asyncio.wait_for(future,90)
    except asyncio.TimeoutError:
     if tid:await self.bridge.request("turn/interrupt",{"threadId":self.thread,"turnId":tid},timeout=10)
     return {**result,"status":"unknown","reason":"inference-timeout"}
    error=done.get("error") or {}
    if done.get("status")=="completed" and "".join(texts).strip()==token and not tool_seen:
     return {**result,"status":"pass"}
    # Upstream/auth/quota and unknown errors cannot authorize rollback.
    local=error.get("codexErrorInfo") in ("badRequest","sandboxError","threadRollbackFailed")
    return {**result,"status":"local-failure" if local else "unknown","reason":"inference-contract" if local else "upstream-or-output-unconfirmed","turn_status":done.get("status"),"text_count":len(texts),"token_match":"".join(texts).strip()==token,"tool_seen":tool_seen,"error_class":error.get("codexErrorInfo")}
   except Exception as e:
    return {**result,"status":"unknown","reason":type(e).__name__}
   finally:
    self.events.remove_critical_observer(observe)
    if self.thread:
     try:await self.bridge.request("thread/unsubscribe",{"threadId":self.thread},timeout=10)
     except Exception:pass
    self.thread=""
