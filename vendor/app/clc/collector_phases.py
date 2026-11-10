"""Bounded production transport handoff; never acquires Android or exports.

Service success is only permission to run the source-specific verifier. Always
drain an already active run before requesting a fresh inventory of Drive.
"""
import asyncio
import time


class PhaseDeadline:
    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.ends = clock() + seconds

    def remaining(self):
        return max(0., self.ends - self.clock())


async def fresh_transport(host, *, seconds=600, poll=5, sleep=asyncio.sleep):
    """Wait for older run, start new inventory, and await completion in ONE cap.

The supplied host executor must propagate cancellation and bound its SSH child.
This does not assert that the desired ZIP exists; callers must verify its ID,
hash, CRC, imported content and recovered sequence afterward.
"""
    prefix = ['ssh', 'sasocq-server', 'sudo', 'systemctl']

    async def inactive(after=None):
        while True:
            raw = await host(prefix + ['show', 'whatsapp-drive.service',
                                      '--property=ActiveState,Result,ExecMainStartTimestampMonotonic'])
            fields = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
            active = fields.get('ActiveState')
            if active in {'inactive', 'failed'}:
                tick = fields.get('ExecMainStartTimestampMonotonic', '')
                if not tick.isdecimal():
                    raise RuntimeError('transport_generation_unavailable')
                if after is None or int(tick) > after:
                    return fields
                # systemctl --no-block may return before systemd starts the job.
                await sleep(poll)
                continue
            if active not in {'active', 'activating', 'deactivating', 'reloading'}:
                raise RuntimeError('transport_service_state_unknown')
            await sleep(poll)

    async with asyncio.timeout(seconds):
        # start --no-block during an older run would coalesce with that run.
        # Waiting first guarantees our request is after its inventory snapshot.
        previous = await inactive()
        await host(prefix + ['start', '--no-block', 'whatsapp-drive.service'])
        result = await inactive(after=int(previous['ExecMainStartTimestampMonotonic']))
        if result.get('ActiveState') != 'inactive' or result.get('Result') != 'success':
            raise RuntimeError('import_failed')


def submitted_delivery(state, allowed):
    """Strict durable handoff, rejecting partial/uncertain and malformed input."""
    import datetime as dt
    import re
    if state.get('state') != 'verification_pending':
        raise ValueError('delivery_phase_invalid')
    day = state.get('source_day')
    if not isinstance(day, str) or not re.fullmatch(r'\d{8}', day):
        raise ValueError('delivery_day_invalid')
    dt.datetime.strptime(day, '%Y%m%d')
    selected = state.get('selected')
    if (not isinstance(selected, list) or not selected
            or any(not isinstance(code, str) for code in selected)
            or len(set(selected)) != len(selected) or not set(selected) <= set(allowed)
            or not isinstance(state.get('uploads'), dict)
            or set(state['uploads']) != set(selected)):
        raise ValueError('delivery_groups_invalid')
    for code in selected:
        upload = state['uploads'][code]
        expected = f'SASOCQ-{code}-{day}-OFICIAL.zip'
        if (not isinstance(upload, dict) or upload.get('state') != 'submitted'
                or upload.get('name') != expected
                or state.get('reconciliation', {}).get(code, {}).get('decision') != 'export'):
            raise ValueError('delivery_submission_unproved')
    return day, selected
