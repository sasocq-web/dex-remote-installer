"""PRIVATE resident protocol C. No public IPC, new executor, export or cloud window.
System alone writes protected tickets. A validated file is not a Python grant:
consumption revalidates its exact bytes, inputs and resident scope under SAME lock.
"""
import asyncio,copy,hashlib,json,math,os,re,time,uuid
from dataclasses import dataclass
from . import eligible_delivery as E
from .collector_phases import PhaseDeadline,submitted_delivery

_KIND='post_delivery_compare_only'
_SEAL=object()
_RESIDENT=uuid.uuid4().hex
_MAX_TTL=24*60  # Finite private turn TTL, capped by existing UI24; NOT cloud900.
_INPUTS=('config.json','status.json','canonical.json')

def _J():
    from . import android_jobs
    return android_jobs

def marked(s):
    return any(k in s for k in ('post_delivery_compare','post_delivery_history','post_delivery_result'))

def _finite(x):return type(x) in (int,float) and math.isfinite(x)
def _id(x):return isinstance(x,str) and re.fullmatch(r'[0-9a-f]{32}',x) is not None
def _hash(x):return isinstance(x,str) and re.fullmatch(r'[0-9a-f]{64}',x) is not None

def _raw(job,name):
    value=job.read(name)  # Original protected-file checks; regular file below.
    path=job.directory/name
    if not path.is_file() or path.is_symlink():raise ValueError('post_compare_input_missing')
    return value,hashlib.sha256(path.read_bytes()).hexdigest()

def _inputs(job):return {n:_raw(job,n)[1] for n in _INPUTS}

def _free(job):
    r=job.runtime
    if (r.owner is not None or r.background_owner is not None or r.cleanup_failed
            or getattr(r,'durable_generation',None) is not None
            or r.authority.inspect() is not None or not r.background_admission()):
        raise ValueError('post_compare_actor_not_free')

def _completed(job,config,state,registry):
    J=_J()
    if (marked(state) or config.get('enabled') is not False or config.get('validated') is not False
            or not isinstance(state.get('request'),str) or not state['request']
            or config.get('run_once')!=state['request']
            or state.get('phase') not in {'complete','selected_verified_pending_ui'}
            or state.get('state') not in {'verified','pending_reconciliation'}
            or not _finite(state.get('finished')) or state['finished']<=0
            or any(state.get(k) for k in ('cancelled','revoked','cancel_requested','error'))):
        raise ValueError('post_compare_previous_delivery_not_complete')
    if (state.get('state'),state.get('phase')) not in {('verified','complete'),('pending_reconciliation','selected_verified_pending_ui')}:
        raise ValueError('post_compare_terminal_phase_conflict')
    p=E.validate(state)
    if p is None:raise ValueError('post_compare_verified_policy_required')
    display=copy.deepcopy(state);E.finish(display)
    if state.get('delivery_result')!=display['delivery_result']:
        raise ValueError('post_compare_terminal_result_conflict')
    # Exact ORIGINAL all-submitted guard, even though final state is no longer handoff.
    submitted_delivery(dict(state,state='verification_pending'),J.GROUPS)
    if set(state.get('uploads',{}))!=set(p['selected']):raise ValueError('post_compare_submission_unproved')
    proof=state.get('proof',{})
    if (proof.get('state')!='verified' or set(proof.get('groups',{}))!=set(p['selected'])
            or any(v.get('new_sequence_verified') is not True for v in proof['groups'].values())):
        raise ValueError('post_compare_sequence_unverified')
    if J.advance_sources(registry,state['reconciliation'],state['source_day'],proof,p['selected'])!=registry:
        raise ValueError('post_compare_canonical_not_committed')
    # Reject extra/replaced registry data too: original guard compares identities,
    # here exact expected full canonical shape/content is additionally required.
    old={'schema':1,'groups':{c:{'source_day':v['source_day'],'receipt':v['receipt']} for c,v in p['sources'].items()}}
    expected=J.advance_sources(old,state['reconciliation'],state['source_day'],proof,p['selected'])
    if expected!=registry:raise ValueError('post_compare_canonical_exact_mismatch')
    budget=state.get('cloud_budget')
    if (not isinstance(budget,dict) or type(budget.get('schema')) is not int or budget['schema']!=2
            or budget.get('inflight') is not False or budget.get('attempt_active') is not False
            or getattr(job,'_cloud_verify_active',False)):
        raise ValueError('post_compare_cloud_unsettled')
    # Original cloud proof must be terminal, not a deadline-based inference.
    from .collector_cloud_budget import scope
    if budget.get('scope')!=scope(state):raise ValueError('post_compare_cloud_scope_changed')
    last=budget.get('last_completion')
    if (not isinstance(last,dict) or last.get('kind')!='private_broker_final_response'
            or last.get('scope')!=budget['scope'] or last.get('attempt')!=state.get('verification_attempts')
            or not _id(last.get('token')) or budget.get('inflight_token') is not None):
        raise ValueError('post_compare_cloud_receipt_uncertain')
    for key in ('started_wall','deadline_wall','started_mono','deadline_mono','last_wall','last_mono'):
        if not _finite(budget.get(key)):raise ValueError('post_compare_cloud_metadata_invalid')
    if (budget['deadline_wall']!=budget['started_wall']+900
            or budget['deadline_mono']!=budget['started_mono']+900
            or not budget['started_wall']<=budget['last_wall']<=budget['deadline_wall']
            or not budget['started_mono']<=budget['last_mono']<=budget['deadline_mono']
            or type(state.get('verification_attempts')) is not int
            or not 1<=state['verification_attempts']<=3
            or type(budget.get('attempt_count')) is not int
            or budget.get('attempt_count')!=state['verification_attempts']):
        raise ValueError('post_compare_cloud_metadata_invalid')
    return p

