"""Durable cloud deadline, single existing broker channel, no Android/export.

900 seconds total, three fixed 300s slots. 130s of each slot reserved for the
existing broker's120s SOCKET timeout model plus scheduling/persistence margin.
That timeout is NOT completion proof; only private final-response callback clears
inflight, and crash/IPC uncertainty requires reconciliation regardless of time.
No generic broker bypass and no second host executor. Bounds are a model until
validated with the real broker; an overrun fails closed and never detaches work.
"""
import asyncio,hashlib,json,math,time
from pathlib import Path

TOTAL=900.
SLOT=300.
MAX_ATTEMPTS=3
BROKER_BOUND=120.
RESERVE=130.
DRAIN_GUARD=125.


def boot_id():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
def finite(n):return type(n) in (int,float) and math.isfinite(n)
def scope(state):
    return hashlib.sha256(json.dumps({k:state.get(k) for k in ('request','source_day','selected','uploads')},sort_keys=True,separators=(',',':')).encode()).hexdigest()


class CloudWindow:
    def __init__(self,state,save,wall=time.time,mono=time.monotonic,boot=boot_id):
        self.state,self.save,self.wall,self.mono,self.boot=state,save,wall,mono,boot

    def check(self,create=False):
        s=self.state;w,m=self.wall(),self.mono();bid=self.boot()
        if not finite(w) or not finite(m) or w<=0 or m<0:raise ValueError('cloud_clock_invalid')
        attempts=s.get('verification_attempts',0)
        if type(attempts) is not int or not 0<=attempts<=MAX_ATTEMPTS:raise ValueError('cloud_attempt_count_invalid')
        b=s.get('cloud_budget')
        if b is None:
            if attempts:raise ValueError('cloud_legacy_attempt_without_deadline')
            if not create:raise ValueError('cloud_deadline_missing')
            b={'schema':2,'scope':scope(s),'started_wall':w,'deadline_wall':w+TOTAL,'started_mono':m,'deadline_mono':m+TOTAL,'last_wall':w,'last_mono':m,'boot_id':bid,'attempt_count':0,'attempt_active':False,'inflight':False,'host_started_mono':None,'inflight_token':None}
            s['cloud_budget']=b;self.save()
        if not isinstance(b,dict) or b.get('schema')!=2 or type(b.get('schema')) is not int or b.get('scope')!=scope(s):raise ValueError('cloud_budget_scope_or_schema_invalid')
        for key in ('started_wall','deadline_wall','started_mono','deadline_mono','last_wall','last_mono'):
            if not finite(b.get(key)):raise ValueError('cloud_budget_nonfinite')
        if b.get('attempt_count')!=attempts or type(b.get('attempt_count')) is not int:raise ValueError('cloud_attempt_counter_corrupt')
        if b.get('boot_id')!=bid:raise ValueError('cloud_budget_boot_changed')
        if abs(b['deadline_wall']-b['started_wall']-TOTAL)>1e-6 or abs(b['deadline_mono']-b['started_mono']-TOTAL)>1e-6:raise ValueError('cloud_budget_duration_corrupt')
        if not (b['started_wall']<=b['last_wall']<=w and b['started_mono']<=b['last_mono']<=m):raise ValueError('cloud_clock_rollback_or_future_budget')
        if abs((w-b['last_wall'])-(m-b['last_mono']))>5:raise ValueError('cloud_clock_discontinuity')
        if w>=b['deadline_wall'] or m>=b['deadline_mono']:raise ValueError('cloud_global_deadline_exhausted')
        if type(b.get('attempt_active')) is not bool or type(b.get('inflight')) is not bool:raise ValueError('cloud_budget_flags_corrupt')
        if b['inflight']:
            if not isinstance(b.get('inflight_token'),str) or len(b['inflight_token'])!=32:raise ValueError('cloud_inflight_metadata_corrupt')
            if not b['attempt_active'] or not finite(b.get('host_started_mono')) or not b['started_mono']<=b['host_started_mono']<=m:raise ValueError('cloud_inflight_metadata_corrupt')
        if attempts:
            start=b.get('attempt_started_mono');end=b.get('attempt_deadline_mono')
            if not finite(start) or not finite(end) or not b['started_mono']+(attempts-1)*SLOT<=start<=m or abs(end-min(b['deadline_mono'],b['started_mono']+attempts*SLOT))>1e-6:
                raise ValueError('cloud_attempt_metadata_corrupt')
        elif b['attempt_active'] or b['inflight']:raise ValueError('cloud_attempt_metadata_corrupt')
        b.update(last_wall=w,last_mono=m);self.save()
        return b

    def remaining(self):
        b=self.check();return min(b['deadline_wall']-self.wall(),b['deadline_mono']-self.mono())

    def abandon_interrupted(self):
        b=self.state.get('cloud_budget')
        if isinstance(b,dict) and b.get('inflight') is True:
            # Time, socket timeout and elapsed120s are NOT command completion.
            raise ValueError('cloud_inflight_reconciliation_required')
        b=self.check()
        if b['attempt_active']:
            b['attempt_active']=False;self.save()
        return True

    def begin_attempt(self):
        b=self.check(create=True);attempts=self.state.get('verification_attempts',0)
        if b['attempt_active']:raise ValueError('cloud_attempt_still_active')
        if attempts>=MAX_ATTEMPTS:raise ValueError('cloud_attempts_exhausted')
        start=b['started_mono']+attempts*SLOT
        if self.mono()<start:return None
        # A missed slot is not shifted forward by restart or scheduling delay.
        end=min(b['deadline_mono'],start+SLOT)
        if end-self.mono()<=RESERVE:raise ValueError('cloud_attempt_slot_exhausted')
        self.state['verification_attempts']=attempts+1
        b.update(attempt_count=attempts+1,attempt_active=True,attempt_deadline_mono=end,attempt_started_mono=self.mono())
        self.save();return end-self.mono()-RESERVE

    def guard_work(self):
        b=self.check()
        end=b.get('attempt_deadline_mono')
        if not b['attempt_active'] or not finite(end) or end> b['deadline_mono'] or end> b['started_mono']+self.state['verification_attempts']*SLOT:raise ValueError('cloud_attempt_deadline_corrupt')
        if end-self.mono()<=RESERVE:raise ValueError('cloud_work_margin_exhausted')
        return min(end-self.mono()-RESERVE,self.remaining()-RESERVE)

    def finish(self):
        b=self.state['cloud_budget']
        if b['inflight']:raise ValueError('cloud_inflight_reconciliation_required')
        b.update(attempt_active=False,host_started_mono=None,inflight_token=None)
        self.save()


