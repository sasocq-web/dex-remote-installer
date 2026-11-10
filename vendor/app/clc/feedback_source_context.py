"""Server-owned managed-browser leases; no context or identity from tool input.

Candidate wiring point: observe must run for every managed Playwright event,
and acquire receives only the native dispatcher workspace/thread. Fixed collector
code still verifies actual origin/account in the page. A lease is not login proof.
"""
import asyncio,secrets,time
from contextlib import contextmanager
from .feedback_source_receipts import ORIGIN,POLICY

OWNER='project:project-96921b3806e0'
RESIDENT='01a0ed9a-011f-7612-a25d-13e88635606d'

class ManagedSourceContexts:
    def __init__(self,active_turns,states,bridge,clock=time.monotonic):
        self.active_turns=active_turns;self.states=states;self.bridge=bridge;self.clock=clock
        self.epochs={};self.leases={}
    def observe(self,event):
        if not isinstance(event,dict) or event.get('kind')!='notification':return
        n=event.get('notification') or {};p=n.get('params') or {};turn=p.get('turn') or {}
        key=(str(event.get('workspace') or 'system'),str(p.get('threadId') or turn.get('threadId') or ''))
        if not key[1]:return
        item=p.get('item') or {};method=n.get('method')
        lifecycle=method in {'turn/started','turn/completed'}
        browser=(isinstance(item,dict) and item.get('type')=='mcpToolCall'
                 and (str(item.get('server') or '').casefold()=='playwright'
                      or str(item.get('tool') or '').casefold().startswith('browser_')))
        if lifecycle or browser:
            # Start, completion and even failed calls invalidate the old lease.
            # A replacement context can never inherit a prior private capture.
            self.epochs[key]=secrets.token_hex(24)
    @contextmanager
    def acquire(self,workspace,thread):
        key=(workspace,thread)
        if workspace not in {'system',OWNER} or (workspace==OWNER and thread!=RESIDENT):
            raise ValueError('source canonical native resident required')
        if key in self.leases:raise ValueError('source context already leased')
        active=self.active_turns.get(key);state=self.states.get(key);bridge=self.bridge(workspace)
        if (not isinstance(active,dict) or not isinstance(active.get('turn_id'),str)
            or not active['turn_id'] or not isinstance(state,dict)
            or state.get('context_open') is not True or state.get('turn_active') is not True
            or any(state.get(k) for k in ('browser_call_started','preview_call_started','credential_waiting'))
            or bridge is None):raise ValueError('source requires idle own active managed browser')
        epoch=self.epochs.setdefault(key,secrets.token_hex(24));lease=secrets.token_hex(32)
        context=dict(workspace=workspace,thread=thread,turn=active['turn_id'],context_id=epoch,
                     lease_id=lease,origin=ORIGIN,device='managed_desktop',account_policy=POLICY)
        started=self.clock();self.leases[key]=lease
        def current():
            now=self.clock()
            a=self.active_turns.get(key);s=self.states.get(key)
            if (self.leases.get(key)!=lease or self.epochs.get(key)!=epoch or a is not active or s is not state
                or a.get('turn_id')!=context['turn'] or self.bridge(workspace) is not bridge
                or s.get('context_open') is not True or s.get('turn_active') is not True
                or any(s.get(k) for k in ('browser_call_started','preview_call_started','credential_waiting'))
                or not started<=now<=started+180):raise ValueError('source native lease changed or expired')
            return dict(context)
        busy=False
        async def call(tool,args):
            nonlocal busy
            current()
            if tool!='browser_evaluate' or not isinstance(args,dict) or set(args)!={'function'} or not isinstance(args['function'],str):
                raise ValueError('private source collector operation required')
            if busy:raise ValueError('private source MCP call already active')
            busy=True
            try:
                result=await asyncio.wait_for(bridge.request('mcpServer/tool/call',{
                    'threadId':thread,'server':'playwright','tool':tool,'arguments':args},timeout=150),150)
                current();return result
            finally:busy=False
        try:yield current,call
        finally:
            if self.leases.get(key)==lease:self.leases.pop(key,None)