def publish_resident(job):
    """C worker's private rendezvous, not authorization. Restart changes nonce."""
    if job.directory.is_symlink():raise ValueError('post_compare_private_directory_unsafe')
    if not job.directory.is_dir():return None  # OFF/absent config remains inert.
    value={'schema':1,'resident':_RESIDENT,'pid':os.getpid()}
    if job.read('post-delivery-resident.json')!=value:
        _J().atomic(job.directory/'post-delivery-resident.json',value)
    return value

def _resident(job):
    value,_=_raw(job,'post-delivery-resident.json')
    if (set(value)!={'schema','resident','pid'} or type(value['schema']) is not int or value['schema']!=1
            or not _id(value['resident']) or type(value['pid']) is not int or value['pid']<=0):
        raise ValueError('post_compare_resident_invalid')
    return value

def _issue_system_ticket(job,turn_id,ttl=24*60):
    """System-private reconciler helper; NEVER registered to Projects/JSON/IPC.
    External System process reads the worker-owned rendezvous, cannot create a
    new resident capability. No operational invocation belongs to this package.
    """
    if not _id(turn_id) or not _finite(ttl) or not 0<ttl<=_MAX_TTL:raise ValueError('post_compare_turn_or_ttl_invalid')
    config,state,registry=[_raw(job,n)[0] for n in _INPUTS]
    _completed(job,config,state,registry);_free(job)
    prior=job.read('resume.json')
    if prior and prior.get('state')=='authorized':raise ValueError('post_compare_ticket_already_authorized')
    resident=_resident(job);now=time.time()
    if not _finite(now) or now<state['finished']:raise ValueError('post_compare_clock_invalid')
    ticket={'schema':2,'kind':_KIND,'id':uuid.uuid4().hex,'state':'authorized',
            'request':state['request'],'turn_id':turn_id,'resident':resident,
            'issued':now,'expires':now+ttl,'inputs':_inputs(job),
            'previous_ticket_sha256':E.digest(prior),'previous_ticket':copy.deepcopy(prior),
            'history_scope':E.digest({'status':state,'canonical':registry}),
            'source_scope':E.digest(registry)}
    turn={'schema':1,'turn_id':turn_id,'ticket_id':ticket['id'],'ticket_sha256':E.digest(ticket),
          'resident':resident,'issued':now,'expires':now+ttl}
    # A crash between the two atomic writes cannot admit: both hashes required.
    _J().atomic(job.directory/'post-delivery-turn.json',turn)
    _J().atomic(job.directory/'resume.json',ticket)
    return copy.deepcopy(ticket)

@dataclass(frozen=True,eq=False)
class _Admission:
    seal:object
    job:object
    runtime:object
    ticket:dict
    ticket_sha:str
    inputs:dict
    wall:float
    mono:float
    binding:tuple


