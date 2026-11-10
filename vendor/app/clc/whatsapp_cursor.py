"""Bounded official accessibility cursor. Raw content stays in process memory.

Only explicit date/author context is accepted. No OCR, private DB, timestamps
from chat headers, feedback flags or assumptions about hidden media.
"""
import asyncio
import datetime as dt
import re
import xml.etree.ElementTree as ET
from .collector_evidence import Observation, reconcile
from .whatsapp_notice import parse_notice
from .whatsapp_collector import WA, GROUPS, ExportError, nodes, select, center


def bounds(node):
    value = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
    if not value:
        raise ExportError('invalid_bounds')
    return tuple(map(int, value.groups()))


def page(xml, code, reference_day=None):
    rows = nodes(xml)
    headers = [r for r in rows if r.get('resource-id') == WA+':id/conversation_contact_name']
    if len(headers) != 1 or headers[0].get('text') != GROUPS[code][1]:
        raise ExportError('group_identity_changed')
    tree = ET.fromstring(xml)
    lists = [n for n in tree.iter('node') if n.get('resource-id') == 'android:id/list'
             and n.get('package') == WA]
    if len(lists) != 1:
        raise ExportError('conversation_list_missing')
    viewport = bounds(lists[0])
    result = []
    for row in lists[0]:
        attrs = [n.attrib for n in row.iter('node')]
        rid = row.get('resource-id', '').split('/')[-1]
        notice = parse_notice(row, viewport)
        # The actual app nests date dividers inside conversation_row_text.
        dividers = [a for a in attrs if a.get('resource-id') ==
                    WA+':id/conversation_row_date_divider']
        if dividers:
            if len(dividers) != 1:
                raise ExportError('day_context_unsupported')
            divider = dividers[0]
            box = bounds(divider)
            if box[1] <= viewport[1] or box[3] >= viewport[3]:
                result.append(('clipped',))
                continue
            value = divider.get('text', '')
            if value in {'Today', 'Yesterday'}:
                # Supplied only by a trusted adapter after checking Android's
                # own calendar date/timezone. Never infer it from chat bodies.
                if not isinstance(reference_day, dt.date) or isinstance(reference_day, dt.datetime):
                    raise ExportError('relative_day_requires_device_date')
                day = reference_day - dt.timedelta(days=value == 'Yesterday')
            elif value in ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'):
                if not isinstance(reference_day, dt.date) or isinstance(reference_day, dt.datetime):
                    raise ExportError('relative_day_requires_device_date')
                weekday = ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday').index(value)
                delta = (reference_day.weekday() - weekday) % 7
                if not 1 <= delta <= 6:
                    raise ExportError('weekday_context_ambiguous')
                day = reference_day - dt.timedelta(days=delta)
            else:
                try:
                    day = dt.datetime.strptime(value, '%B %d, %Y').date()
                except ValueError:
                    raise ExportError('day_context_unsupported') from None
            result.append(('day', day.isoformat()))
            # A divider may be nested INSIDE an actual message/media row.
            # Keep that row as a separate event, including unknown-media gaps.
            if rid not in {'conversation_row_text', 'conversation_row_image',
                           'conversation_row_video', 'conversation_row_audio',
                           'conversation_row_document', 'conversation_row_sticker'}:
                if notice is not None:
                    result.append(('notice', notice))
                    continue
                # Unknown containers may hold BOTH a divider and an actual
                # payload. Keep that payload as a barrier, never as participant
                # text and never silently reduce the whole row to a day.
                divider_node = next(n for n in row.iter('node')
                    if n.get('resource-id') == WA+':id/conversation_row_date_divider')
                outside = [n for n in row.iter('node') if n is not divider_node]
                has_payload = any(
                    (n is not row and n.get('resource-id')) or
                    n.get('text') or n.get('content-desc') or
                    n.get('clickable') == 'true' or n.get('long-clickable') == 'true'
                    for n in outside)
                if has_payload:
                    row_box = bounds(row)
                    result.append(('clipped',) if row_box[1] <= viewport[1] or
                                  row_box[3] >= viewport[3] else ('unsupported',))
                continue
        if notice is not None:
            result.append(('notice', notice))
            continue
        # Fully visible rows only. A clipped last row cannot certify the tail.
        box = bounds(row)
        if box[1] <= viewport[1] or box[3] >= viewport[3]:
            # Container padding touching the edge isn't clipped content. All
            # meaningful text/date/name descendants must be strictly inside.
            meaningful = [a for a in attrs if a.get('resource-id') in {
                WA+':id/message_text', WA+':id/date', WA+':id/name_in_group_tv'}]
            content_inside = (rid == 'conversation_row_text' and meaningful and
                any(a.get('resource-id') == WA+':id/message_text' for a in meaningful) and
                all(viewport[1] < bounds(a)[1] and bounds(a)[3] < viewport[3]
                    for a in meaningful))
            if not content_inside and rid.startswith('conversation_row_'):
                result.append(('clipped',))
                continue
        if rid in {'conversation_row_date', 'date_divider'}:
            values = [a.get('text') for a in attrs if a.get('text')]
            dates = []
            for value in values:
                try:
                    dates.append(dt.datetime.strptime(value, '%B %d, %Y').date().isoformat())
                except ValueError:
                    pass
            if len(set(dates)) != 1:
                raise ExportError('day_context_unsupported')
            result.append(('day', dates[0]))
        elif rid == 'conversation_row_text':
            def value(name):
                matches = [a.get('text','') for a in attrs if a.get('resource-id') == WA+':id/'+name]
                if len(matches) > 1:
                    raise ExportError('ambiguous_row')
                return matches[0] if matches else None
            body, clock = value('message_text'), value('date')
            if not body or not clock or any(a.get('resource-id') == WA+':id/read_more' for a in attrs):
                raise ExportError('incomplete_text_row')
            if ' '.join(body.split()) == 'Unsupported message':
                result.append(('unsupported',))
                continue
            result.append(('text', value('name_in_group_tv'), clock, body))
        elif rid == 'unread_divider_tv':
            continue
        else:
            # Includes media/system/unknown rows: never silently skip a gap.
            result.append(('unsupported',))
    return result, select(rows, rid=WA+':id/scroll_bottom'), viewport


