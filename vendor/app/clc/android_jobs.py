"""Trusted background job using the SAME conversation AndroidLifecycle.

Only the fixed WhatsApp collector is registered; no generic job, shell or public
API. Configuration is local protected Control Plane state. Absent config is off.
"""
import asyncio
import contextvars
import math
import base64
import gzip
import hashlib
import io
import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .whatsapp_collector import OfficialUI, GROUPS, ExportError
from .whatsapp_cursor import page
from .whatsapp_frames import PhysicalTailCursor
from .device_date import read_device_day
from .collector_phases import PhaseDeadline, fresh_transport, submitted_delivery

from .collector_cloud_budget import CloudWindow, work_with_drain, MAX_ATTEMPTS, scope as cloud_scope
import uuid
from . import eligible_delivery as eligible_policy
from . import post_delivery_compare as post_compare

UI_BUDGET = 24*60
GROUP_BUDGET = 240
KEY = ('system', 'scheduled-whatsapp')
ZONE = ZoneInfo('America/Sao_Paulo')
BOOTSTRAP_SOURCES = {'REDUC':'20261029', 'ADM':'20261030',
                     'IPS':'20261030', 'GESTAO-IPS':'20261030'}


# Python-internal only; never a caller JSON fact or a wire capability.
_SCHEDULED_SEAL = object()
_SCHEDULED_UI = frozenset({'open','ui','tap','type','swipe','long_press','back',
                           'home','orientation','pinch','screenshot','apps','is-installed'})

class _ScheduledBinding:
    def __init__(self, runtime):
        self.seal = _SCHEDULED_SEAL
        self.runtime = runtime
        self.generation = getattr(runtime, 'durable_generation', None)
        self.failed = False


def _require_scheduled_binding(runtime, binding, key, args):
    # Called by execute INSIDE its existing non-reentrant lock. No await here:
    # owner validation and native dispatch/acquire share that SAME lock.
    if (not isinstance(binding, _ScheduledBinding) or binding.seal is not _SCHEDULED_SEAL
            or binding.runtime is not runtime or binding.failed or key != KEY
            or runtime.owner != KEY or runtime.background_owner != KEY
            or runtime.cleanup_failed
            or not isinstance(binding.generation, str)
            or not re.fullmatch(r'[0-9a-f]{32}', binding.generation)
            or getattr(runtime, 'durable_generation', None) != binding.generation
            or args.get('operation') not in _SCHEDULED_UI):
        raise ValueError('scheduled_android_reservation_lost')
    lease = runtime.authority.inspect()
    if (not lease or lease['generation'] != binding.generation
            or tuple(lease['owner']) != KEY or lease['phase'] != 'held'):
        raise ValueError('scheduled_android_generation_unavailable')


async def _scheduled_call(runtime, binding, args):
    if (not isinstance(binding, _ScheduledBinding) or binding.seal is not _SCHEDULED_SEAL
            or binding.runtime is not runtime):
        raise ValueError('scheduled_android_binding_invalid')
    # Protect/drain ONE execute task, not just its inner IPC. Parent timeout
    # must NOT reach execute's native BaseException->stop path. Real operation
    # errors still use that native path and permanently invalidate this binding.
    async def dispatch():
        try:
            return await runtime.execute(*KEY, args, turn_active=False,
                                         _scheduler_binding=binding)
        except BaseException:
            binding.failed = True
            raise
    return await _drain_scheduled_task(asyncio.create_task(dispatch()))


async def _drain_scheduled_task(task):
    # One task, no retry/detach; also used for the scheduler final stop.
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


_BOOT_SCOPE = contextvars.ContextVar('scheduled_boot_scope', default=None)
_BOOT_SEAL = object()

class _BootScope:
    def __init__(self, runtime, deadline):
        self.seal, self.runtime, self.deadline = _BOOT_SEAL, runtime, deadline
        self.cancelled = False
        self.task = None
        self.generation = None

def _scheduled_boot_check(runtime):
    """Private same-task checkpoint. Default native callers have NO scope.
    Never called from _stop: cancellation must not cancel cleanup itself.
    """
    scope = _BOOT_SCOPE.get()
    if scope is None:return
    if (not isinstance(scope, _BootScope) or scope.seal is not _BOOT_SEAL
            or scope.runtime is not runtime or scope.task is not asyncio.current_task()):
        raise ValueError('scheduled_boot_scope_invalid')
    if scope.cancelled:raise asyncio.CancelledError()
    now = time.monotonic()
    if (type(scope.deadline) not in (int, float) or not math.isfinite(scope.deadline)
            or not math.isfinite(now) or now >= scope.deadline):
        raise TimeoutError('scheduled_boot_absolute_deadline')
    lease = runtime.authority.inspect()
    generation = getattr(runtime, 'durable_generation', None)
    if (not lease or lease['phase'] != 'held' or tuple(lease['owner']) != KEY
            or lease['generation'] != generation or runtime.background_owner != KEY
            or runtime.owner not in (None, KEY) or runtime.cleanup_failed
            or not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{32}', generation)
            or scope.generation not in (None, generation)):
        raise ValueError('scheduled_boot_generation_unavailable')
    scope.generation = generation