def admission(job,config,state):
    ticket,rawsha=_raw(job,'resume.json')
    if (type(ticket.get('schema')) is not int or ticket['schema']!=2 or ticket.get('kind')!=_KIND
            or ticket.get('state')!='authorized' or not _id(ticket.get('id')) or not _id(ticket.get('turn_id'))
            or set(ticket)!={'schema','kind','id','state','request','turn_id','resident','issued','expires','inputs','previous_ticket_sha256','previous_ticket','history_scope','source_scope'}):
        raise ValueError('post_compare_ticket_invalid')
    now=time.time();mono=time.monotonic()
    if (not all(_finite(v) for v in (now,mono,ticket['issued'],ticket['expires'])) or mono<0 or now<=0
            or not ticket['issued']<=now<ticket['expires']<=ticket['issued']+_MAX_TTL
            or ticket['issued']<state.get('finished',float('inf'))):
        raise ValueError('post_compare_ticket_expired_or_clock_invalid')
    resident=_resident(job)
    if resident!={'schema':1,'resident':_RESIDENT,'pid':os.getpid()} or ticket['resident']!=resident:
        raise ValueError('post_compare_resident_changed')
    turn,_=_raw(job,'post-delivery-turn.json')
    expected={'schema':1,'turn_id':ticket['turn_id'],'ticket_id':ticket['id'],'ticket_sha256':E.digest(ticket),'resident':resident,'issued':ticket['issued'],'expires':ticket['expires']}
    if turn!=expected or type(turn.get('schema')) is not int:raise ValueError('post_compare_turn_changed')
    actual=_inputs(job)
    if (ticket['inputs']!=actual or set(ticket['inputs'])!=set(_INPUTS)
            or any(not _hash(v) for v in ticket['inputs'].values())
            or config!=job.read('config.json') or state!=job.read('status.json')):
        raise ValueError('post_compare_inputs_changed')
    registry=job.read_canonical();_completed(job,config,state,registry)
    if (ticket['request']!=state['request'] or ticket['history_scope']!=E.digest({'status':state,'canonical':registry})
            or ticket['source_scope']!=E.digest(registry)
            or ticket['previous_ticket_sha256']!=E.digest(ticket['previous_ticket'])):
        raise ValueError('post_compare_scope_changed')
    # Sealed object records identity + exact immutable bytes. Copying is refused.
    obj=_Admission(_SEAL,job,job.runtime,copy.deepcopy(ticket),rawsha,actual,now,mono,())
    object.__setattr__(obj,'binding',(id(obj),id(job),id(job.runtime),E.digest(ticket),rawsha,E.digest(actual),now,mono))
    return obj


def _recheck(job,token):
    if (not isinstance(token,_Admission) or token.seal is not _SEAL or token.job is not job or token.runtime is not job.runtime
            or token.binding!=(id(token),id(job),id(job.runtime),E.digest(token.ticket),token.ticket_sha,E.digest(token.inputs),token.wall,token.mono)):
        raise ValueError('post_compare_private_admission_invalid')
    now=time.time();mono=time.monotonic()
    if not _finite(now) or not _finite(mono) or now<token.wall or mono<token.mono:
        raise ValueError('post_compare_clock_rollback')
    if mono-token.mono>=token.ticket['expires']-token.wall:raise ValueError('post_compare_ticket_expired_or_clock_invalid')
    fresh=admission(job,job.read('config.json'),job.read('status.json'))
    if fresh.ticket_sha!=token.ticket_sha or fresh.ticket!=token.ticket or fresh.inputs!=token.inputs:
        raise ValueError('post_compare_ticket_changed')
    _free(job)