def prepend(older, newer):
    # Unique overlap only; do not deduplicate legitimate repeated messages.
    overlaps = [n for n in range(2, min(len(older), len(newer))+1)
                if older[-n:] == newer[:n] and not any(r[0] == 'clipped' for r in older[-n:])]
    if len(overlaps) != 1:
        raise ExportError('page_overlap_missing_or_ambiguous')
    return older[:-overlaps[0]] + newer


def records(events):
    day = sender = None
    output = []
    # Earlier visible rows may precede the first explicit day marker. They are
    # outside the proven suffix; never infer their day from the export.
    start = next((i for i, e in enumerate(events) if e[0] == "day"), None)
    if start is None:
        raise ExportError("day_context_missing")
    for event in events[start:]:
        if event[0] == 'day':
            day, sender = event[1], None
        elif event[0] == 'text':
            _, author, clock, body = event
            sender = author
            if not day or not sender:
                raise ExportError('day_or_author_context_missing')
            clock = ' '.join(clock.split())
            try:
                fmt = '%I:%M %p' if re.fullmatch(r'\d{1,2}:\d{2} [AP]M', clock, re.I) else '%H:%M'
                parsed = dt.datetime.strptime(clock.upper(), fmt).time()
            except ValueError:
                raise ExportError('clock_format_unsupported') from None
            output.append({'stamp': day+'T'+parsed.isoformat(), 'sender':sender, 'body':body})
        else:
            raise ExportError('clipped_media_or_unsupported_context')
    return tuple(output)


class TailCursor:
    def __init__(self, ui, reference_day=None):
        self.ui = ui
        self.reference_day = reference_day

    async def snapshot(self, code):
        result = await self.ui.call({'operation':'ui'})
        return page(result['output'], code, self.reference_day)

    async def bottom(self, code):
        for _ in range(16):
            events, button, viewport = await self.snapshot(code)
            if button:
                x,y = center(button)
                await self.ui.call({'operation':'tap','x':x,'y':y})
            else:
                x = (viewport[0]+viewport[2])//2
                await self.ui.call({'operation':'swipe','x':x,'y':viewport[3]-60,
                                    'x2':x,'y2':viewport[1]+60,'duration_ms':400})
                after, button, _ = await self.snapshot(code)
                if button is None and events == after:
                    return after
            await asyncio.sleep(.3)
        raise ExportError('live_tail_not_reached')

    async def compare(self, code, baseline):
        await self.ui.inspect_group(code)
        tail = await self.bottom(code)
        events = tail
        decision = None
        for _ in range(12):
            try:
                observation = Observation(code, records(events), True, True, True)
                decision = reconcile(code, baseline, observation)
                if decision['state'] != 'pending_reconciliation':
                    break
            except ExportError:
                pass
            current, _, viewport = await self.snapshot(code)
            x = (viewport[0]+viewport[2])//2
            await self.ui.call({'operation':'swipe','x':x,'y':viewport[1]+100,
                                'x2':x,'y2':viewport[3]-100,'duration_ms':500})
            older, _, _ = await self.snapshot(code)
            if older == current:
                break
            # Clip markers are boundaries, not records. Require overlap among
            # fully visible rows; keep the terminal clip to refuse a cut tail.
            older = [r for r in older if r[0] != 'clipped']
            prior = events[1:] if events and events[0][0] == 'clipped' else events
            try:
                events = prepend(older, prior)
            except ExportError:
                decision = None
                break
            if len(events) > 200:
                decision = None
                break
        # Return to the live tail and verify it has not changed during capture.
        if await self.bottom(code) != tail:
            raise ExportError('live_tail_changed_during_capture')
        return decision or {'state':'pending_reconciliation', 'may_export':False,
                            'reason':'bounded_cursor_context_not_resolved'}
