"""Dex-side typed client: no Lifecycle, device lock, broker or collector.

Service state is refreshed by the heartbeat. Unknown state fails closed.
No operation is retried automatically after a timeout or lost response.
"""
import asyncio,time,uuid,logging
LOG=logging.getLogger(__name__)
from pathlib import Path
from clc.android_lifecycle import AndroidLifecycle
from .android_runtime_ipc import call
from .parallel_runtime import validate_request

class Client:
    def __init__(self,marker,clock=time.monotonic,expected_release=None):
        self.marker=Path(marker);self.path=self.marker.parent/'android-runtime'/'android-runtime.sock'
        self.expected_release=expected_release
        self.clock=clock;self.cache=None;self.checked_at=0;self.tokens={}
        self.feedback_worker=None;self.background_admission=None

    def current(self):
        return self.cache if self.cache is not None and self.clock()-self.checked_at<5 else None

    @property
    def owner(self):
        state=self.current();return tuple(state['owner']) if state and state.get('owner') else None

    @property
    def background_owner(self):
        state=self.current();return tuple(state['background_owner']) if state and state.get('background_owner') else None

    @property
    def cleanup_failed(self):
        state=self.current();return state is None or state.get('cleanup_pending') is not False

    def _key(self,workspace,thread):return AndroidLifecycle._key(self,workspace,thread)

    def validate_state(self,state):
        if type(state) is not dict or state.get('protocol')!=1:raise ValueError('runtime_protocol_mismatch')
        if self.expected_release is not None and state.get('release_id')!=self.expected_release:
            raise ValueError('runtime_release_mismatch')

    async def refresh(self,operation='state',**kwargs):
        state=await call(self.path,dict(operation=operation,**kwargs))
        self.validate_state(state)
        self.cache=state;self.checked_at=self.clock();return state

    async def execute(self,workspace,thread,args,turn_active=True,turn_id=''):
        self._key(workspace,thread)
        validate_request(args)
        result=await call(self.path,dict(operation='execute',workspace=workspace,thread=thread,arguments=args,turn_id=turn_id))
        state=result.get('state')
        self.validate_state(state)
        self.cache=state;self.checked_at=self.clock()
        return result['value']

    async def collect_feedback_probe(self,worker,arguments,workspace,thread,turn_id):
        # The service resolves its own current canonical worker; no caller facts.
        return await call(self.path,dict(operation='probe',workspace=workspace,thread=thread,turn_id=turn_id,arguments=arguments))

    async def quiesce(self):
        return await call(self.path,dict(operation='quiesce'))

    async def resume_admission(self):
        return await self.refresh('resume_admission')

    async def handle_server_request(self,workspace,message):
        return await AndroidLifecycle.handle_server_request(self,workspace,message)

    async def viewer(self,key,socket,connected):
        token=self.tokens.get(socket)
        if connected:
            if token is None:token=uuid.uuid4().hex;self.tokens[socket]=token
        elif token is None:return
        try:
            await self.refresh('viewer',workspace=key[0],thread=key[1],token=token,connected=connected)
        finally:
            if not connected:self.tokens.pop(socket,None)

    async def emulator_status(self,key):
        return await call(self.path,dict(operation='emulator_status',workspace=key[0],thread=key[1]))

    async def emulator_view(self,key,socket,args):
        token=self.tokens.get(socket)
        if not token:raise ValueError('viewer_not_connected')
        return await call(self.path,dict(operation='emulator_view',workspace=key[0],thread=key[1],token=token,arguments=args))

    async def observe(self,event):
        note=event.get('notification') or {}
        if event.get('kind')=='notification' and note.get('method') in {'turn/started','turn/completed'}:
            await self.refresh('observe',event=event)

    async def recover(self):
        # Backend shutdown must not kill a service-owned background reservation.
        return await self.refresh()

    async def worker(self,events,active_keys):
        queue=await events.subscribe()
        next_pulse=0
        try:
            while True:
                if self.clock()>=next_pulse:
                    try:await self.refresh('pulse',active_keys=[list(k) for k in active_keys()])
                    except Exception:LOG.warning('Android authority heartbeat unavailable')
                    next_pulse=self.clock()+1
                try:event=await asyncio.wait_for(queue.get(),max(.01,next_pulse-self.clock()))
                except TimeoutError:continue
                try:await self.observe(event)
                except Exception:LOG.warning('Android turn event uncertain; not replayed')
        finally:await events.unsubscribe(queue)