def _archive(job,token):
    status=job.read('status.json');canonical=job.read_canonical()
    value={'schema':1,'ticket_id':token.ticket['id'],'ticket':token.ticket,
           'previous_status':status,'config':job.read('config.json'),'canonical':canonical,
           'previous_ticket':token.ticket['previous_ticket']}
    d=job.directory/'post-delivery-history'
    if d.is_symlink():raise ValueError('post_compare_history_unsafe')
    d.mkdir(mode=0o700,exist_ok=True)
    if d.stat().st_mode & 0o077:raise ValueError('post_compare_history_unsafe')
    name=token.ticket['id']+'-'+E.digest(value)+'.json';path=d/name
    if path.exists():
        saved,_=_raw(job,'post-delivery-history/'+name)
        if saved!=value:raise ValueError('post_compare_history_conflict')
    else:
        # Atomic no-clobber publish: final archive can NEVER be replaced, even
        # under an accidental competing private writer. Partial temp survives a
        # failed fsync for reconciliation, never admitted as complete history.
        temporary=d/('.'+token.ticket['id']+'-'+uuid.uuid4().hex+'.pending')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,path,follow_symlinks=False)
        except FileExistsError:
            if _raw(job,'post-delivery-history/'+name)[0]!=value:
                raise ValueError('post_compare_history_conflict')
        directory=os.open(d,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        temporary.unlink()
        directory=os.open(d,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    return value,{'schema':1,'id':token.ticket['id'],'file':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


@dataclass(eq=False)
class _Session:
    seal:object
    job:object
    envelope:dict
    fresh:dict
    ticket:dict
    config_sha:str
    canonical_sha:str
    binding:object=None
    last_wall:float=0.
    last_mono:float=0.
    ui_deadline:float=0.


def _persist(job,fresh):
    session=getattr(job,'_post_compare_session',None)
    if session is None:return False
    if not isinstance(session,_Session) or session.seal is not _SEAL or session.job is not job or session.fresh is not fresh:
        raise ValueError('post_compare_persist_scope_invalid')
    _guard(job,session)
    session.envelope['post_delivery_compare']['comparison']=copy.deepcopy(fresh)
    _J().atomic(job.directory/'status.json',session.envelope)
    return True


def _guard(job,session):
    if (not isinstance(session,_Session) or session.seal is not _SEAL or session.job is not job
            or getattr(job,'_post_compare_session',None) is not session):raise ValueError('post_compare_session_invalid')
    if _raw(job,'config.json')[1]!=session.config_sha or _raw(job,'canonical.json')[1]!=session.canonical_sha:
        raise ValueError('post_compare_inputs_changed')
    ticket=job.read('resume.json')
    if (ticket.get('state')!='consumed' or ticket.get('id')!=session.ticket['id']
            or ticket.get('authorized_ticket')!=session.ticket):raise ValueError('post_compare_consumed_ticket_changed')
    turn=job.read('post-delivery-turn.json')
    expected={'schema':1,'turn_id':session.ticket['turn_id'],'ticket_id':session.ticket['id'],
              'ticket_sha256':E.digest(session.ticket),'resident':session.ticket['resident'],
              'issued':session.ticket['issued'],'expires':session.ticket['expires']}
    now=time.time();mono=time.monotonic()
    if (not _finite(mono) or now<session.last_wall or mono<session.last_mono
            or abs((now-session.last_wall)-(mono-session.last_mono))>5):
        raise ValueError('post_compare_clock_rollback_or_discontinuity')
    session.last_wall=now;session.last_mono=mono
    if mono>=session.ui_deadline:raise ValueError('post_compare_ui_deadline_expired')
    if turn!=expected or not _finite(now) or not session.ticket['issued']<=now<session.ticket['expires']:
        raise ValueError('post_compare_turn_revoked_or_expired')
    if job.read('status.json')!=session.envelope:raise ValueError('post_compare_status_changed')
    return True


async def compare(job,token):
    J=_J();runtime=job.runtime;binding=None;session=None
    try:
        async with runtime.lock:
            _recheck(job,token)
            history,ref=_archive(job,token)
            _recheck(job,token)  # Inputs may have changed during fsync in another trusted process.
            if (history['previous_status']!=job.read('status.json')
                    or history['config']!=job.read('config.json')
                    or history['canonical']!=job.read_canonical()):
                raise ValueError('post_compare_archived_inputs_changed')
            envelope=copy.deepcopy(history['previous_status'])
            envelope.update(state='delivery_reconciliation_required',phase='post_delivery_compare_admitting',
                            reason='post_delivery_compare_only',post_delivery_history=ref,
                            post_delivery_compare={'schema':1,'kind':_KIND,'ticket_id':token.ticket['id'],
                                                   'resident':token.ticket['resident'],'state':'admitting',
                                                   'historical_status_sha256':E.digest(history['previous_status'])})
            # Durable downgrade fence BEFORE consume/acquire. Crash here is held,
            # never auto-consumed by C and never exporter mode in B-fence.
            J.atomic(job.directory/'status.json',envelope)
            consumed=copy.deepcopy(token.ticket)
            consumed.update(state='consumed',consumed_at=time.time(),previous_status=copy.deepcopy(history['previous_status']),
                            authorized_ticket=copy.deepcopy(token.ticket),history=ref)
            J.atomic(job.directory/'resume.json',consumed)
            marker=envelope['post_delivery_compare'];marker['state']='consumed'
            envelope['phase']='post_delivery_compare_running'
            J.atomic(job.directory/'status.json',envelope)
            fresh={'request':token.ticket['request'],'state':'running','started':time.time(),'uploads':{}}
            session=_Session(_SEAL,job,envelope,fresh,token.ticket,token.inputs['config.json'],token.inputs['canonical.json'])
            session.last_wall=token.wall;session.last_mono=token.mono
            job._post_compare_session=session
            budget=PhaseDeadline(J.UI_BUDGET)
            session.ui_deadline=budget.ends
            _guard(job,session)  # Consumed ticket, current turn/config/canonical BEFORE boot.
            runtime.background_owner=J.KEY
            try:
                async with asyncio.timeout(budget.remaining()):
                    await J._drain_scheduled_task(asyncio.create_task(runtime._acquire(J.KEY,False)))
                binding=J._ScheduledBinding(runtime);session.binding=binding
            except BaseException:
                # Drain may finish acquisition after parent cancellation. Bind that
                # SAME generation before leaving lock; stop OUTSIDE this lock.
                if runtime.owner==J.KEY and runtime.background_owner==J.KEY:
                    binding=J._ScheduledBinding(runtime);session.binding=binding
                elif runtime.owner is None and runtime.authority.inspect() is None:
                    runtime.background_owner=None
                raise
    except BaseException:
        if session is not None:
            marker.update(state='incomplete',reason='post_compare_acquisition_failed')
            try:J.atomic(job.directory/'status.json',envelope)
            finally:
                try:
                    if binding is not None:await J._scheduled_release(job,binding)
                finally:job._post_compare_session=None
        raise
    async def call(args):
        _guard(job,session)
        result=await J._scheduled_call(runtime,binding,args)
        _guard(job,session)
        return result
    try:
        async with asyncio.timeout(budget.remaining()):
            _guard(job,session)
            await job.preflight(J.OfficialUI(call),fresh)
            _guard(job,session)
            if budget.remaining()<=0:raise TimeoutError()
            if fresh.get('source_gate')=='all_four_verified':
                selected,pending,unchanged=E.selection(job._eligible_preflight,fresh,job.read_canonical())
            else:selected=[];pending=list(J.GROUPS);unchanged=[]
            # Comparison proof retained, but NEVER delivery authorization.
            outcome={'schema':1,'comparison_only':True,'novelty_requires_authorization':selected,
                                             'pending_ui':pending,'unchanged':unchanged,'delivery_authorized':False,
                                             'all_groups_ui_resolved':not pending,'G5_qualified':False}
            marker.update(state='captured',comparison=copy.deepcopy(fresh))
            final_reason='post_compare_novelty_requires_authorization' if selected else 'post_compare_ui_pending' if pending else 'post_compare_no_new_text'
            envelope.update(phase='post_delivery_compare_cleanup_pending',reason='post_compare_cleanup_pending')
    except BaseException as exc:
        marker.update(state='incomplete',reason='post_compare_cancelled' if isinstance(exc,asyncio.CancelledError) else 'post_compare_failed')
        envelope.update(phase='post_delivery_compare_incomplete',reason=marker['reason'])
        raise
    finally:
        persisted=False
        try:
            J.atomic(job.directory/'status.json',envelope);persisted=True
        finally:
            try:
                await J._scheduled_release(job,binding)  # SAME task, even repeat cancel.
                if marker['state']=='captured':
                    if not persisted:raise ValueError('post_compare_persistence_failed')
                    _free(job)  # Refuse a replacement owner or pending cleanup; never stop it.
                    _guard(job,session)  # OFF cleanup drain is not a deadline renewal.
                    marker['state']='finished'
                    envelope.update(phase='post_delivery_compare_finished',reason=final_reason,
                                    post_delivery_result=outcome)
            except BaseException as exc:
                marker.update(state='incomplete',reason='post_compare_cleanup_cancelled' if isinstance(exc,asyncio.CancelledError) else 'post_compare_cleanup_or_deadline_failed')
                envelope.pop('post_delivery_result',None)
                envelope.update(phase='post_delivery_compare_incomplete',reason=marker['reason'])
                raise
            finally:
                marker['finished']=time.time()
                try:J.atomic(job.directory/'status.json',envelope)
                finally:job._post_compare_session=None;job._eligible_preflight=None
    # NO verify_delivery, CloudWindow, fresh_transport, intent, export or run.


async def _source_read(job,argv):
    session=getattr(job,'_post_compare_session',None)
    if session is not None:
        _guard(job,session)
        prefix=['ssh','sasocq-server','sudo','-u','whatsapp-import','/usr/bin/python3']
        if (argv[:6]!=prefix or len(argv)!=9 or argv[6] not in (
                '/opt/whatsapp-import/collector_verify.py','/opt/whatsapp-import/collector_baseline.py')
                or argv[-1] not in _J().GROUPS):raise ValueError('post_compare_source_read_only')
    value=await job.runtime._host(argv)
    if session is not None:_guard(job,session)
    return value
