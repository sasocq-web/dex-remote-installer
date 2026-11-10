"""Fixed native feedback guard, event wakeups and 24 hour recovery."""
import asyncio, contextlib, json, logging, time, re
from pathlib import Path
from .control_plane import request
from .feedback_native import FeedbackNative
FEEDBACK_AUTOMATION_ID="03a1f80223504265bc55691dcb74ebe8"
RESIDENT="01a11ac1-3b34-7d31-b2f5-000ac0978dd7"
LOG=logging.getLogger(__name__)

class FeedbackScheduler:
    def __init__(self, config_dir, automations, lifecycle, ready):
        self.directory=Path(config_dir)/"feedback-native"
        self.config_path=Path(__file__).with_name("feedback_config.json")
        self.automations=automations; self.lifecycle=lifecycle; self.ready=ready
        self.lock=asyncio.Lock();self.task=None;self.event_cursor=None
        self.last_check=0;self.last_result={}
    def config(self):
        config=json.loads(self.config_path.read_text())
        # Operational activation is separate from immutable release sources.
        activation=self.directory/"activation.json"
        if activation.exists():
            state=json.loads(activation.read_text())
            config["dispatch_enabled"]=(state.get("enabled") is True
                and state.get("validated") is True
                and state.get("contract")=="feedback-native-v1")
        config["canary_mode"]=False
        canary=self.directory/"canary.json"
        if not config.get("dispatch_enabled") and canary.exists():
            state=json.loads(canary.read_text())
            ids=state.get("event_ids")
            expires=state.get("expires_at",0)
            if (state.get("contract")=="feedback-canary-v1"
                and isinstance(expires,(int,float)) and time.time()<expires<=time.time()+3600
                and isinstance(ids,list) and 1<=len(ids)<=5
                and all(isinstance(i,str) and re.fullmatch(r"[a-f0-9]{64}",i) for i in ids)):
                config["canary_mode"]=True
                config["event_ids"]=list(dict.fromkeys(ids))
        adoption=self.directory/'adoption.json'
        if adoption.exists():
            state=json.loads(adoption.read_text())
            ids=state.get('event_ids')
            if (state.get('contract')!='feedback-adoption-v1' or not state.get('approval_ref')
                or not isinstance(ids,list) or len(ids)>300
                or any(not isinstance(i,str) or not re.fullmatch(r'(?:[a-f0-9]{64}|historical:[a-f0-9]{16})',i) for i in ids)
                or len(set(ids))!=len(ids)):
                raise ValueError('Invalid historical adoption manifest')
            config['adopted_event_ids']=ids
        return config

    @staticmethod
    def permitted(record, config):
        if not record:return False
        if config.get("canary_mode"):
            return record.get("status")=="PAUSED"
        return record.get("status")=="ACTIVE" and config.get("dispatch_enabled",False)
    def remote(self, data):
        helper=str(Path(__file__).resolve().parents[1]/"system/feedback_transport.py")
        result=request("host-admin",dict(operation="exec",
            argv=["runuser","-u","codex-worker","--","python3",helper],
            input_text=json.dumps(data),timeout=22),timeout=28)
        raw=result.get("result",{}).get("output",{})
        if not result.get("ok") or raw.get("returncode"):raise RuntimeError("Feedback transport unavailable")
        reply=json.loads(raw.get("output","{}"))
        if reply.get("ok") is False:raise RuntimeError("Feedback operation rejected")
        return reply.get("result",reply)
    def record(self):
        return next((r for r in self.automations.store.list_all() if r["id"]==FEEDBACK_AUTOMATION_ID),None)
    def observation_worker(self):
        config=self.config()
        if not self.ready() or not self.permitted(self.record(),config):
            raise ValueError('Feedback observation disabled')
        # Used only for scoped read-only contract lookup by the native channel.
        return FeedbackNative(config,self.directory,self.remote,None)
    async def run(self, automation):
        if automation.get("id")!=FEEDBACK_AUTOMATION_ID:raise ValueError("Fixed feedback binding required")
        if not self.ready():return ""
        async with self.lock:
            current=await asyncio.to_thread(self.record)
            config=self.config()
            if not self.permitted(current,config):return ""
            loop=asyncio.get_running_loop()
            def native(data):
                future=asyncio.run_coroutine_threadsafe(
                    self.lifecycle.execute("system",RESIDENT,data),loop)
                return future.result(timeout=45)
            task=asyncio.create_task(asyncio.to_thread(
                FeedbackNative(config,self.directory,self.remote,native).tick))
            try:
                result=await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            self.last_check=time.time();self.last_result=result
            # Empty queue is a deterministic check, never a dummy model turn.
            return ""
    async def validate_payload(self,payload):
        message=str(payload.get("message") or "")
        if not message.startswith("Lote nativo SASOCQ."):return
        meta=json.loads(message.rsplit("\n",1)[-1])
        config=self.config()
        route=config["sites"].get(meta["site"])
        if not (config.get("dispatch_enabled") or config.get("canary_mode")) or not route or not route.get("enabled",True) or any(
            payload.get(k)!=route.get(k) for k in ("project_id","thread_id")):
            raise ValueError("Feedback route disabled")
        current=await asyncio.to_thread(self.record)
        if not self.permitted(current,config):raise ValueError("Feedback automation paused")
        fence=await asyncio.to_thread(self.remote,dict(action="native_validate_batch",
            batch_id=meta["batch_id"],token=meta["token"],sites=list(config["sites"]),
            not_before_epoch=config["not_before_epoch"],
            adopted_event_ids=config.get("adopted_event_ids",[]),
            route_paths={s:r["path_prefixes"] for s,r in config["sites"].items() if "path_prefixes" in r},
            **({"canary_mode":True,"event_ids":config["event_ids"]} if config.get("canary_mode") else {})))
        if not fence.get("allowed"):raise ValueError("Feedback cancelled or outside scope")
    async def start(self):
        self.task=asyncio.create_task(self.loop(),name="clc-feedback-native-events")
    async def loop(self):
        while True:
            try:
                record=await asyncio.to_thread(self.record)
                config=self.config()
                if self.permitted(record,config) and self.ready():
                    data=dict(_transport_mode="events",cutoff=config["not_before_epoch"],wait_seconds=0)
                    if self.event_cursor:data["after_seq"]=self.event_cursor
                    wake=await asyncio.to_thread(self.remote,data)
                    next_check=self.last_result.get("next_check",self.last_check+86400)
                    if wake.get("events") or time.time()>=min(self.last_check+86400,next_check):
                        await self.run(record)
                    if wake.get("next_seq"):self.event_cursor=wake["next_seq"]
                    # Active claims must receive receipts/renewal promptly, not 15 min later.
                    if self.last_result.get("skip_reason") in ("awaiting_native_receipt","running","continuation_prepared") and time.time()-self.last_check>=15:
                        await self.run(record)
            except asyncio.CancelledError:raise
            except Exception:
                LOG.exception("Native feedback event check failed closed")
            await asyncio.sleep(5)
    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.task
