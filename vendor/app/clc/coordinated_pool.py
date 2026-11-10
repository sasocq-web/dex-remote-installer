"""Opt-in, workspace-scoped screen sharing. No implicit credential migration.

A physical lease remains durable until its last member exits. Member sessions
are ephemeral: recovery closes the recorded physical leases, never replays UI.
Only the explicit screen holder can read or send input; queued members receive
status, never a screenshot of another member's application.
"""
import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from .emulator_pool import Pool, profile_id, drain
from .emulator_leases import Unavailable

MODES={'exclusive','coordinated','auto','isolated-displays'}
CONTROL={'acquire','yield'}

@dataclass
class Member:
    package: str
    turn: str
    used: float
    viewers: set=field(default_factory=set)

@dataclass
class Group:
    owner: tuple
    purpose: str
    physical: str
    members: dict=field(default_factory=dict)
    queue: list=field(default_factory=list)
    holder: tuple|None=None
    lock: asyncio.Lock=field(default_factory=asyncio.Lock)
    failed: bool=False

class CoordinatedPool:
    def __init__(self,*args,**kwargs):
        self.base=Pool(*args,**kwargs)
        self.groups={};self.member_groups={}
        self.membership=asyncio.Lock()

    def __getattr__(self,name):return getattr(self.base,name)
    @property
    def external_admission(self):return self.base.external_admission
    @external_admission.setter
    def external_admission(self,value):self.base.external_admission=value

    def owners(self):
        shared={g.owner for g in self.groups.values()}
        return [k for k in self.member_groups]+[(r.workspace,r.thread) for r in self.reservations.list() if (r.workspace,r.thread) not in shared]

    def lease(self,key):
        g=self.member_groups.get(key)
        if g:return self.base.lease(g.owner)
        if any(g.owner==key for g in self.groups.values()):return None
        return self.base.lease(key)

    def status(self,key):
        g=self.member_groups.get(key)
        if not g:
            if any(g.owner==key for g in self.groups.values()):
                return {**self.base.status(key),'owned':False,'running':False,'viewer_url':''}
            return self.base.status(key)
        s=self.base.status(g.owner)
        holder=g.holder==key and not g.failed
        return {**s,'mode':'coordinated','shared':True,'members':len(g.members),
                'screen_owned':holder,'waiting':key in g.queue,
                'selection_reason':'coordinated_screen_only_isolated_displays_unavailable',
                'queue_position':g.queue.index(key)+1 if key in g.queue else None,
                'viewer_url':('/android-emulator.html?thread_id='+key[1]) if holder else '',
                'session_failed':g.failed}

    async def recover(self):
        async with self.membership:
            await self.base.recover()
            self.groups.clear();self.member_groups.clear()

    async def _physical(self,g,args):
        # Called only while holding g.lock. No member-provided owner or token.
        try:
            return await self.base.execute(*g.owner,{'purpose':g.physical,**args},'shared-session',True)
        except BaseException:
            g.failed=True;g.holder=None
            raise

    async def _guard(self,g,key):
        lease=self.base.lease(g.owner)
        if not lease or lease.phase!='ready':raise Unavailable('shared_instance_unavailable')
        result=await drain(self.host.command(lease,{'operation':'foreground','package':g.members[key].package}))
        if result.get('matches') is not True:
            g.holder=None
            raise Unavailable('shared_app_focus_changed_acquire_again')

    async def _acquire(self,g,key):
        if g.failed:raise Unavailable('shared_session_cleanup_required')
        if g.holder==key:return self.status(key)
        if key not in g.queue:g.queue.append(key)
        if g.holder is not None or g.queue[0]!=key:return self.status(key)
        await self._physical(g,{'operation':'open','package':g.members[key].package})
        # App startup may be asynchronous. A failed check grants no screen.
        await self._guard(g,key)
        g.queue.remove(key);g.holder=key
        return self.status(key)

    async def execute(self,workspace,thread,args,turn_id='',turn_active=True):
        mode=args.get('mode','exclusive');op=args['operation'];key=(workspace,thread)
        if mode not in MODES:raise ValueError('invalid_sharing_mode')
        if mode=='isolated-displays':raise Unavailable('isolated_displays_not_implemented')
        purpose=args.get('purpose');profile_id(workspace,purpose)
        if not isinstance(thread,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',thread):raise ValueError('invalid_thread')
        if not isinstance(turn_id,str) or len(turn_id)>200:raise ValueError('invalid_turn')
        g=self.member_groups.get(key)
        if g is None and mode=='exclusive':
            if any(x.owner==key for x in self.groups.values()):raise Unavailable('shared_physical_owner_still_reserved')
            return await self.base.execute(workspace,thread,{k:v for k,v in args.items() if k!='mode'},turn_id,turn_active)
        if g and (g.purpose!=purpose or mode=='exclusive'):raise Unavailable('shared_mode_and_purpose_required')
        if op=='status':return self.status(key)
        if op=='stop' and g is None:return self.status(key)
        if not turn_active or not turn_id:raise Unavailable('active_turn_required')
        if g is None:
            if op not in {'start','open','acquire'}:raise Unavailable('shared_start_with_package_required')
            package=args.get('package')
            if not isinstance(package,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+',package):raise ValueError('shared_package_required')
            async with self.membership:
                if key in self.member_groups:raise Unavailable('shared_join_in_progress')
                if self.base.lease(key):raise Unavailable('conversation_has_other_reservation')
                if not self.base.accepting or not self.external_admission():raise Unavailable('admission_closed')
                group_key=(workspace,purpose)
                g=self.groups.get(group_key)
                if g is None:
                    if not self.base.accepting or not self.external_admission():raise Unavailable('admission_closed')
                    physical='shared-'+hashlib.sha256(purpose.encode()).hexdigest()[:48]
                    g=Group(key,purpose,physical)
                    await self.base.execute(workspace,thread,{'operation':'start','purpose':physical},'shared-session',True)
                    self.groups[group_key]=g
                async with g.lock:
                    if self.groups.get(group_key) is not g or not self.base.lease(g.owner):raise Unavailable('shared_group_changed_retry_join')
                    if g.failed:raise Unavailable('shared_session_cleanup_required')
                    if len(g.members)>=8:raise Unavailable('shared_session_capacity')
                    if any(m.package==package for m in g.members.values()):raise Unavailable('shared_app_reserved')
                    g.members[key]=Member(package,turn_id,self.clock())
                    self.member_groups[key]=g
        async with g.lock:
            if key not in g.members:raise Unavailable('shared_session_ended')
            m=g.members[key]
            if args.get('package',m.package)!=m.package:raise Unavailable('shared_package_mismatch')
            m.turn=turn_id;m.used=self.clock()
            if op=='stop':
                if m.viewers:raise Unavailable('close_viewer_before_stop')
                await self._leave(g,key)
                return self.status(key)
            if op=='yield':
                if m.viewers:raise Unavailable('close_viewer_before_yield')
                if g.holder==key:g.holder=None
                if key in g.queue:g.queue.remove(key)
                return self.status(key)
            if op in {'start','open','acquire'}:return await self._acquire(g,key)
            return await self._command(g,key,{k:v for k,v in args.items() if k not in {'purpose','mode'}})

    async def _command(self,g,key,args):
        if g.failed:raise Unavailable('shared_session_cleanup_required')
        if g.holder!=key:raise Unavailable('shared_screen_not_owned')
        if args['operation'] in {'home','play','capabilities'}:raise Unavailable('operation_requires_exclusive_instance')
        if args.get('package',g.members[key].package)!=g.members[key].package:raise Unavailable('shared_package_mismatch')
        await self._guard(g,key)
        result=await self._physical(g,args)
        # Do not return another app's capture/UI after a transition.
        await self._guard(g,key)
        return {**result,**self.status(key)}

    async def _leave(self,g,key):
        if len(g.members)==1:
            await self.base.execute(*g.owner,{'operation':'stop','purpose':g.physical})
        if g.holder==key:g.holder=None
        if key in g.queue:g.queue.remove(key)
        del g.members[key];self.member_groups.pop(key,None)
        if not g.members:self.groups.pop((g.owner[0],g.purpose),None)

    async def viewer(self,key,token,connected):
        g=self.member_groups.get(key)
        if not g:
            if any(x.owner==key for x in self.groups.values()):raise PermissionError('not_owner')
            return await self.base.viewer(key,token,connected)
        if not re.fullmatch(r'[a-f0-9]{32}',token) or type(connected) is not bool:raise ValueError('invalid_viewer')
        async with g.lock:
            m=g.members.get(key)
            if not m:raise PermissionError('not_owner')
            if connected:
                if g.holder!=key or g.failed:raise PermissionError('shared_screen_not_owned')
                m.viewers.add(token)
            else:
                m.viewers.discard(token)
                if not m.viewers and not m.turn:await self._leave(g,key)

    async def viewer_command(self,key,token,args):
        self.validate(args)
        g=self.member_groups.get(key)
        if not g:
            if any(x.owner==key for x in self.groups.values()):raise PermissionError('not_owner')
            return await self.base.viewer_command(key,token,args)
        if args['operation'] in {'start','stop','status','open'}:raise ValueError('viewer_operation_not_allowed')
        async with g.lock:
            if key not in g.members or token not in g.members[key].viewers:raise PermissionError('not_owner')
            g.members[key].used=self.clock()
            return await self._command(g,key,args)

    async def capture(self,key,token):return await self.viewer_command(key,token,{'operation':'screenshot'})

    async def observe(self,event):
        note=event.get('notification') or {};p=note.get('params') or {}
        key=(event.get('workspace','system'),p.get('threadId') or (p.get('turn') or {}).get('threadId'))
        g=self.member_groups.get(key)
        if g:
            turn=str((p.get('turn') or {}).get('id') or p.get('turnId') or '')
            if not turn:return
            async with g.lock:
                m=g.members.get(key)
                if not m:return
                if note.get('method')=='turn/started':m.turn=turn
                elif note.get('method')=='turn/completed' and m.turn==turn:
                    m.turn=''
                    if not m.viewers:await self._leave(g,key)
            return
        if any(x.owner==key for x in self.groups.values()):return
        await self.base.observe(event)

    async def sweep(self,active_keys,pulse_fresh=True):
        for g in list(self.groups.values()):
            async with g.lock:
                for key,m in list(g.members.items()):
                    if not pulse_fresh or (key not in active_keys and not m.viewers and self.clock()-m.used>60):
                        await self._leave(g,key)
        protected={g.owner for g in self.groups.values()}
        await self.base.sweep(set(active_keys)|protected,pulse_fresh)