async def work_with_drain(coro,seconds,metrics):
    """One cancellation to inner task; no nested timer cancels shielded drain.

    External repeated cancellation is deferred until the same in-flight task
    finishes. Never detach broker work to claim deadline success. Record overruns.
    """
    task=asyncio.create_task(coro)
    cancelled=False;timed_out=False
    try:
        done,_=await asyncio.wait({task},timeout=seconds)
        if done:return task.result()
        timed_out=True
    except asyncio.CancelledError:
        cancelled=True
    task.cancel()  # Exactly once; _call can now shield/drain its existing command.
    start=time.monotonic();overrun=False
    while not task.done():
        try:
            await asyncio.wait({task},timeout=DRAIN_GUARD)
            if not task.done():overrun=True
        except asyncio.CancelledError:
            cancelled=True  # Do not send another cancellation into broker drain.
    metrics['drain_seconds']=time.monotonic()-start
    metrics['drain_guard_exceeded']=overrun or metrics['drain_seconds']>DRAIN_GUARD
    if not task.cancelled():
        try:task.result()
        except BaseException:pass
    if cancelled:raise asyncio.CancelledError
    if metrics['drain_guard_exceeded']:raise RuntimeError('cloud_broker_drain_bound_exceeded')
    if timed_out:raise TimeoutError('cloud_attempt_work_timeout')
