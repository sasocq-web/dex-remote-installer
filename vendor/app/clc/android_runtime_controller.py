"""Single service controller. No device construction or activation on import.

The migration entrypoint must take the singleton before constructing its sole
Lifecycle and inject the existing fixed collector. Never launch beside Dex's
legacy authority. Production migration/rollback remains a separate gate.
"""
import asyncio,contextlib,time
from .android_authority import AuthorityBusy

# Paired with native_budget.CLEANUP_AND_DRAIN_RESERVE; never extends TTL.
ORIGINAL_TTL_CLEANUP_RESERVE=100

class Controller:
    def __init__(self,runtime,server,collector,clock=time.monotonic):
        self.runtime=runtime;self.server=server;self.collector=collector
        server.controller=self
        self.clock=clock;self.pause_until=0;self.cloud_busy=False;self.release_allowed=lambda:True
        self.collector_task=None;self.started=False;self.stop_event=asyncio.Event()
        self.authority_contended=False
        runtime.background_admission=lambda:self.admitted()

    def admitted(self):return self.started and not self.authority_contended and self.clock()>=self.pause_until and self.release_allowed()

    async def quiesce(self):
        async with self.runtime.lock:
            self.pause_until=self.clock()+120
            return {'quiesced':not(self.runtime.owner or self.runtime.cleanup_failed or self.cloud_busy or (self.runtime.pool.occupied if hasattr(self.runtime,'pool') else False))}

    async def resume_admission(self):
        async with self.runtime.lock:self.pause_until=0

    async def verify_delivery(self,state):
        async with self.runtime.lock:
            if not self.admitted():return
            self.cloud_busy=True
        try:return await self.collector_verify(state)
        finally:
            async with self.runtime.lock:self.cloud_busy=False

    async def recover_expired(self):
        try:
            lease=self.runtime.authority.inspect()
            # Begin the SAME generation cleanup before the original lease expires,
            # rather than starting its bounded drain/stop at the expiry instant.
            # Authority.inspect already validates schema/boot/clock; no host day or
            # timezone fallback, no lease renewal or second timer/controller.
            clock=getattr(self.runtime.authority,"clock",None)
            if lease and lease["phase"]=="held" and callable(clock):
                if lease["expires"]-clock()<=ORIGINAL_TTL_CLEANUP_RESERVE:
                    self.runtime.authority.begin_cleanup(lease["generation"])
                    lease=self.runtime.authority.inspect()
        except AuthorityBusy:
            # Retry only observation on the existing tick. No command replay,
            # lease rewrite, deadline extension or unverified cleanup.
            self.authority_contended=True
            return False
        self.authority_contended=False
        if not lease or lease['phase']!='cleanup_required':return
        # Cleanup in progress or failed must finish through its own verified
        # path. Never cancel the collector merely because it marked cleanup.
        if self.runtime.cleanup_failed:return
        # Cancellation awaits the in-flight broker operation and finally cleanup.
        # The durable state is never cleared merely because the TTL expired.
        if self.runtime.background_owner is not None and self.collector_task:
            self.collector_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.collector_task
            self.collector_task=None
        await getattr(self.runtime,'recover_legacy',self.runtime.recover)()
        if self.started and self.collector_task is None:
            self.collector_task=asyncio.create_task(self.collector.worker())

    async def tick(self):
        if await self.recover_expired() is False:return
        keys=self.server.active_keys
        self.runtime.frontend_alive=self.clock()-self.server.last_pulse<=30
        if self.clock()-self.server.last_pulse>30:
            keys=set()
            if self.runtime.owner != self.runtime.background_owner:
                self.runtime.viewers.clear()
        await self.runtime.sweep(keys)

    async def run(self):
        await self.runtime.recover()
        self.collector_verify=self.collector.verify_delivery
        self.collector.verify_delivery=self.verify_delivery
        self.started=True
        self.collector_task=asyncio.create_task(self.collector.worker())
        try:
            while not self.stop_event.is_set():
                if self.collector_task.done():
                    # A failed service does not silently create another collector.
                    await self.collector_task
                    raise RuntimeError('collector_worker_terminated')
                await self.tick()
                try:await asyncio.wait_for(self.stop_event.wait(),1)
                except TimeoutError:pass
        finally:
            self.started=False
            try:
                if self.collector_task:
                    self.collector_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):await self.collector_task
            finally:
                try:await self.runtime.recover()
                finally:self.collector.verify_delivery=self.collector_verify
