"""Explicit sender probe through the official, cancelled Reply preview.

Caller must own the Android lease. Never types, sends, forwards or changes a
message. Existing composer/selection state prevents the probe. A result is
returned only after the preview has been removed and the viewport restored.
"""
import xml.etree.ElementTree as ET
from .whatsapp_collector import WA, GROUPS, ExportError, nodes, center
from .whatsapp_cursor import bounds


def _one(items, reason):
    if len(items) != 1:
        raise ExportError(reason)
    return items[0]


def _by_id(tree, name):
    return [n for n in tree.iter('node') if n.get('resource-id') == WA+':id/'+name]


def _read(xml, code):
    nodes(xml)  # Includes size/entity validation.
    tree = ET.fromstring(xml)
    header = _one(_by_id(tree, 'conversation_contact_name'), 'author_probe_wrong_group')
    if header.get('text') != GROUPS[code][1]:
        raise ExportError('author_probe_wrong_group')
    listing = _one([n for n in tree.iter('node') if n.get('resource-id') == 'android:id/list'
                    and n.get('package') == WA], 'author_probe_list_missing')
    return tree, bounds(listing)


def _empty_composer(tree):
    entry = _one(_by_id(tree, 'entry'), 'author_probe_composer_missing')
    # Some accessibility dumps expose the hint as text. The official mic
    # control, rather than a Send control, independently proves empty input.
    if entry.get('text', '') not in ('', 'Message'):
        raise ExportError('author_probe_existing_draft')
    if not _by_id(tree, 'voice_note_btn') or _by_id(tree, 'send'):
        raise ExportError('author_probe_existing_draft')


def _target(tree, body, clock, viewport, selected=False):
    found = []
    for row in _by_id(tree, 'conversation_row_text'):
        texts, dates = _by_id(row, 'message_text'), _by_id(row, 'date')
        if len(texts) != 1 or len(dates) != 1:
            continue
        if (texts[0].get('text') != body or
                ' '.join(dates[0].get('text', '').split()) != ' '.join(clock.split())):
            continue
        if selected and row.get('selected') != 'true':
            continue
        if _by_id(row, 'read_more'):
            raise ExportError('author_probe_partial_target')
        for node in (texts[0], dates[0]):
            box = bounds(node)
            if not viewport[1] < box[1] < box[3] < viewport[3]:
                raise ExportError('author_probe_partial_target')
        found.append((row, texts[0]))
    return _one(found, 'author_probe_target_ambiguous')


def _quote(tree, body):
    preview = _one(_by_id(tree, 'quoted_message_preview_container'), 'author_probe_preview_missing')
    title = _one(_by_id(preview, 'quoted_title'), 'author_probe_sender_missing')
    quoted = _one(_by_id(preview, 'quoted_text'), 'author_probe_body_missing')
    if quoted.get('text') != body:
        raise ExportError('author_probe_body_mismatch')
    sender = title.get('text', '').strip()
    if not sender or sender.casefold() in {'you', 'você', 'voce'}:
        raise ExportError('author_probe_sender_unsupported')
    return sender


def _physical_frame(tree, code, reference_day=None):
    from .whatsapp_frames import capture
    from .whatsapp_cursor import page
    return capture(ET.tostring(tree, encoding='unicode'), code, reference_day, page)


def _restore_step(original, current):
    """Proposal from >=2 unique complete identical text occurrences, not a proof.

    Unknown/interior/missing rows are not skipped to ACCEPT restoration: the
    ENTIRE original Frame must be exactly recaptured after the bounded action.
    No body-only cache and no resolved author is attached to a shifted frame.
    """
    from collections import Counter
    if original.viewport != current.viewport:
        raise ExportError('author_probe_restore_viewport_changed')
    def complete(row, viewport):
        return (row.visible and row.key is not None and row.key[0] == 'text'
                and row.event[0] == 'text' and viewport[1] < row.box[1] < row.box[3] < viewport[3])
    old = [r for r in original.rows if complete(r, original.viewport)]
    new = [r for r in current.rows if complete(r, current.viewport)]
    # Count identities in ALL rows, including clipped copies: duplicates refuse.
    a = Counter(r.key for r in original.rows)
    b = Counter(r.key for r in current.rows)
    pairs = [(x,y) for x in old for y in new
             if x.key == y.key and a[x.key] == b[y.key] == 1 and x.event == y.event
             and x.prefix == y.prefix]
    if len(pairs) < 2:
        raise ExportError('author_probe_restore_identity_insufficient')
    shifts = []
    for x,y in pairs:
        if x.box[::2] != y.box[::2] or min(x.box[3],y.box[3]) <= max(x.box[1],y.box[1]):
            raise ExportError('author_probe_restore_geometry_ambiguous')
        shifts.extend(x.box[i]-y.box[i] for i in (1,3))
    if (not shifts or min(shifts)*max(shifts) <= 0 or max(shifts)-min(shifts)>8
            or max(abs(v) for v in shifts)>48):
        raise ExportError('author_probe_restore_movement_ambiguous')
    return round(sum(shifts)/len(shifts))


