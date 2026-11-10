"""Conversation routing for the two-slot emulator provider.

No import-time activation. The host adapter must implement start/stop/command
and positive exit verification. The production legacy runtime stays separate.
"""
import asyncio
import hashlib
import math
import re
import time
from dataclasses import replace
from urllib.parse import urlencode

from .emulator_leases import Reservations, Unavailable


def profile_id(workspace, purpose):
    if not isinstance(workspace, str) or not re.fullmatch(r'system|project:[A-Za-z0-9_.:-]{1,200}', workspace):
        raise ValueError('invalid_workspace')
    if not isinstance(purpose, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', purpose):
        raise ValueError('invalid_purpose')
    return hashlib.sha256((workspace+'\0'+purpose).encode()).hexdigest()


def admitted(metrics, leases):
    """Measured host reserve including all running Android providers; defer backups."""
    try:
        for name in ('memory_full_avg10','memory_full_avg60','mem_available_mib','disk_available_mib'):
            value=metrics[name]
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:return False
        return (metrics['backup_running'] is False
                and metrics['memory_full_avg10'] <= 1.0
                and metrics['memory_full_avg60'] <= 2.0
                # Admit each instance independently: 3500 MiB emulator cap,
                # 128 MiB networking cap, plus at least 1800 MiB host reserve.
                # A first instance need not reserve RAM for an unused second.
                and metrics['mem_available_mib'] >= 5500
                and metrics['disk_available_mib'] >= 8192)
    except (KeyError, TypeError):
        return False


async def drain(awaitable):
    """Never let cancellation leave a submitted host mutation running unseen."""
    task=asyncio.ensure_future(awaitable)
    cancelled=False
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:cancelled=True
    result=task.result()
    if cancelled:raise asyncio.CancelledError
    return result


class Pool:
    def __init__(self, database, host, validate, clock=time.monotonic, lock=None):
        self.reservations = Reservations(database)
        self.host, self.validate, self.clock = host, validate, clock
        self.lock = lock if lock is not None else asyncio.Lock()
        self.turns, self.viewers, self.last_used = {}, {}, {}
        self.accepting = False
        self.slot_locks = (asyncio.Lock(), asyncio.Lock())
        self.external_admission = lambda: True

    def lease(self, key):
        return next((r for r in self.reservations.list() if (r.workspace,r.thread)==key), None)

    @property
    def occupied(self):
        return bool(self.reservations.list())

    def status(self, key):
        lease = self.lease(key)
        return {'provider':'emulator', 'owned':bool(lease),
                'running': bool(lease and lease.phase=='ready'),
                'cleanup_pending':bool(lease and lease.phase=='stopping'),
                'capacity':2, 'occupied_slots':len(self.reservations.list()),
                'viewer_url':'/android-emulator.html?'+urlencode({'thread_id':key[1]}) if lease and lease.phase=='ready' else ''}

    async def _stop(self, lease):
        async with self.slot_locks[lease.slot]:
            current=self.lease((lease.workspace,lease.thread))
            if current is None:return
            if current.token!=lease.token:raise PermissionError('reservation_changed')
            await self._stop_locked(current)

    async def _stop_locked(self, lease):
        if lease.phase != 'stopping':
            self.reservations.transition(lease,'stopping')
            lease = replace(lease,phase='stopping')
        await drain(self.host.stop(lease))
        # A successfully submitted stop is not proof that all processes exited.
        stopped = await self.host.stopped(lease)
        self.reservations.release_after_exit(lease,lambda _:stopped is True)
        self.turns.pop(lease.token,None)
        self.viewers.pop(lease.token,None)
        self.last_used.pop(lease.token,None)

    async def recover(self):
        async with self.lock:
            self.accepting = False
            for lease in self.reservations.list():
                await self._stop(lease)
            self.accepting = True

    async def quiesce(self):
        async with self.lock:
            self.accepting = False
            return not self.occupied

    async def resume_admission(self):
        async with self.lock:
            if any(r.phase!='ready' for r in self.reservations.list()):
                raise Unavailable('cleanup_required')
            self.accepting=True

    async def execute(self,workspace,thread,args,turn_id='',turn_active=True):
        if not isinstance(thread,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',thread):
            raise ValueError('invalid_thread')
        if not isinstance(turn_id,str) or len(turn_id)>200:
            raise ValueError('invalid_turn')
        args=dict(args)
        purpose=args.pop('purpose',None)
        profile=profile_id(workspace,purpose)
        self.validate(args)  # Validate every argument before host IO or reservation.
        op=args['operation'];key=(workspace,thread)
        # Ready devices do not wait behind another device's boot or command.
        # Slot locks serialize input/capture/cleanup; generation is rechecked
        # after acquiring the lock, so a recycled slot can never receive input.
        if op not in {'stop','status'}:
            lease=self.lease(key)
            if lease and lease.phase=='ready':
                return await self._ready_command(key,profile,args,turn_id,turn_active,lease)
        async with self.lock:
            lease=self.lease(key)
            if lease and lease.purpose!=profile: raise Unavailable('conversation_has_other_purpose')
            if op=='status':return self.status(key)
            if op=='stop':
                if lease:
                    if self.viewers.get(lease.token):raise Unavailable('close_viewer_before_stop')
                    await self._stop(lease)
                return self.status(key)
            if not turn_active or not turn_id:raise Unavailable('active_turn_required')
            if lease and lease.phase!='ready':raise Unavailable('cleanup_required')
            if lease is None:
                if not self.accepting or not self.external_admission():raise Unavailable('admission_closed')
                metrics=await self.host.metrics()
                lease=self.reservations.reserve(workspace,thread,profile,lambda rows:admitted(metrics,rows))
                # Persist the reservation before any start can reach the host.
                try:
                    await drain(self.host.start(lease))
                    self.reservations.transition(lease,'ready')
                    lease=replace(lease,phase='ready')
                except BaseException:
                    # The adapter must drain a submitted start before reporting
                    # cancellation; otherwise stop could race with a later start.
                    await asyncio.shield(self._stop(lease))
                    raise
            return await self._ready_command(key,profile,args,turn_id,turn_active,lease)

    async def _ready_command(self,key,profile,args,turn_id,turn_active,lease):
        async with self.slot_locks[lease.slot]:
            current=self.lease(key)
            if not current or current.token!=lease.token:raise Unavailable('reservation_changed')
            if current.purpose!=profile:raise Unavailable('conversation_has_other_purpose')
            if current.phase!='ready':raise Unavailable('cleanup_required')
            if not turn_active or not turn_id:raise Unavailable('active_turn_required')
            self.turns[lease.token]=turn_id
            self.last_used[lease.token]=self.clock()
            try:
                output={} if args['operation']=='start' else await drain(self.host.command(lease,args))
            except BaseException:
                await asyncio.shield(self._stop_locked(current))
                raise
            return {**self.status(key),**output}

    async def viewer(self,key,token,connected):
        if not re.fullmatch(r'[a-f0-9]{32}',token) or type(connected) is not bool:
            raise ValueError('invalid_viewer')
        async with self.lock:
            lease=self.lease(key)
            if not lease or lease.phase!='ready':raise PermissionError('not_owner')
            viewers=self.viewers.setdefault(lease.token,set())
            if connected:viewers.add(token)
            else:
                viewers.discard(token)
                if not viewers and not self.turns.get(lease.token):await self._stop(lease)

    async def capture(self,key,token):
        lease=self.lease(key)
        if not lease:raise PermissionError('not_owner')
        async with self.slot_locks[lease.slot]:
            current=self.lease(key)
            if not current or current.token!=lease.token:raise PermissionError('not_owner')
            lease=current
            if not lease or lease.phase!='ready' or token not in self.viewers.get(lease.token,set()):
                raise PermissionError('not_owner')
            return await self.host.command(lease,{'operation':'screenshot'})

    async def viewer_command(self,key,token,args):
        self.validate(args)
        if args['operation'] in {'start','stop','status'}:raise ValueError('viewer_operation_not_allowed')
        lease=self.lease(key)
        if not lease:raise PermissionError('not_owner')
        async with self.slot_locks[lease.slot]:
            current=self.lease(key)
            if not current or current.token!=lease.token:raise PermissionError('not_owner')
            lease=current
            if not lease or lease.phase!='ready' or token not in self.viewers.get(lease.token,set()):
                raise PermissionError('not_owner')
            self.last_used[lease.token]=self.clock()
            return await drain(self.host.command(lease,args))

    async def observe(self,event):
        if event.get('kind')!='notification':return
        note=event.get('notification') or {};params=note.get('params') or {}
        key=(event.get('workspace','system'),params.get('threadId') or (params.get('turn') or {}).get('threadId'))
        turn=str((params.get('turn') or {}).get('id') or params.get('turnId') or '')
        async with self.lock:
            lease=self.lease(key)
            if not lease or not turn:return
            if note.get('method')=='turn/started':self.turns[lease.token]=turn
            elif note.get('method')=='turn/completed' and self.turns.get(lease.token)==turn:
                self.turns.pop(lease.token,None)
                if not self.viewers.get(lease.token):await self._stop(lease)

    async def sweep(self,active_keys,pulse_fresh=True):
        async with self.lock:
            for lease in self.reservations.list():
                key=(lease.workspace,lease.thread)
                if not pulse_fresh:self.viewers.pop(lease.token,None)
                if (lease.phase=='stopping' or not pulse_fresh or
                    (key not in active_keys and not self.viewers.get(lease.token) and
                     self.clock()-self.last_used.get(lease.token,0)>60)):
                    await self._stop(lease)