async def _scheduled_acquire(runtime, deadline):
    """Caller holds runtime.lock. ONE acquisition task, no native cancel/reboot.

    A partial durable lease may exist before native owner/marker assignment.
    Only a lease created by this attempt under this lock may be cleaned here.
    No timeout or socket exception is interpreted as OFF/free.
    """
    if runtime.authority.inspect() is not None:
        if runtime.owner is None and runtime.background_owner == KEY:
            runtime.background_owner = None  # No lease was acquired by THIS attempt.
        raise ValueError('scheduled_acquire_authority_not_free')
    scope = _BootScope(runtime, deadline)
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or time.monotonic() >= deadline:
        runtime.background_owner = None
        raise TimeoutError('scheduled_boot_absolute_deadline')
    async def acquire():
        scope.task = asyncio.current_task()
        token = _BOOT_SCOPE.set(scope)
        try:
            return await runtime._acquire(KEY, False)
        finally:
            _BOOT_SCOPE.reset(token)
    task = asyncio.create_task(acquire())
    cancelled = False
    try:
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():raise
                cancelled = True
                scope.cancelled = True  # Stops NEXT boot step, NEVER cleanup.
            except BaseException:
                if cancelled:raise asyncio.CancelledError() from None
                raise
        if cancelled:raise asyncio.CancelledError()
        if time.monotonic() >= deadline:raise TimeoutError('scheduled_boot_absolute_deadline')
        binding = _ScheduledBinding(runtime)
        _require_scheduled_binding(runtime, binding, KEY, {'operation':'ui'})
        return binding  # Held generation/TTL checked BEFORE source/UI work.
    except BaseException:
        lease = runtime.authority.inspect()
        generation = getattr(runtime, 'durable_generation', None)
        if (lease is not None and isinstance(generation, str)
                and re.fullmatch(r'[0-9a-f]{32}', generation)
                and lease['generation'] == generation and tuple(lease['owner']) == KEY
                and runtime.owner in (None, KEY) and runtime.background_owner == KEY):
            # Already inside SAME lock; _stop does not acquire it. Shield/drain
            # its SAME task, including repeated cancellation. Never stop a
            # replacement owner/generation or clear an uncertain reservation.
            try:
                await _drain_scheduled_task(asyncio.create_task(runtime._stop()))
            finally:
                if (runtime.owner is None and runtime.background_owner == KEY
                        and getattr(runtime, 'durable_generation', None) is None
                        and runtime.cleanup_failed is False and runtime.authority.inspect() is None):
                    runtime.background_owner = None
        elif (runtime.owner is None and runtime.background_owner == KEY
                and generation is None and runtime.cleanup_failed is False and lease is None):
            runtime.background_owner = None
        raise


async def _scheduled_release(job, binding):
    if (not isinstance(binding, _ScheduledBinding) or binding.seal is not _SCHEDULED_SEAL
            or binding.runtime is not job.runtime):
        raise ValueError('scheduled_android_binding_invalid')
    return await _drain_scheduled_task(asyncio.create_task(job.release(binding)))


def _record_eligible_runtime_fence(state):
    prior=state.get('eligible_runtime_fence')
    if isinstance(prior,dict) and prior.get('restored') is not True:
        return
    policy=state.get('eligible_delivery')
    state['eligible_runtime_fence']={'schema':1,'request':state.get('request'),
        'policy_scope':policy.get('scope') if isinstance(policy,dict) else None,
        'previous_state':state.get('state'),'previous_phase':state.get('phase'),
        'previous_reason':state.get('reason'),'restored':False}



def _post_delivery_marked(state):
    return any(k in state for k in ('post_delivery_compare','post_delivery_history','post_delivery_result'))