async def _restore_exact(ui, code, original, tree, reference_day=None):
    # Two proposals maximum, each requires a new UI capture. No search, calendar,
    # jump or re-identification by matching body alone. Exact equality decides.
    current = _physical_frame(tree, code, reference_day)
    for _ in range(2):
        if current == original:
            return
        shift = _restore_step(original, current)
        v = current.viewport
        x,y = (v[0]+v[2])//2, (v[1]+v[3])//2
        await ui.call({'operation':'swipe','x':x,'y':y,'x2':x,'y2':y+shift,'duration_ms':500})
        result = await ui.call({'operation':'ui'})
        tree, _ = _read(result['output'], code)
        _empty_composer(tree)
        if _by_id(tree,'quoted_message_preview_container') or _by_id(tree,'action_mode_bar'):
            raise ExportError('author_probe_restore_composer_changed')
        current = _physical_frame(tree, code, reference_day)
    if current != original:
        raise ExportError('author_probe_restore_not_exact')


async def explicit_sender(ui, code, body, clock, *, reference_day=None, _acquisition_deadline=None):
    async def snapshot():
        result = await ui.call({'operation': 'ui'})
        return _read(result['output'], code)

    import asyncio
    import math
    if (_acquisition_deadline is not None and
            (isinstance(_acquisition_deadline, bool)
             or not isinstance(_acquisition_deadline, (int,float))
             or not math.isfinite(_acquisition_deadline)
             or _acquisition_deadline <= asyncio.get_running_loop().time())):
        raise ExportError('author_probe_acquisition_deadline_exceeded')
    async def acquisition_call(arguments):
        if (_acquisition_deadline is not None and
                asyncio.get_running_loop().time() >= _acquisition_deadline):
            raise TimeoutError()
        result = await ui.call(arguments)
        # A concrete IPC return may arrive before the event loop delivers its
        # timeout callback. Never accept a late reply because of that ordering.
        if (_acquisition_deadline is not None and
                asyncio.get_running_loop().time() >= _acquisition_deadline):
            raise TimeoutError()
        return result

    async def acquisition_snapshot():
        result = await acquisition_call({'operation':'ui'})
        return _read(result['output'], code)

    started = False
    try:
        # Deadline bounds acquisition only. Mandatory cleanup below must drain
        # even after expiry/cancellation, without renewing the group deadline.
        async with asyncio.timeout_at(_acquisition_deadline):
            tree, viewport = await acquisition_snapshot()
            _empty_composer(tree)
            if _by_id(tree, 'quoted_message_preview_container') or _by_id(tree, 'action_mode_bar'):
                raise ExportError('author_probe_existing_composer_state')
            _, target = _target(tree, body, clock, viewport)
            original = _physical_frame(tree, code, reference_day)
            x, y = center(target.attrib)
            # Mark before the action: uncertain UI transport must still clean up.
            started = True
            await acquisition_call({'operation': 'long_press', 'x': x, 'y': y, 'duration_ms': 600})
            tree, current = await acquisition_snapshot()
            count = _one(_by_id(tree, 'action_bar_title'), 'author_probe_selection_missing')
            if count.get('text') != '1':
                raise ExportError('author_probe_multiple_selection')
            _target(tree, body, clock, current, selected=True)
            reply = _one([n for n in tree.iter('node') if n.get('content-desc') == 'Reply'
                          and n.get('class') == 'android.widget.Button'
                          and n.get('enabled') == n.get('clickable') == 'true'],
                         'author_probe_reply_unavailable')
            if bounds(reply)[3] > current[1]:
                raise ExportError('author_probe_reply_outside_toolbar')
            x, y = center(reply.attrib)
            await acquisition_call({'operation': 'tap', 'x': x, 'y': y})
            tree, _ = await acquisition_snapshot()
            sender = _quote(tree, body)
            _empty_composer(tree)
            if (_acquisition_deadline is not None and
                    asyncio.get_running_loop().time() >= _acquisition_deadline):
                raise TimeoutError()
    finally:
        if started:
            async def cleanup():
                tree, current = await snapshot()
                previews = _by_id(tree, 'quoted_message_preview_container')
                if previews:
                    preview = _one(previews, 'author_probe_cleanup_ambiguous')
                    cancel = _one(_by_id(preview, 'quoted_preview_cancel'), 'author_probe_cancel_missing')
                    x, y = center(cancel.attrib)
                    await ui.call({'operation': 'tap', 'x': x, 'y': y})
                    tree, current = await snapshot()
                elif _by_id(tree, 'action_mode_bar'):
                    await ui.call({'operation': 'back'})
                    tree, current = await snapshot()
                # Reply opens the keyboard. A reduced list proves a changed
                # viewport; one Back restores it without guessing keyboard nodes.
                if current[3] < viewport[3] - 100:
                    await ui.call({'operation': 'back'})
                    tree, current = await snapshot()
                _empty_composer(tree)
                if (current != viewport or _by_id(tree, 'quoted_message_preview_container')
                        or _by_id(tree, 'action_mode_bar')):
                    raise ExportError('author_probe_cleanup_not_confirmed')
                await _restore_exact(ui, code, original, tree, reference_day)
            import asyncio
            task = asyncio.create_task(cleanup())
            cancelled = False
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.done() and task.cancelled():
                        raise
                    cancelled = True
                    continue
            if cancelled:
                raise asyncio.CancelledError()
    return sender
