"""Fail-closed selection of independently eligible groups, fixed four-source gate.
Private Python preflight proof + durable additive policy; no public flags/UI.
"""
from .whatsapp_collector import GROUPS as _GROUPS
from dataclasses import dataclass
import hashlib,json,re
from .collector_evidence import canonical_tokens,CanonicalGap,compatible_window,token_stamp,key
_SEAL=object()

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
def _jobs():
    from . import android_jobs
    return android_jobs

def baseline_tokens(value):
    if not isinstance(value,list):raise ValueError('eligible_baseline_invalid')
    rows=canonical_tokens(value)
    if any(token_stamp(a)>token_stamp(b) for a,b in zip(rows,rows[1:])):
        raise ValueError('eligible_baseline_unordered')
    return rows

def classification(item,rows=None):
    comparison=item.get('comparison')
    if comparison is None:
        if item.get('decision')!='pending':raise ValueError('eligible_decision_unproved')
        return 'pending'
    if not isinstance(comparison,dict):raise ValueError('eligible_decision_invalid')
    state=comparison.get('state');export=comparison.get('may_export')
    if state=='pending_reconciliation' and export is False:
        if item.get('decision')!='pending':raise ValueError('eligible_decision_conflict')
        return 'pending'
    if state not in {'new_participant_sequence','visible_tail_matches'}:
        raise ValueError('eligible_decision_invalid')
    anchor,new=comparison.get('anchor_records'),comparison.get('new_occurrences');evidence=comparison.get('evidence_sha256')
    if (type(anchor) is not int or not 2<=anchor<=32 or type(new) is not int
            or not 0<=new<=198 or anchor+new>200 or not isinstance(evidence,str)
            or not re.fullmatch(r'[0-9a-f]{64}',evidence)
            or comparison.get('coverage')!='visible_text_suffix_only'
            or comparison.get('media_coverage')!='unproven'):
        raise ValueError('eligible_sequence_proof_invalid')
    if rows is not None:
        tail=rows[-anchor:]
        if (len(tail)!=anchor or any(isinstance(t,CanonicalGap) for t in tail)
                or sum(compatible_window(rows[i:i+anchor],tail) for i in range(len(rows)-anchor+1))!=1):
            raise ValueError('eligible_original_anchor_ambiguous')
        for token in tail:
            key(dict(stamp=token[0],sender=token[1],body=token[2]))  # Original text-only contract.
    if state=='new_participant_sequence':
        if export is not True or new<1 or item.get('decision')!='export':raise ValueError('eligible_decision_conflict')
        return 'export'
    if export is not False or new!=0 or item.get('decision')!='unchanged_visible_suffix':raise ValueError('eligible_decision_conflict')
    return 'unchanged'

def _analyse(state,baselines=None):
    J=_jobs();groups=state.get('reconciliation')
    if set(J.GROUPS)!=set(_GROUPS):raise ValueError('eligible_fixed_four_sources_required')
    if not isinstance(groups,dict) or set(groups)!=set(J.GROUPS):raise ValueError('eligible_all_sources_required')
    selected=[];pending=[];unchanged=[];sources={}
    for code in J.GROUPS:
        item=groups[code]
        if not isinstance(item,dict) or item.get('global_guard_failure') is True:raise ValueError('eligible_global_guard_failed')
        receipt=item.get('receipt');J.receipt_identity(receipt)
        if item.get('baseline_verified') is not True or item.get('baseline_sha256')!=receipt['sha256'] or type(item.get('baseline_records')) is not int or item['baseline_records']<0:
            raise ValueError('eligible_all_baselines_required')
        # source_days validates date/allowlist, never treats a UI date as source.
        sources[code]={'source_day':item.get('source_day'),'receipt':{k:receipt[k] for k in ('id','sha256','crc','imported')},'baseline_records':item['baseline_records']}
        rows=None
        if baselines is not None:
            if code not in baselines:raise ValueError('eligible_all_baselines_required')
            rows=baseline_tokens(baselines[code])
            if len(rows)!=item['baseline_records']:raise ValueError('eligible_baseline_count_changed')
        which=classification(item,rows)
        (selected if which=='export' else pending if which=='pending' else unchanged).append(code)
    if len({v['receipt']['id'] for v in sources.values()})!=len(_GROUPS):raise ValueError('eligible_source_identity_conflict')
    J.source_days({'schema':1,'groups':sources})
    return tuple(selected),tuple(pending),tuple(unchanged),sources

