"""Bounded growth for a demonstrably active executor, with host reserves."""
from pathlib import Path
import asyncio,json,logging,os,time

LOG=logging.getLogger(__name__)
BASE=512
CEILING=1024
STEP=128
GIB=1024**3

def proposed_limit(*, current, limit, active, idle, recent, available, pressure, ancestor_tasks, ancestor_memory, denials):
    if active==0 and idle==0 and current<BASE-64 and limit>BASE:return BASE
    if not (active==1 and idle==0 and recent and denials==0):return None
    if current < limit*.75 or limit>=CEILING:return None
    if available<2*GIB or pressure>=5 or ancestor_tasks<STEP+64 or ancestor_memory<GIB:return None
    return min(limit+STEP,CEILING)

def scope_path(unit):
    uid=os.getuid()
    return Path(f'/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/codex.slice/codex-control.slice')/unit

def inspect(path):
    current=int((path/'pids.current').read_text())
    limit=int((path/'pids.max').read_text())
    available=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))*1024
    pressure=float(Path('/proc/pressure/memory').read_text().split('avg10=',1)[1].split()[0])
    tasks=memory=float('inf')
    parent=path.parent
    while parent!=Path('/sys/fs/cgroup'):
        for resource in ('pids','memory'):
            maxfile=parent/(resource+'.max')
            if not maxfile.exists():continue
            maximum=maxfile.read_text().strip()
            if maximum=='max':continue
            margin=int(maximum)-int((parent/(resource+'.current')).read_text())
            if resource=='pids':tasks=min(tasks,margin)
            else:memory=min(memory,margin)
        parent=parent.parent
    denials=int((path/'pids.events').read_text().split()[1])
    return dict(current=current,limit=limit,available=available,pressure=pressure,ancestor_tasks=tasks,ancestor_memory=memory,denials=denials)

async def supervise(bridge):
    """Lives in backend cgroup, never in the potentially saturated executor."""
    while True:
        await asyncio.sleep(5)
        if not bridge.running or not bridge._process_scope:continue
        try:
            stats=inspect(scope_path(bridge._process_scope))
            active=len(bridge._active_threads)
            idle=len(bridge._loaded_threads-set(bridge._active_threads)-bridge.private_threads)
            limit=proposed_limit(**stats,active=active,idle=idle,recent=time.monotonic()-bridge._last_progress<30)
            bridge.resource_status={**stats,'active_threads':active,'retained_idle_threads':idle,'ceiling':CEILING}
            # JSON may not encode infinity; use explicit unlimited markers.
            for name in ('ancestor_tasks','ancestor_memory'):
                if bridge.resource_status[name]==float('inf'):bridge.resource_status[name]=None
            if limit is None:continue
            # Local subscription bookkeeping is insufficient: after unsubscribe,
            # the engine may still be retaining a thread during its grace period.
            active_before=dict(bridge._active_threads)
            generation=bridge.generation
            loaded=await bridge._request_no_start('thread/loaded/list',{},10)
            if not isinstance(loaded,dict) or not isinstance(loaded.get('data'),list):continue
            if set(loaded['data'])-set(active_before)-bridge.private_threads:continue
            if bridge.generation!=generation or bridge._active_threads!=active_before:continue
            unit=bridge._process_scope
            process=await asyncio.create_subprocess_exec('/usr/bin/systemctl','--user','set-property','--runtime',unit,f'TasksMax={limit}',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            try:code=await asyncio.wait_for(process.wait(),10)
            except asyncio.TimeoutError:
                process.kill();await process.wait();raise RuntimeError('budget adjustment timed out')
            if code:raise RuntimeError('budget adjustment refused')
            LOG.warning('Executor %s budget changed from %s to %s tasks with host reserves',bridge.label,stats['limit'],limit)
            await bridge.events.publish({'kind':'executor_capacity','workspace':bridge.label,'tasks_max':limit,'previous':stats['limit']})
        except asyncio.CancelledError:raise
        except (OSError,ValueError,IndexError,StopIteration,RuntimeError) as exc:
            bridge.resource_status={'measurement':'unavailable','error_type':type(exc).__name__}
            LOG.warning('Executor budget unchanged (%s): %s',bridge.label,type(exc).__name__)
