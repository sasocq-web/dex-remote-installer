"""Resolve native probe IDs against current scoped contracts, never feedback text.

Development candidate. This does not classify an account/profile or publish a
positive observation. Those semantics must be supplied by an audited producer.
"""
import asyncio
import hashlib
import json
import re
from .feedback_readers import PROJECT, registry
from .feedback_native_receipts import NativeReceiptStore, protected

MODULE_DEPENDENCIES = {
    'pdc': {'android_nc_pdc'},
    'avaliacao': {'android_nc_avaliacao'},
    'apontamento': {'android_nc_apontamento', 'android_legitimate_pointing_session', 'android_pointing_session'},
    'ssm': {'android_ssm_session'},
    'controles': {'android_nc_controls'},
}


def identifiers(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {'event_id', 'dependency_id'}:
        raise ValueError('native probe accepts only event/dependency IDs')
    event = arguments['event_id']; dep = arguments['dependency_id']
    if not isinstance(event, str) or not re.fullmatch('[a-f0-9]{64}', event):
        raise ValueError('native event ID')
    if not isinstance(dep, str) or not re.fullmatch('[a-z][a-z0-9_]{0,100}', dep):
        raise ValueError('native dependency ID')
    return event, dep


def resolve(worker, arguments, workspace, thread):
    event, dep = identifiers(arguments)
    pinned = registry().get(event)
    if not pinned or dep not in pinned['dependencies']:
        raise ValueError('native dependency not registered')
    if (pinned['dependencies'][dep]['kind'] != 'authenticated_session'
            or dep not in MODULE_DEPENDENCIES.get(pinned['module'], set())):
        raise ValueError('native session reader not registered')
    # The inventory operation applies the same cancellation, cutoff, adoption,
    # route-path and enabled-site fences as the automatic resumption coordinator.
    rows = [r for r in worker.call('resumption_inventory') if r.get('event_id') == event]
    if len(rows) != 1:
        raise ValueError('native event outside current scope')
    row = rows[0]
    target = worker.routes.get(row.get('site'))
    if (row.get('state') not in {'blocked', 'validation_pending'}
            or row.get('site') not in {'nc.sasocq.com', 'controles.sasocq.com'}
            or not target or target.get('project_id') != PROJECT):
        raise ValueError('native event no longer held by owner')
    if (workspace != 'system' and
            (workspace != 'project:' + PROJECT or thread != target.get('thread_id'))):
        raise ValueError('native probe requires canonical resident')
    document = json.loads(row.get('document') or '{}')
    required=document.get('required_dependency_ids')
    if not isinstance(required,list) or not required or len(required)>100:
        raise ValueError('native contract dependency list')
    if (row.get('revision') != pinned['contract_revision']
            or document.get('source_report_id') != pinned['source_report_id']
            or document.get('owner_project') != PROJECT
            or document.get('resident') != target.get('thread_id')
            or dep not in required):
        raise ValueError('native contract changed')
    return {'event_id': event, 'dependency_id': dep,
            'contract_revision': pinned['contract_revision'],
            'source_report_id': pinned['source_report_id'],
            'module': pinned['module'], 'site': row['site'],
            'project_id': PROJECT, 'resident': target['thread_id'],
            'required_dependency_ids':required}


def unchanged(before, after):
    if before != after:
        raise ValueError('native contract changed during collection')


def require_owned_turn(lifecycle, workspace, thread, turn_id):
    """Call while holding lifecycle.lock and before any broker/runtime operation."""
    if (lifecycle.owner != (workspace, thread) or lifecycle.cleanup_failed
            or not lifecycle.turn_active or not turn_id
            or lifecycle.turn_id != turn_id or not lifecycle.marker.is_file()
            or lifecycle.marker.is_symlink()):
        raise ValueError('native probe requires own active Android turn')
    marker = lifecycle.marker.stat()
    # Bind receipt to the particular ownership marker, not only a reusable
    # workspace/thread pair; a later lease must not consume the old witness.
    return f'{marker.st_dev}:{marker.st_ino}:{marker.st_ctime_ns}:{turn_id}'


async def collect(lifecycle, worker, arguments, workspace, thread, turn_id, *, publish_observation=False):
    """Internal fixed collector. Returns a witness, never a satisfied condition.

    Must not be exposed as a caller-provided receipt/facts endpoint. The public
    native dispatcher, once integrated, may supply only the two identifiers.
    """
    identifiers(arguments)  # Reject hostile input before locking or any IO.
    async with lifecycle.lock:
        lease = require_owned_turn(lifecycle, workspace, thread, turn_id)
        binding = await asyncio.to_thread(resolve, worker, arguments, workspace, thread)
        if publish_observation:
            from .feedback_native_policy import policy
            policy(binding)  # Unknown profile policies fail before device IO.
        from system.android_nc_probe import fixed_source
        code_hash = hashlib.sha256(fixed_source(binding['module']).encode()).hexdigest()
        scope = {k: binding[k] for k in (
            'event_id', 'dependency_id', 'contract_revision', 'source_report_id')}
        scope.update(workspace=workspace, thread=thread, turn=turn_id, lease=lease,
                     origin='https://nc.sasocq.com', device='android', code_sha256=code_hash)
        worker.state_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
        protected(worker.state_dir)
        store = NativeReceiptStore(worker.state_dir / 'native-receipts', clock=worker.clock)
        try:
            nonce = store.issue(scope)
            output = await lifecycle._host([
                'timeout', '40', lifecycle.native_probe_helper, 'observe_nc', binding['module']])
            value = json.loads(output)
            if (not isinstance(value, dict)
                    or set(value) != {'result', 'collected_at', 'code_sha256'}
                    or value['code_sha256'] != code_hash):
                raise ValueError('native collector source mismatch')
            # Re-read current contract after actual device IO: no observation is
            # admitted if cancellation, ownership or revision changed meanwhile.
            current = await asyncio.to_thread(resolve, worker, arguments, workspace, thread)
            unchanged(binding, current)
            def same_lease(expected):
                return require_owned_turn(lifecycle, workspace, thread, turn_id) == expected['lease']
            if not same_lease(scope):
                raise ValueError('native lease changed during collection')
            receipt = store.seal(nonce, value['result'], value['collected_at'])
            witness=store.consume(receipt, scope, same_lease)
            if not publish_observation:return witness
            from .feedback_native_policy import classify
            from .feedback_observation_store import publish
            observation=classify(binding,witness,worker.clock())
            await asyncio.to_thread(publish,worker.state_dir,binding,observation,worker.clock())
            # Capability and authenticated session are independent conditions.
            # Attach only decoded captures actually collected in this same lease.
            from .feedback_capture_proof import observation as capture_observation
            capture=(capture_observation(binding,getattr(lifecycle,'capture_proofs',{}),lease,worker.clock())
                     if 'android_capture' in binding['required_dependency_ids'] else None)
            if capture is not None:
                await asyncio.to_thread(publish,worker.state_dir,binding,capture,worker.clock())
            return {'observation':observation,'capture_recorded':capture is not None,'recorded':True}
        finally:
            store.close()