@dataclass(frozen=True)
class _PreflightProof:
    seal:object
    state:object
    registry_digest:str
    reconciliation_digest:str
    selected:tuple
    pending:tuple
    unchanged:tuple
    sources_json:str
    request:object
    binding:tuple
    used:bool=False

def issue_preflight(state,registry,baselines):
    J=_jobs();selected,pending,unchanged,sources=_analyse(state,baselines);known=J.source_days(registry)
    for code in J.GROUPS:
        if known[code]!=sources[code]['source_day']:raise ValueError('eligible_original_source_changed')
        if registry and J.receipt_identity(registry['groups'][code]['receipt'])!=J.receipt_identity(sources[code]['receipt']):raise ValueError('eligible_original_source_changed')
    reg,rec=digest(registry),digest(state['reconciliation']);data=json.dumps(sources,sort_keys=True)
    bound=(id(state),reg,rec,selected,pending,unchanged,data,state.get('request'))
    return _PreflightProof(_SEAL,state,reg,rec,selected,pending,unchanged,data,state.get('request'),bound)

def selection(proof,state,registry):
    if (not isinstance(proof,_PreflightProof) or proof.seal is not _SEAL or proof.used or proof.state is not state
            or proof.binding!=(id(state),proof.registry_digest,proof.reconciliation_digest,proof.selected,proof.pending,proof.unchanged,proof.sources_json,proof.request)
            or proof.request!=state.get('request') or proof.registry_digest!=digest(registry) or proof.reconciliation_digest!=digest(state.get('reconciliation'))):
        raise ValueError('eligible_preflight_proof_missing_or_changed')
    return list(proof.selected),list(proof.pending),list(proof.unchanged)

def _scope(state):
    return digest({k:state.get(k) for k in ('request','source_day','selected','pending_groups','unchanged_groups','reconciliation')})

def persist_policy(proof,state,registry,day):
    J=_jobs();selected,pending,unchanged=selection(proof,state,registry)
    if not selected:raise ValueError('eligible_no_selected_group')
    if not isinstance(state.get('request'),str) or not state['request']:raise ValueError('eligible_request_invalid')
    if any(state['reconciliation'][c]['source_day']>=day for c in selected):raise ValueError('same_day_source_requires_reconciliation')
    object.__setattr__(proof,'used',True)
    state.update(source_day=day,selected=selected,pending_groups=pending,unchanged_groups=unchanged)
    state['eligible_delivery']={'schema':1,'selected':selected.copy(),'pending':pending.copy(),'unchanged':unchanged.copy(),'original_registry_digest':digest(registry),'sources':json.loads(proof.sources_json),'scope':_scope(state)}
    validate(state)

def has_protocol(state):
    groups=state.get('reconciliation', {})
    return (any(k in state for k in ('eligible_delivery','pending_groups','source_gate','delivery_result'))
            or (isinstance(groups,dict) and any(isinstance(v,dict) and 'baseline_verified' in v for v in groups.values())))

def validate(state):
    """New protected durable handoff only; old no-marker states remain legacy.
    No caller field can replace issue_preflight BEFORE the first intent.
    """
    if 'eligible_delivery' not in state:
        if has_protocol(state) and (state.get('uploads') or state.get('state')=='verification_pending'):
            raise ValueError('eligible_policy_missing_requires_reconciliation')
        return None
    p=state['eligible_delivery']
    if not isinstance(p,dict) or type(p.get('schema')) is not int or p['schema']!=1:raise ValueError('eligible_policy_requires_supported_runtime')
    selected,pending,unchanged,sources=_analyse(state)
    if (list(selected)!=p.get('selected') or list(pending)!=p.get('pending') or list(unchanged)!=p.get('unchanged')
            or state.get('selected')!=list(selected) or state.get('pending_groups')!=list(pending) or state.get('unchanged_groups')!=list(unchanged)
            or sources!=p.get('sources') or p.get('scope')!=_scope(state) or not selected):
        raise ValueError('eligible_policy_scope_changed')
    old={'schema':1,'groups':{c:{'source_day':item['source_day'],'receipt':item['receipt']} for c,item in sources.items()}}
    # Initial bootstrap registry{} has its own digest; all four verified sources
    # are still embedded. No current/new canonical value is silently substituted.
    if p.get('original_registry_digest') not in {digest(old),digest({})}:raise ValueError('eligible_policy_original_registry_invalid')
    day=state.get('source_day')
    if not isinstance(day,str) or not re.fullmatch(r'\d{8}',day):raise ValueError('eligible_policy_day_invalid')
    import datetime as dt
    dt.datetime.strptime(day,'%Y%m%d')
    if any(sources[c]['source_day']>=day for c in selected):raise ValueError('eligible_policy_same_day_conflict')
    return p