def receipt_identity(receipt):
    if (not isinstance(receipt, dict) or not isinstance(receipt.get('id'), str)
            or not receipt['id'] or not isinstance(receipt.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', receipt['sha256'])
            or receipt.get('crc') != 'ok' or receipt.get('imported') is not True):
        raise ValueError('canonical_receipt_invalid')
    return receipt['id'], receipt['sha256']


def source_days(registry):
    if registry == {}:
        return dict(BOOTSTRAP_SOURCES)
    if (not isinstance(registry, dict) or registry.get('schema') != 1
            or not isinstance(registry.get('groups'), dict)
            or set(registry['groups']) != set(GROUPS)):
        raise ValueError('canonical_registry_invalid')
    result = {}
    for code, item in registry['groups'].items():
        day = item.get('source_day') if isinstance(item, dict) else None
        if not isinstance(day, str) or not re.fullmatch(r'\d{8}', day):
            raise ValueError('canonical_source_day_invalid')
        dt.datetime.strptime(day, '%Y%m%d')
        if day < BOOTSTRAP_SOURCES[code]:
            raise ValueError('canonical_source_regressed')
        receipt_identity(item.get('receipt'))
        result[code] = day
    return result


def advance_sources(registry, reconciliation, day, proof, selected):
    """Commit pointers only from fresh verified receipts, never UI intent."""
    previous = source_days(registry)
    if not isinstance(day, str) or not re.fullmatch(r'\d{8}', day):
        raise ValueError('canonical_source_day_invalid')
    dt.datetime.strptime(day, '%Y%m%d')
    if (not selected or len(selected) != len(set(selected)) or
            not set(selected) <= set(GROUPS) or proof.get('state') != 'verified' or
            set(proof.get('groups', {})) != set(selected)):
        raise ValueError('canonical_new_proof_invalid')
    # A verified canonical write can survive a crash before status=verified.
    # Re-verify the SAME source/sequence, then accept only the exact complete
    # atomic result already committed. No partial/new/same-day conflict or
    # altered nonselected pointer is reconciled by this path.
    if registry and any(reconciliation[code]['source_day'] != previous[code]
                        for code in selected):
        prior_days = source_days({'schema':1, 'groups':{
            code:{'source_day':reconciliation[code]['source_day'],
                  'receipt':reconciliation[code]['receipt']} for code in GROUPS}})
        for code in GROUPS:
            current = registry['groups'][code]['receipt']
            if code in selected:
                fresh = proof['groups'][code]
                if (fresh.get('new_sequence_verified') is not True
                        or previous[code] != day or prior_days[code] >= day
                        or receipt_identity(current) != receipt_identity(fresh)):
                    raise ValueError('canonical_commit_recovery_conflict')
            elif (prior_days[code] != previous[code]
                  or receipt_identity(reconciliation[code]['receipt']) != receipt_identity(current)):
                raise ValueError('canonical_commit_recovery_conflict')
        return registry  # Value-identical; no source/occurrence is advanced twice.
    groups = {}
    for code in GROUPS:
        prior = reconciliation[code]
        if prior['source_day'] != previous[code]:
            raise ValueError('canonical_source_changed')
        old = prior['receipt']
        identity = receipt_identity(old)
        if registry and identity != receipt_identity(registry['groups'][code]['receipt']):
            raise ValueError('canonical_identity_changed')
        receipt, source_day = old, previous[code]
        if code in selected:
            receipt = proof['groups'][code]
            if receipt.get('new_sequence_verified') is not True:
                raise ValueError('canonical_new_sequence_unverified')
            new_identity = receipt_identity(receipt)
            if day < source_day or (day == source_day and new_identity != identity):
                raise ValueError('canonical_source_day_conflict')
            source_day = day
        groups[code] = {'source_day':source_day,
                       'receipt':{key:receipt[key] for key in ('id','sha256','crc','imported')}}
    return {'schema':1, 'groups':groups}


def verify_new_sequence(records, comparison):
    """Confirm the actual new export contains the exact sequence proved in UI."""
    from .collector_evidence import canonical_tokens, compatible_window, CanonicalGap
    anchor, following = comparison.get('anchor_records'), comparison.get('new_occurrences')
    evidence = comparison.get('evidence_sha256')
    if (comparison.get('state') != 'new_participant_sequence' or comparison.get('may_export') is not True
            or type(anchor) is not int or type(following) is not int or anchor < 2 or following < 1
            or anchor+following > 200 or not isinstance(evidence, str)
            or not re.fullmatch(r'[0-9a-f]{64}', evidence)):
        raise ValueError('new_sequence_evidence_invalid')
    rows = canonical_tokens(records)
    size = anchor + following
    matches = 0
    target = None
    for start in range(len(rows)-size+1):
        window = rows[start:start+size]
        if any(isinstance(token, CanonicalGap) for token in window):
            continue
        digest = hashlib.sha256(json.dumps(window,ensure_ascii=False,
                    separators=(',',':')).encode()).hexdigest()
        if digest == evidence:
            matches += 1
            target = window
    if matches != 1:
        raise ValueError('new_sequence_missing_or_ambiguous_in_export')
    if sum(compatible_window(rows[i:i+size], target)
           for i in range(len(rows)-size+1)) != 1:
        raise ValueError('new_sequence_missing_or_ambiguous_in_export')
    return True

def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

def due(config, state, now):
    if config.get('run_once') and config['run_once'] != state.get('request'):
        return str(config['run_once'])
    if config.get('enabled') is not True or config.get('validated') is not True:
        return None
    # A bounded same-day catchup window, including temporary Android contention.
    # No Android activity on Sunday or unrestricted reactivation in idle periods.
    if now.weekday() == 6 or not 6 <= now.hour < 8:
        return None
    key = now.date().isoformat()
    return key if state.get('request') != key else None

class ScheduledWhatsApp:
    def __init__(self, lifecycle):
        self.runtime = lifecycle
        self.directory = lifecycle.marker.parent / 'whatsapp-collector'

    def read(self, name):
        path = self.directory / name
        if not path.exists():
            return {}
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError('collector_state_not_private')
        return json.loads(path.read_text())

    def read_canonical(self):
        registry = self.read('canonical.json')
        if registry == {} and (self.directory / 'canonical.json').exists():
            raise ValueError('canonical_registry_empty')
        return registry

    def resume_ticket(self, config, state):
        """Single-use protected reconciliation, bound to exact durable inputs.

        Never infer retry authorization from run_once, dates or failure states.
        Upload intents, cancellations and running jobs cannot be resumed here.
        """
        ticket = self.read('resume.json')
        if not ticket or ticket.get('state') != 'authorized':
            return None
        now = time.time()
        if (ticket.get('schema') != 1
                or ticket.get('release') != '20261031-whatsapp-protected-resume'
                or not isinstance(ticket.get('expires'), (int, float))
                or not now < ticket['expires'] <= now + 86400
                or not isinstance(ticket.get('id'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', ticket['id'])
                or config.get('enabled') is not False
                or config.get('validated') is not False
                or state.get('state') != 'pending_reconciliation'
                or state.get('uploads') != {}
                or not state.get('finished')
                or not state.get('request')
                or ticket.get('request') != state['request']
                or config.get('run_once') != state['request']):
            return None
        expected = ticket.get('inputs')
        if not isinstance(expected, dict) or set(expected) != {'config.json','status.json','canonical.json'}:
            return None
        for name in expected:
            # read() also enforces private regular non-symlink state.
            self.read(name)
            path = self.directory / name
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if expected[name] != actual:
                return None
        source_days(self.read_canonical())
        return ticket

    async def preflight(self, ui, state):
        # Historical source dates are identifiers, never freshness/expiry rules.
        # Re-read the protected verifier; a prior turn's summary is not proof.
        self._eligible_preflight = None
        baselines = {}  # Private actual recoveries, never caller JSON proof flags.
        registry = self.read_canonical()
        known = source_days(registry)
        groups = state['reconciliation'] = {}
        for code in GROUPS:
            try:
                async with asyncio.timeout(GROUP_BUDGET) as group_window:
                    item = groups[code] = {'decision': 'pending'}
                    try:
                        proof = json.loads(await post_compare._source_read(self, [
                            'ssh', 'sasocq-server', 'sudo', '-u', 'whatsapp-import',
                            '/usr/bin/python3', '/opt/whatsapp-import/collector_verify.py',
                            known[code], code]))
                        receipt = proof.get('groups', {}).get(code, {})
                        if (proof.get('state') != 'verified' or not receipt.get('id')
                                or not isinstance(receipt.get('sha256'), str)
                                or len(receipt['sha256']) != 64
                                or receipt.get('crc') != 'ok'
                                or receipt.get('imported') is not True):
                            raise ValueError('receipt_not_verified')
                        receipt_identity(receipt)
                        if set(proof.get('groups', {})) != {code}:
                            raise ValueError('receipt_group_mismatch')
                        if registry and receipt_identity(receipt) != receipt_identity(registry['groups'][code]['receipt']):
                            raise ValueError('canonical_identity_changed')
                        item['receipt'] = receipt
                        item['source_day'] = known[code]
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        item['reason'] = 'receipt_unavailable_or_invalid'
                    if 'receipt' in item:
                        try:
                            recovered = json.loads(await post_compare._source_read(self, [
                                'ssh', 'sasocq-server', 'sudo', '-u', 'whatsapp-import',
                                '/usr/bin/python3', '/opt/whatsapp-import/collector_baseline.py',
                                known[code], code]))
                            if (recovered.get('state') != 'verified'
                                    or recovered.get('sha256') != receipt['sha256']):
                                raise ValueError('baseline_not_verified')
                            packed = base64.b64decode(recovered['records_gzip'], validate=True)
                            with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
                                data = stream.read(4*1024*1024+1)
                            if len(data) > 4*1024*1024:
                                raise ValueError('baseline_limit')
                            baseline = json.loads(data)
                            eligible_policy.baseline_tokens(baseline)
                            baselines[code] = baseline
                            item.update(baseline_verified=True, baseline_sha256=receipt['sha256'],
                                        baseline_records=len(baseline))
                            item['global_guard_failure'] = True
                            reference_day = await read_device_day(ui)
                            item.pop('global_guard_failure', None)
                            cursor = PhysicalTailCursor(ui, reference_day, parser=page)
                            cursor.group_deadline = group_window.when()
                            try:
                                comparison = await cursor.compare(code, baseline)
                            finally:
                                item['continuity_diagnostics'] = list(getattr(cursor, 'safe_trace', []))[-32:]
                            item['global_guard_failure'] = True
                            if await read_device_day(ui) != reference_day:
                                raise ValueError('device_day_changed_during_capture')
                            item.pop('global_guard_failure', None)
                            item['comparison'] = comparison
                            item['decision'] = ('export' if item['comparison'].get('may_export') is True
                                                else 'unchanged_visible_suffix' if item['comparison'].get('state') == 'visible_tail_matches'
                                                else 'pending')
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            if str(exc) == 'device_day_changed_during_capture':
                                item['global_guard_failure'] = True
                            item['reason'] = 'canonical_or_ui_context_unresolved'
                            # Fixed stage identifiers only, never selector text or UI.
                            item['navigation_stages'] = list(getattr(ui, 'navigation_trace', []))[-16:]
                            item['error_class'] = type(exc).__name__ if type(exc) in (ExportError, ValueError, KeyError, TypeError, TimeoutError, RuntimeError) else 'unexpected_exception'
                            if isinstance(exc, ExportError):
                                import re
                                value = str(exc)
                                if re.fullmatch(r'[a-z][a-z0-9_]{0,95}', value):
                                    item['reason'] = value
                                if value == 'whatsapp_link_required':
                                    # Account-wide loss of the official companion
                                    # session is not four independent chat failures.
                                    # Preserve receipts already read; no export,
                                    # no registration, no retry on remaining groups.
                                    item['global_guard_failure'] = True
                                    state.update(source_gate='blocked',
                                                 reason='whatsapp_link_required',
                                                 required_action='relink_official_companion')
                                    return False
            except TimeoutError:
                groups[code] = dict(groups.get(code, {}), decision='pending',
                                    reason='group_total_deadline_exceeded')
            if not post_compare._persist(self, state):
                atomic(self.directory / 'status.json', state)
        try:
            self._eligible_preflight = eligible_policy.issue_preflight(state, registry, baselines)
            selected, pending, unchanged = eligible_policy.selection(self._eligible_preflight, state, registry)
            state.update(pending_groups=pending, unchanged_groups=unchanged, source_gate='all_four_verified')
            if pending and not selected:
                state['reason'] = 'ui_pending_without_eligible_group'
            return bool(selected) or not pending
        except (ValueError, KeyError, TypeError):
            self._eligible_preflight = None
            state.update(source_gate='blocked', reason='all_source_baselines_or_decisions_unproved')
            return False

    async def run(self, request, state, resume=None):
        if _post_delivery_marked(state):
            raise ValueError('post_delivery_compare_requires_supported_runtime')
        eligible_policy.allow_new_cycle(state, request, self.read_canonical())
        previous_delivery = ({'request':state.get('request'), 'selected':state.get('selected'),
                              'pending_groups':state.get('pending_groups'), 'proof':state.get('proof')}
                             if 'eligible_delivery' in state else None)
        runtime = self.runtime
        budget = PhaseDeadline(UI_BUDGET)
        async with runtime.lock:
            if runtime.owner or runtime.cleanup_failed or not runtime.background_admission():
                return  # Not admitted: retry later, never steal another lease.
            if resume is not None:
                if runtime.authority.inspect() is not None:
                    return  # Durable contention must not consume protected authorization.
                current = self.resume_ticket(self.read('config.json'), self.read('status.json'))
                if current != resume or request != current.get('request'):
                    return
                # Persist consumption before any lease/UI side effect. A crash
                # requires another explicit reconciliation, never automatic replay.
                consumed = dict(current, state='consumed', consumed_at=time.time(),
                                previous_status=self.read('status.json'))
                atomic(self.directory / 'resume.json', consumed)
            runtime.background_owner = KEY
            try:
                async with asyncio.timeout(budget.remaining()):
                    binding = await _scheduled_acquire(runtime, budget.ends)
            except BaseException as exc:
                # Consumption/admission already happened. Preserve the complete
                # previous record and hold this request; no automatic boot retry.
                cleanup_pending = True
                try:
                    cleanup_pending = bool(runtime.owner or runtime.cleanup_failed
                                           or getattr(runtime, 'durable_generation', None)
                                           or runtime.authority.inspect() is not None)
                except Exception:
                    pass  # Unknown authority is never recorded as OFF/free.
                failed = {'request':request,
                          'state':'interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed',
                          'phase':'ui_acquire_failed', 'reason':'scheduled_acquire_not_completed',
                          'error':type(exc).__name__, 'finished':time.time(),
                          'uploads':{}, 'previous_status':state,
                          'cleanup_pending':cleanup_pending}
                atomic(self.directory / 'status.json', failed)
                raise
        state = {'request':request, 'state':'running', 'started':time.time(), 'uploads':{}}
        if previous_delivery is not None:
            state['previous_delivery'] = previous_delivery
        day = dt.datetime.now(ZONE).strftime('%Y%m%d')
        async def call(args):
            if 'eligible_delivery' in state and not self.cloud_authorized(state):
                raise ValueError('eligible_authorization_revoked')
            return await _scheduled_call(runtime, binding, args)
        async def intent(code, name):
            eligible_policy.validate(state)
            if not self.cloud_authorized(state):
                raise ValueError('eligible_authorization_revoked')
            if code not in state.get('selected', []) or code in state['uploads'] or name != f'SASOCQ-{code}-{day}-OFICIAL.zip':
                raise ValueError('eligible_intent_not_authorized')
            async with runtime.lock:
                _require_scheduled_binding(runtime, binding, KEY, {'operation':'ui'})
            state['uploads'][code] = {'name':name, 'state':'upload_uncertain'}
            atomic(self.directory / 'status.json', state)
        try:
            atomic(self.directory / 'status.json', state)
            async with asyncio.timeout(budget.remaining()):
                ui = OfficialUI(call)
                if not await self.preflight(ui, state):
                    state['state'] = 'pending_reconciliation'
                    return
                selected, pending, unchanged = eligible_policy.selection(
                    getattr(self, '_eligible_preflight', None), state, self.read_canonical())
                if not selected:
                    state['state'] = 'pending_reconciliation' if pending else 'visible_suffixes_match'
                    return
                # The official output name is date-based. Do not create a
                # second same-day source under that name after a verified run.
                if any(state.get('reconciliation', {}).get(code, {}).get('source_day', '') >= day
                       for code in selected):
                    state['state'] = 'pending_reconciliation'
                    state['reason'] = 'same_day_source_requires_reconciliation'
                    return
                if not self.cloud_authorized(state):
                    raise ValueError('eligible_authorization_revoked')
                eligible_policy.persist_policy(self._eligible_preflight, state, self.read_canonical(), day)
                atomic(self.directory / 'status.json', state)  # BEFORE the first intent/effect.
                for code in selected:
                    eligible_policy.validate(state)
                    if not self.cloud_authorized(state):
                        raise ValueError('eligible_authorization_revoked')
                    if eligible_policy.digest(self.read_canonical()) != state['eligible_delivery']['original_registry_digest']:
                        raise ValueError('eligible_original_source_changed')
                    async with runtime.lock:
                        _require_scheduled_binding(runtime, binding, KEY, {'operation':'ui'})
                    await ui.export(code, day, intent)
                    state['uploads'][code]['state'] = 'submitted'
                    atomic(self.directory / 'status.json', state)
                state['state'] = 'verification_pending'
                state['phase'] = 'cloud_handoff'
                state['source_day'] = day
                state['selected'] = selected
                atomic(self.directory / 'status.json', state)
        except BaseException as exc:
            state['state'] = 'interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed'
            # No UI contents, messages, credentials or arbitrary exception text.
            state['error'] = type(exc).__name__
            if type(exc) in (ValueError, RuntimeError, ExportError) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', str(exc)):
                state['reason'] = str(exc)
            raise
        finally:
            state['finished'] = time.time()
            try:
                atomic(self.directory / 'status.json', state)
            finally:
                await _scheduled_release(self, binding)

        if state.get('state') == 'verification_pending':
            await self.verify_delivery(state)

    def cloud_authorized(self, state):
        config = self.read('config.json')
        return ((bool(state.get('request')) and config.get('run_once') == state['request']) or
                (config.get('enabled') is True and config.get('validated') is True))

    def cloud_block(self, state, reason):
        state.update(state='delivery_reconciliation_required', phase='cloud_blocked', reason=reason)
        atomic(self.directory / 'status.json', state)

    async def verify_delivery(self, state):
        """Verify submitted delivery only, under ONE durable900s cloud window."""
        if _post_delivery_marked(state):
            self.cloud_block(state, 'post_delivery_compare_requires_supported_runtime')
            return
        runtime = self.runtime
        if getattr(self, '_cloud_verify_active', False):
            return
        # A->B: unknown/V1 durable state cannot be reset or granted a new900.
        if 'cloud_budget' in state and (not isinstance(state['cloud_budget'], dict)
                or state['cloud_budget'].get('schema') != 2):
            self.cloud_block(state, 'cloud_deadline_requires_supported_runtime')
            return
        if isinstance(state.get('cloud_budget'), dict) and state['cloud_budget'].get('inflight') is True:
            self.cloud_block(state, 'cloud_inflight_reconciliation_required')
            return
        try:
            eligible_policy.validate(state)
        except (ValueError, KeyError, TypeError):
            self.cloud_block(state, 'eligible_policy_invalid_requires_reconciliation')
            return
        day, selected = submitted_delivery(state, GROUPS)
        # Existing/legacy budgets expire even if Android later belongs to another owner.
        if state.get('cloud_budget') is not None or state.get('verification_attempts', 0):
            try:
                CloudWindow(state, lambda: atomic(self.directory / 'status.json', state)).check()
            except ValueError as exc:
                self.cloud_block(state, str(exc))
                return
        # Internal authority's stopped/free/clean state. Never probe/start Android.
        if runtime.owner is not None or runtime.cleanup_failed or getattr(runtime, 'background_owner', None) is not None:
            return
        if getattr(self, '_cloud_verify_active', False):
            return
        self._cloud_verify_active = True
        persist = lambda: atomic(self.directory / 'status.json', state)
        window = CloudWindow(state, persist)
        metrics = {}
        def guard():
            eligible_policy.validate(state)
            if not self.cloud_authorized(state):
                raise ValueError('cloud_authorization_revoked')
            if runtime.owner is not None or runtime.cleanup_failed or getattr(runtime, 'background_owner', None) is not None:
                raise ValueError('cloud_authority_no_longer_free')
            return window.guard_work()
        async def host(argv):
            guard()
            budget = state['cloud_budget']
            token = uuid.uuid4().hex
            bound_scope, bound_attempt = budget['scope'], state['verification_attempts']
            budget.update(inflight=True, host_started_mono=window.mono(), inflight_token=token)
            persist()  # BEFORE submission; restart cannot infer completion by time.
            confirmed = False
            def completed():
                nonlocal confirmed
                if confirmed:
                    raise ValueError('cloud_completion_duplicate')
                if (not self._cloud_verify_active or state.get('cloud_budget') is not budget
                        or budget.get('scope') != bound_scope or cloud_scope(state) != bound_scope
                        or state.get('verification_attempts') != bound_attempt
                        or not budget.get('inflight') or budget.get('inflight_token') != token):
                    raise ValueError('cloud_completion_scope_or_token_invalid')
                saved = dict(budget)
                budget.update(inflight=False, host_started_mono=None, inflight_token=None,
                              last_completion={'scope':bound_scope, 'attempt':bound_attempt,
                                               'token':token, 'kind':'private_broker_final_response'})
                try:
                    persist()
                except BaseException:
                    budget.clear();budget.update(saved)
                    raise ValueError('cloud_completion_persistence_failed') from None
                confirmed = True
            value = await runtime._host(argv, _on_host_completion=completed)
            if not confirmed or budget['inflight']:
                raise ValueError('cloud_inflight_reconciliation_required')
            # Receipt records completion even if revoked/expired; never authorizes
            # another effect or canonical commit after revocation/deadline.
            guard()
            return value
        async def work():
            # No nested cancelling timer; the enclosing work/drain controller owns it.
            await fresh_transport(host, seconds=None)
            proof = {'state':'verified', 'groups':{}}
            for code in selected:
                state['phase'] = 'verify_new_export'
                state['verifying_group'] = code
                part = json.loads(await host([
                    'ssh','sasocq-server','sudo','-u','whatsapp-import','/usr/bin/python3',
                    '/opt/whatsapp-import/collector_verify.py',day,code]))
                if part.get('state') != 'verified' or code not in part.get('groups',{}):
                    raise RuntimeError('collector_end_to_end_not_verified')
                receipt = part['groups'][code]
                receipt_identity(receipt)
                recovered = json.loads(await host([
                    'ssh','sasocq-server','sudo','-u','whatsapp-import','/usr/bin/python3',
                    '/opt/whatsapp-import/collector_baseline.py',day,code]))
                if recovered.get('state') != 'verified' or recovered.get('sha256') != receipt['sha256']:
                    raise ValueError('new_export_recovery_changed')
                packed = base64.b64decode(recovered['records_gzip'], validate=True)
                with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
                    decoded = stream.read(4*1024*1024+1)
                if len(decoded) > 4*1024*1024:
                    raise ValueError('new_export_recovery_limit')
                verify_new_sequence(json.loads(decoded), state['reconciliation'][code]['comparison'])
                receipt['new_sequence_verified'] = True
                proof['groups'].update(part['groups'])
            guard()
            state['phase'] = 'commit_canonical_sources'
            registry = advance_sources(self.read_canonical(),
                                       state['reconciliation'], day, proof, selected)
            guard()  # Deadline/authorization immediately before atomic canonical commit.
            atomic(self.directory / 'canonical.json', registry)
            state['proof'] = proof
            eligible_policy.finish(state)

        try:
            if not self.cloud_authorized(state):
                raise ValueError('cloud_authorization_revoked')
            window.check(create=True)  # Persist ONCE, after OFF, BEFORE cloud effects.
            if window.abandon_interrupted() is False:
                return  # Broker may still run from previous process; no side effect.
            seconds = window.begin_attempt()
            if seconds is None:
                return  # Next start-based300s slot, inside ORIGINAL900s window.
            await work_with_drain(work(), seconds, metrics)
        except asyncio.CancelledError:
            # Drain has completed; original window/attempt remain durable.
            if state.get('cloud_budget', {}).get('inflight'):
                self.cloud_block(state, 'cloud_inflight_reconciliation_required')
            elif metrics.get('drain_guard_exceeded'):
                self.cloud_block(state, 'cloud_broker_drain_bound_exceeded')
            else:
                try:
                    window.check()
                except ValueError as exc:
                    self.cloud_block(state, str(exc))
            if state.get('cloud_budget', {}).get('attempt_active') and not state['cloud_budget'].get('inflight'):
                window.finish()
            state['last_verification_attempt'] = time.time()
            persist()
            raise
        except Exception as exc:
            reason = str(exc) if type(exc) in (ValueError, RuntimeError) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', str(exc)) else 'cloud_verification_failed'
            terminal = (reason.startswith(('cloud_', 'eligible_')) and reason not in {'cloud_verification_failed'})
            if state.get('verification_attempts', 0) >= MAX_ATTEMPTS:
                terminal, reason = True, 'cloud_attempts_exhausted'
            if metrics.get('drain_guard_exceeded'):
                terminal, reason = True, 'cloud_broker_drain_bound_exceeded'
            if state.get('cloud_budget', {}).get('inflight'):
                terminal, reason = True, 'cloud_inflight_reconciliation_required'
            state.update(state='delivery_reconciliation_required' if terminal else 'verification_pending', phase='cloud_blocked' if terminal else 'cloud_verification_pending', reason=reason, error=type(exc).__name__)
        finally:
            self._cloud_verify_active = False
            budget = state.get('cloud_budget')
            # Never clear recovery metadata for a still-unknown crash inflight.
            if budget is not None and budget.get('attempt_active') and not budget.get('inflight'):
                window.finish()
            if metrics:
                state['cloud_drain_metrics'] = metrics
            if budget is not None:
                state['last_verification_attempt'] = time.time()
                persist()

    async def release(self, binding=None):
        runtime = self.runtime
        async with runtime.lock:
            if binding is not None:
                if (not isinstance(binding, _ScheduledBinding)
                        or binding.seal is not _SCHEDULED_SEAL or binding.runtime is not runtime):
                    raise ValueError('scheduled_android_binding_invalid')
                # Final stop may clean an expired/failed OWN generation, but
                # never stop or clear background flags of a replacement owner.
                if (binding.runtime is runtime and isinstance(binding.generation, str)
                        and re.fullmatch(r'[0-9a-f]{32}', binding.generation)
                        and runtime.owner == KEY
                        and runtime.background_owner == KEY
                        and getattr(runtime, 'durable_generation', None) == binding.generation):
                    try:
                        await runtime._stop()
                    finally:
                        runtime.background_owner = None
                elif (binding.runtime is runtime and runtime.owner is None
                        and runtime.background_owner == KEY
                        and getattr(runtime, 'durable_generation', None) is None
                        and runtime.authority.inspect() is None):
                    runtime.background_owner = None  # native error already stopped it
                return
            try:
                if runtime.owner == KEY:
                    await runtime._stop()
            finally:
                runtime.background_owner = None

    async def worker(self):
        while True:
            try:
                post_compare.publish_resident(self)
                config = self.read('config.json')
                state = self.read('status.json')
                if _post_delivery_marked(state):
                    # Consumed/incomplete/finished C is held; never mode downgrade.
                    await asyncio.sleep(15)
                    continue
                if eligible_policy.has_protocol(state):
                    try:
                        if eligible_policy.restore_runtime_fence(state, self.read_canonical()):
                            atomic(self.directory / 'status.json', state)
                        eligible_policy.validate(state)
                    except (ValueError, KeyError, TypeError):
                        self.cloud_block(state, 'eligible_policy_invalid_requires_reconciliation')
                        await asyncio.sleep(15)
                        continue
                if state.get('state') == 'verification_pending':
                    # The durable window owns slots/expiry/revocation, not a gap
                    # measured after completion (which extended recovery to3300s).
                    await self.verify_delivery(state)
                    await asyncio.sleep(5)
                    continue
                private_ticket = self.read('resume.json')
                if private_ticket.get('state') == 'authorized' and (private_ticket.get('schema') != 1 or type(private_ticket.get('schema')) is not int):
                    try:
                        grant = post_compare.admission(self, config, state)
                        await post_compare.compare(self, grant)
                    except (ValueError, KeyError, TypeError):
                        pass  # Keep original delivery/ticket; no downgrade to run().
                    await asyncio.sleep(15)
                    continue
                resume = self.resume_ticket(config, state)
                if resume:
                    await self.run(resume['request'], state, resume=resume)
                    await asyncio.sleep(15)
                    continue
                # A crash after any upload intent requires explicit reconciliation;
                # no automatic upload replay, even for a new calendar day.
                if state.get('state') in {'running','failed','interrupted','delivery_reconciliation_required'}:
                    await asyncio.sleep(15)
                    continue
                request = due(config, state, dt.datetime.now(ZONE))
                if request:
                    await self.run(request, state)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Remain fail-closed, leaving durable status for the operator.
                pass
            await asyncio.sleep(15)