def finish(state):
    p=validate(state)
    if p is None:
        state.update(state='verified',phase='complete');return
    pending=p['pending']
    state.update(state='pending_reconciliation' if pending else 'verified',phase='selected_verified_pending_ui' if pending else 'complete',
                 delivery_result={'selected_verified':p['selected'].copy(),'pending_ui':pending.copy(),'partial':bool(pending),'all_groups_ui_resolved':not pending})
    if pending:state['reason']='selected_delivery_verified_ui_pending'
    else:state.pop('reason',None)

def allow_new_cycle(state,request,registry):
    p=validate(state)
    if p is None:return
    if request==state.get('request') or state.get('phase') not in {'complete','selected_verified_pending_ui'}:
        raise ValueError('eligible_previous_delivery_requires_reconciliation')
    J=_jobs();proof=state.get('proof',{})
    # Reuse the ORIGINAL atomic crash-recovery verifier, not relaxed equality.
    if J.advance_sources(registry,state['reconciliation'],state['source_day'],proof,p['selected'])!=registry:
        raise ValueError('eligible_previous_commit_unproved')


def restore_runtime_fence(state,registry):
    """Typed A->B status recovery only; NEVER renew budget or submit effects."""
    f=state.get('eligible_runtime_fence')
    if f is None or (isinstance(f,dict) and f.get('restored') is True):return False
    if (not isinstance(f,dict) or type(f.get('schema')) is not int or f['schema']!=1
            or f.get('request')!=state.get('request')
            or state.get('reason')!='eligible_delivery_requires_supported_runtime'):
        raise ValueError('eligible_runtime_fence_invalid')
    snapshot={k:v for k,v in state.items() if k!='eligible_runtime_fence'}
    for field in ('state','phase','reason'):snapshot[field]=f.get('previous_'+field)
    if 'predecessor_digest' in f:
        if f['predecessor_digest']!=digest(snapshot):raise ValueError('eligible_runtime_fence_predecessor_changed')
    elif set(f)!={'schema','request','policy_scope','previous_state','previous_phase','previous_reason','restored'} or f.get('restored') is not False:
        # EXACT private checkpoint v1 of the pinned System fenceA9093759c.
        # Scope/submission/atomic-commit checks below still decide; no defaults.
        raise ValueError('eligible_runtime_fence_checkpoint_unsupported')
    p=validate(state)
    if f.get('policy_scope')!=(p.get('scope') if p else None):raise ValueError('eligible_runtime_fence_scope_changed')
    previous=f.get('previous_state');phase=f.get('previous_phase')
    if previous=='verified' and phase!='complete':raise ValueError('eligible_runtime_fence_phase_conflict')
    if previous=='visible_suffixes_match' and (p is not None or state.get('uploads')):
        raise ValueError('eligible_runtime_fence_nochange_unproved')
    if phase is not None and (not isinstance(phase,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}',phase)):
        raise ValueError('eligible_runtime_fence_phase_invalid')
    if previous=='verification_pending':
        if p is None:raise ValueError('eligible_runtime_fence_submission_unproved')
        # Existing original guard: every SELECTED intent must be submitted.
        _jobs().submitted_delivery(dict(state,state='verification_pending'),_jobs().GROUPS)
        state.update(state='verification_pending',phase=phase)
    elif previous in {'verified','pending_reconciliation'} and phase in {'complete','selected_verified_pending_ui'}:
        if p is None or _jobs().advance_sources(registry,state['reconciliation'],state['source_day'],state.get('proof',{}),p['selected'])!=registry:
            raise ValueError('eligible_runtime_fence_commit_unproved')
        finish(state) # Display of ALREADY atomic-committed delivery; no new success.
    elif previous in {'pending_reconciliation','visible_suffixes_match'} and not state.get('uploads') and p is None:
        if previous=='visible_suffixes_match':
            selected,pending,unchanged,_=_analyse(state)
            if selected or pending or len(unchanged)!=len(_GROUPS):raise ValueError('eligible_runtime_fence_nochange_unproved')
        state.update(state=previous,phase=phase)
    else:
        # Running/partial intent/cancel/error remains held for reconciliation.
        state.update(state='delivery_reconciliation_required',phase='cloud_blocked',reason='eligible_runtime_fence_interrupted_delivery')
        f['restored']=True
        return True
    if state.get('phase') not in {'complete','selected_verified_pending_ui'}:
        if f.get('previous_reason') is not None:state['reason']=f['previous_reason']
        else:state.pop('reason',None)
    f['restored']=True
    return True
