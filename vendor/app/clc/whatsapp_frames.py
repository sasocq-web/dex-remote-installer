"""Join bounded accessibility frames without accepting clipped text as a record.

Text exposed on a clipped row is an identity candidate only. A unique identity,
an overlapping physical row and consistent small positive movement are required
to bridge frames. Only the existing page parser can certify a complete event.
No author/date is inferred here, and unknown rows cannot bridge a gap.
"""
from collections import Counter
from dataclasses import dataclass, replace, field
import hashlib
import xml.etree.ElementTree as ET
from .whatsapp_collector import WA, ExportError
from .whatsapp_continuity_diagnostics import frame_summary, history_step, xml_shape, seam_shape


@dataclass(frozen=True)
class Row:
    key: object
    event: tuple
    box: tuple
    visible: bool = True
    prefix: tuple = ()
    # Certified only by capture from a text layout with no message_text.
    edge_text_sliver: bool = False


# Ephemeral Python-only provenance. UI/parser/JSON never sets this marker.
_OPAQUE_JOIN_SEAL = object()

@dataclass(frozen=True)
class _OpaqueJoinProof:
    seal: object
    rows: tuple
    observations: tuple
    older: object
    newer: object
    max_shift: int
    tail_indices: tuple


@dataclass(frozen=True)
class Frame:
    rows: tuple
    viewport: tuple
    button: object = None
    # Unmatched leading observations retained behind an explicit logical fence.
    # Never identities, never emitted as participant records, bounded below.
    opaque_prefixes: tuple = ()
    _opaque_join: object = field(default=None, compare=False, repr=False)

    def events(self):
        return [event for row in self.rows for event in (*row.prefix, row.event)]


def capture(xml, code, reference_day, parser):
    # Validate identity and the entire page before extracting any identities.
    _, button, viewport = parser(xml, code, reference_day)
    tree = ET.fromstring(xml)
    listing = next(n for n in tree.iter('node') if
                   n.get('resource-id') == 'android:id/list' and n.get('package') == WA)
    header = next(n for n in tree.iter('node') if
                  n.get('resource-id') == WA+':id/conversation_contact_name')
    rows = []
    for node in listing:
        single = ET.Element('hierarchy')
        single.append(header)
        target = ET.SubElement(single, 'node', listing.attrib)
        target.append(node)
        events, _, _ = parser(ET.tostring(single, encoding='unicode'), code, reference_day)
        if not events:
            continue
        prefix = ()
        if len(events) == 2 and events[0][0] == 'day' and events[1][0] != 'day':
            prefix, event = (events[0],), events[1]
        elif len(events) == 1:
            event = events[0]
        else:
            raise ExportError('row_event_ambiguous')
        attrs = [n.attrib for n in node.iter('node')]
        bodies = [a.get('text', '') for a in attrs
                  if a.get('resource-id') == WA+':id/message_text']
        read_more = any(a.get('resource-id') == WA+':id/read_more' for a in attrs)
        dividers = [a.get('text', '') for a in attrs if a.get('resource-id') ==
                    WA+':id/conversation_row_date_divider']
        key = None
        if (node.get('resource-id') == WA+':id/conversation_row_text'
              and len(bodies) == 1 and bodies[0] and not read_more
              and ' '.join(bodies[0].split()) != 'Unsupported message'):
            key = ('text', hashlib.sha256(bodies[0].encode()).hexdigest())
        elif event[0] == 'day':
            key = ('divider', dividers[0]) if len(dividers) == 1 and dividers[0] else event
        elif node.get('resource-id') in {WA+':id/'+kind for kind in (
                'conversation_row_image', 'conversation_row_video',
                'conversation_row_audio', 'conversation_row_document',
                'conversation_row_sticker')}:
            # A typed barrier is never an identity anchor or a media record.
            # It can move with a seam proven by an adjacent unique text row.
            key = ('barrier', node.get('resource-id'))
        from .whatsapp_cursor import bounds
        rows.append(Row(key, event, bounds(node), prefix=prefix,
                        edge_text_sliver=(node.get('resource-id') == WA+':id/conversation_row_text'
                                          and not bodies and not dividers and not prefix
                                          and key is None and event == ('clipped',))))
    return Frame(tuple(rows), viewport, button)


def _movement(older, newer, viewport, max_shift):
    lo, hi = viewport[1], viewport[3]
    if not (older.visible and newer.visible):
        return None
    # A clamped edge carries no displacement information.
    shifts = [older.box[i] - newer.box[i] for i in (1, 3)
              if lo < older.box[i] < hi and lo < newer.box[i] < hi]
    if not shifts:
        return None
    if min(shifts) <= 0 or max(shifts) > max_shift or max(shifts) - min(shifts) > 8:
        raise ExportError('inconsistent_row_movement')
    if min(older.box[3], newer.box[3]) <= max(older.box[1], newer.box[1]):
        # A short row can move clear of its old rectangle. It cannot be the
        # physical anchor, but another long intersecting row can prove the seam.
        return None
    return shifts[0]


def _normalize_overlap_edges(older, newer):
    """Only certified outer text slivers without identity/date can be omitted.

    Never remove interior unknown, media, notice, date prefix or an identified
    clipped text. Counterpart content must still be certified by the seam.
    Return exact normalized inputs and trimmed rows, so callers cannot compute
    overlap length against different frames. Inputs remain immutable.
    """
    old_rows,new_rows=older.rows,newer.rows
    old_trim,new_trim=[],[]
    def sliver(row,viewport,edge):
        return (row.visible and row.edge_text_sliver and row.key is None
                and row.event==('clipped',) and not row.prefix
                and (row.box[3]>=viewport[3] if edge=='bottom' else row.box[1]<=viewport[1]))
    while old_rows and sliver(old_rows[-1],older.viewport,'bottom'):
        old_trim.append(old_rows[-1]);old_rows=old_rows[:-1]
    while new_rows and sliver(new_rows[0],newer.viewport,'top'):
        new_trim.append(new_rows[0]);new_rows=new_rows[1:]
    return (replace(older,rows=old_rows),replace(newer,rows=new_rows),
            tuple(old_trim),tuple(new_trim))


def _prepend_identified_frames(older, newer, max_shift=240):
    if older.viewport != newer.viewport:
        raise ExportError('viewport_changed_during_capture')
    # The local-notice wrapper and core MUST use identical normalized frames.
    older, newer, _, _ = _normalize_overlap_edges(older, newer)
    old_counts = Counter(r.key for r in older.rows if r.visible)
    new_counts = Counter(r.key for r in newer.rows if r.visible)
    candidates = []
    for n in range(1, min(len(older.rows), len(newer.rows)) + 1):
        pairs = list(zip(older.rows[-n:], newer.rows[:n]))
        if any(a.key is None or a.key != b.key for a, b in pairs):
            continue
        movements = [_movement(a, b, older.viewport, max_shift) for a, b in pairs]
        anchors = [move for (a, b), move in zip(pairs, movements)
                   if a.key[0] == 'text' and old_counts[a.key] == new_counts[a.key] == 1]
        anchors = [x for x in anchors if x is not None]
        measured = [x for x in movements if x is not None]
        if anchors and max(measured) - min(measured) <= 8:
            candidates.append((n, pairs))
    if len(candidates) != 1:
        raise ExportError('physical_overlap_missing_or_ambiguous')
    n, pairs = candidates[0]
    joined = []
    for a, b in pairs:
        if a.prefix and b.prefix and a.prefix != b.prefix:
            raise ExportError('row_day_changed_during_capture')
        # A date observed on this same physical occurrence may remain attached
        # when the separator clips away. Never take one from a neighbour.
        a = replace(a, prefix=a.prefix or b.prefix)
        # Complete evidence may enrich a clipped identity; clipping never
        # upgrades itself. Conflicting complete observations fail closed.
        if a.event[0] != 'clipped' and b.event[0] != 'clipped' and a.event != b.event:
            # A clipped-away name may appear in another capture of this very
            # same physically matched row. This is direct evidence for that
            # row, never author inheritance from a neighbouring message.
            if (a.event[0] == b.event[0] == 'text' and a.event[2:] == b.event[2:]
                    and (a.event[1] is None or b.event[1] is None)):
                joined.append(replace(a, event=a.event if a.event[1] else b.event))
                continue
            raise ExportError('row_context_changed_during_capture')
        event = b.event if a.event[0] == 'clipped' else a.event
        joined.append(replace(a, event=event))
    return Frame(older.rows[:-n] + tuple(joined) +
                 tuple(replace(r, visible=False) for r in newer.rows[n:]), older.viewport, older.button)


def _leading_opaque_region(frame):
    """Only a leading opaque/day prefix followed by an uninterrupted text run."""
    cut = 0
    unknown = False
    for row in frame.rows:
        if row.key is None and row.event[0] in {'unsupported', 'clipped'}:
            unknown = True
        elif row.event[0] == 'day' and row.key is not None:
            pass
        else:
            break
        cut += 1
    prefix, region = frame.rows[:cut], frame.rows[cut:]
    if not unknown or not prefix or len(region) < 2:
        raise ExportError('opaque_prefix_region_unproved')
    # No media, unknown, new day or unresolved identity inside/after the run.
    if any(row.key is None or row.key[0] != 'text' or
           row.event[0] not in {'text','clipped'} for row in region):
        raise ExportError('opaque_prefix_region_unproved')
    days = {event[1] for row in prefix for event in (*row.prefix, row.event)
            if event[0] == 'day'}
    if len(days) != 1:
        raise ExportError('opaque_prefix_day_unproved')
    return prefix, region, next(iter(days))


def _join_after_opaque_prefix(older, newer, max_shift):
    old_prefix, old_region, old_day = _leading_opaque_region(older)
    new_prefix, new_region, new_day = _leading_opaque_region(newer)
    if old_day != new_day:
        raise ExportError('row_day_changed_during_capture')
    # Every explicit date on the region must agree too. Never guess a day from
    # an unknown row, or carry context across a conflicting day boundary.
    if any(event[1] != old_day for row in (*old_region, *new_region)
           for event in row.prefix if event[0] == 'day'):
        raise ExportError('row_day_changed_during_capture')
    old_part, new_part = (replace(older, rows=old_region, opaque_prefixes=()),
                          replace(newer, rows=new_region, opaque_prefixes=()))
    joined = _prepend_identified_frames(old_part, new_part, max_shift)
    size = len(old_region) + len(new_region) - len(joined.rows)
    counts_old = Counter(r.key for r in old_region if r.visible)
    counts_new = Counter(r.key for r in new_region if r.visible)
    anchors = []
    if any(counts_old[a.key] != 1 or counts_new[b.key] != 1
           for a,b in zip(old_region[-size:], new_region[:size])):
        raise ExportError('opaque_prefix_anchors_unproved')
    for a,b in zip(old_region[-size:], new_region[:size]):
        if (a.event[0] == b.event[0] == 'text' and a.visible and b.visible
                and counts_old[a.key] == counts_new[b.key] == 1):
            move = _movement(a,b,older.viewport,max_shift)
            if move is not None:
                anchors.append(move)
    # Stronger than the original seam: two complete, unique, intersecting
    # occurrences on both sides, with the SAME positive bounded displacement.
    if len(anchors) < 2 or max(anchors) - min(anchors) > 8:
        raise ExportError('opaque_prefix_anchors_unproved')
    observations = older.opaque_prefixes + newer.opaque_prefixes
    if not older.opaque_prefixes:
        observations += (old_prefix,)
    if not newer.opaque_prefixes:
        observations += (new_prefix,)
    if sum(len(prefix) for prefix in observations) + len(joined.rows) + 1 > 200:
        raise ExportError('excessive_context')
    # Prefixes are NOT aligned, equated, deduplicated or assigned chronology.
    # Keep both observations as evidence, and represent their unresolved region
    # by a nonphysical barrier before the certified text run. Its date was
    # explicitly observed and agrees in BOTH inputs, never inferred from text.
    fence = Row(None, ('unsupported',), old_prefix[-1].box, visible=False,
                prefix=(('day', old_day),))
    result = replace(joined, rows=(fence,) + joined.rows,
                     opaque_prefixes=observations)
    # Only this REAL guarded join issues provenance. Bind exact output and raw
    # inputs; the optional index map is justified by this very same full seam,
    # never a body lookup in a different viewport.
    mapping = ()
    if (len(joined.rows) == len(new_region)
            and tuple(r.key for r in joined.rows) == tuple(r.key for r in new_region)
            and len({r.key for r in new_region}) == len(new_region)):
        mapping = tuple((i+1, len(new_prefix)+i) for i in range(len(new_region)))
    proof = _OpaqueJoinProof(_OPAQUE_JOIN_SEAL, result.rows, observations,
                             older, newer, max_shift, mapping)
    return replace(result, _opaque_join=proof)


def prepend_frames(older, newer, max_shift=240):
    try:
        joined = _prepend_identified_frames(older, newer, max_shift)
    except ExportError as exc:
        if str(exc) != 'physical_overlap_missing_or_ambiguous':
            raise
        # Typed generic notice can match only under a separate local proof.
        # Capture and the returned frame keep keyNone; it is never an anchor.
        local = _join_generic_notice_frames(older, newer, max_shift)
        if local is not None:
            return local
        # A new certified region may begin AFTER opaque leading observations;
        # never extend the old seam through an unknown/internal media gap.
        try:
            return _join_after_opaque_prefix(older, newer, max_shift)
        except ExportError as scoped:
            if str(scoped) in {'opaque_prefix_region_unproved',
                               'opaque_prefix_day_unproved',
                               'opaque_prefix_anchors_unproved'}:
                raise exc from None
            raise
    observations = older.opaque_prefixes + newer.opaque_prefixes
    if observations and sum(len(p) for p in observations) + len(joined.rows) > 200:
        raise ExportError('excessive_context')
    return replace(joined, opaque_prefixes=observations)


def same_tail(before, after):
    # Include identities of clipped rows: ('clipped',) alone hid actual changes.
    if not before.rows or not after.rows:
        return False
    # An old unsupported prefix remains a barrier in suffix_records and seams.
    # It must not prevent comparing a fully observed final text occurrence.
    # Unknown terminal rows still cannot prove the tail.
    if any(frame.rows[-1].key is None for frame in (before, after)):
        return False
    return (before.viewport == after.viewport and
            [r.key for r in before.rows] == [r.key for r in after.rows] and
            before.rows[-1].event[0] == after.rows[-1].event[0] == 'text' and
            before.rows[-1].event == after.rows[-1].event and
            not any(a.prefix and b.prefix and a.prefix != b.prefix
                    for a,b in zip(before.rows, after.rows)) and
            all(a.event == b.event and a.event[1].visibility == 'full'
                for a,b in zip(before.rows, after.rows)
                if a.event[0] == 'notice' or b.event[0] == 'notice'))


def suffix_records(events):
    """Certify only the uninterrupted text suffix following any old barrier.

    The comparator must still find the canonical export's unique final anchor
    entirely in this suffix. No record before a media/unknown/clipped row is
    joined to a record after it. A barrier at the tail therefore yields no proof.
    An explicitly observed day stays valid along the physically proven chain;
    authors never carry over between messages.
    """
    from .whatsapp_cursor import records
    day, suffix, excluded = None, [], 0
    for i, event in enumerate(events):
        if event[0] == 'day':
            day = event
            suffix.append(event)
        elif event[0] == 'text' and event[1]:
            suffix.append(event)
        else:
            suffix = [day] if day else []
            excluded = i + 1
    return records(suffix), excluded


def _certified_fence_day(frame):
    """Validate a join-issued leading fence, never a generic day-through-gap rule.

    This is local immutable-object provenance, not an external authentication
    channel. Raw capture cannot issue it; altered rows/archives lose the binding.
    """
    import datetime as dt
    import math
    proof = frame._opaque_join
    if (not isinstance(proof, _OpaqueJoinProof) or proof.seal is not _OPAQUE_JOIN_SEAL
            or proof.rows != frame.rows or proof.observations != frame.opaque_prefixes
            or isinstance(proof.max_shift, bool) or not isinstance(proof.max_shift, (int,float))
            or not math.isfinite(proof.max_shift) or not 0 < proof.max_shift <= 240
            or len(frame.opaque_prefixes) < 2 or len(frame.rows) < 3
            or sum(len(p) for p in frame.opaque_prefixes)+len(frame.rows)>200):
        return None
    fence = frame.rows[0]
    if (fence.key is not None or fence.event != ('unsupported',) or fence.visible
            or len(fence.prefix) != 1 or len(fence.prefix[0]) != 2
            or fence.prefix[0][0] != 'day'):
        return None
    day = fence.prefix[0][1]
    try:
        if dt.date.fromisoformat(day).isoformat() != day:
            return None
    except (ValueError, TypeError):
        return None
    for observed in frame.opaque_prefixes:
        if not observed:
            return None
        days = []
        for row in observed:
            if not row.visible or any(e[0] != 'day' for e in row.prefix):
                return None
            if not ((row.key is None and row.event in (('unsupported',), ('clipped',)))
                    or (row.key is not None and row.key[0] == 'divider'
                        and row.event[0] == 'day')):
                return None
            days.extend(e for e in (*row.prefix, row.event) if e[0] == 'day')
        if days != [('day', day)]:
            return None
    for row in frame.rows[1:]:
        # No clipping/internal unknown/media/notice is certified by the fence.
        if (row.key is None or row.key[0] != 'text' or row.event[0] != 'text'
                or any(e != ('day', day) for e in row.prefix)):
            return None
    # Re-execute the same physical join over its immutable original frames.
    # Forged/stale metadata cannot substitute for the actual seam guards.
    try:
        replay = prepend_frames(proof.older, proof.newer, proof.max_shift)
    except (ExportError, ValueError, TypeError):
        return None
    if (replay.viewport != frame.viewport or replay.rows != frame.rows
            or replay.opaque_prefixes != frame.opaque_prefixes):
        return None
    return ('day', day)


def _context_candidate(frame, baseline):
    """Plan work, never certify records. All dates/clocks/bodies come from UI.

    Ignore absent authors only to locate a possible canonical-tail window.
    Canonical authors are constraints, NEVER values copied into observations.
    Unknowns/notices/media/clipping reset both the run and its day. The final
    reconcile remains mandatory after explicit authors and exact restoration.
    """
    import datetime as dt
    import re
    from .collector_evidence import canonical_tokens, CanonicalGap, normalized
    try:
        old = canonical_tokens(baseline)
    except (ValueError, KeyError, TypeError):
        return (), None, 'context_source_unsupported'
    if any((a.stamp if isinstance(a, CanonicalGap) else a[0]) >
           (b.stamp if isinstance(b, CanonicalGap) else b[0]) for a,b in zip(old,old[1:])):
        return (), None, 'context_unordered_source'
    certified_day = _certified_fence_day(frame)
    day = None
    run = []
    for index, row in enumerate(frame.rows):
        if index == 0 and certified_day is not None:
            day, run = certified_day, []
            continue
        for event in (*row.prefix, row.event):
            if event[0] == 'day':
                day = event
            elif event[0] == 'text' and day:
                _, author, clock, body = event
                try:
                    clock = ' '.join(clock.split())
                    fmt = '%I:%M %p' if re.fullmatch(r'\d{1,2}:\d{2} [AP]M', clock, re.I) else '%H:%M'
                    stamp = day[1]+'T'+dt.datetime.strptime(clock.upper(), fmt).time().isoformat()
                    dt.datetime.fromisoformat(stamp)
                    body = normalized(body)
                    if not body or body == 'Unsupported message' or any(
                            x in body for x in ('<Media omitted>', '(file attached)')):
                        raise ValueError()
                    run.append((index, stamp, normalized(author) if author else None, body, day))
                except (ValueError, TypeError, AttributeError):
                    run, day = [], None
            else:
                run, day = [], None
    if not run:
        return (), None, 'context_requires_explicit_day_and_text_suffix'
    if any(a[1] > b[1] for a,b in zip(run, run[1:])):
        return (), None, 'context_unordered_ui'
    def fits(token, row):
        if isinstance(token, CanonicalGap):
            return token.stamp == row[1] and (row[2] is None or token.sender == row[2])
        return token[0] == row[1] and token[2] == row[3] and (row[2] is None or token[1] == row[2])
    candidates = []
    for size in range(2, min(32, len(old), len(run))+1):
        tail = old[-size:]
        if any(isinstance(t, CanonicalGap) for t in tail):
            continue
        for start in range(len(run)-size+1):
            window = run[start:start+size]
            if not all(fits(t,r) for t,r in zip(tail, window)):
                continue
            # Missing authors or a historical gap cannot disprove alternatives.
            if sum(all(fits(t,r) for t,r in zip(old[i:i+size],window))
                   for i in range(len(old)-size+1)) != 1:
                continue
            candidates.append((start+size, size, start))
    if len({end for end,_,_ in candidates}) != 1:
        return (), None, 'context_anchor_missing_or_ambiguous'
    _, _, start = max(candidates, key=lambda c:c[1])
    selected = run[start:]
    return tuple(r[0] for r in selected), selected[0][4], 'context_candidate_only'


def _candidate_events(frame, indices, day):
    # Drop only participant text preceding the proposed anchor. The planner
    # rejects every interior barrier; the original assembled frame is retained.
    return [day, *(e for i in indices for e in (*frame.rows[i].prefix, frame.rows[i].event))]


class PhysicalTailCursor:
    """Small, bounded gestures; geometry proves a seam, not message context."""
    def __init__(self, ui, reference_day=None, parser=None):
        from .whatsapp_cursor import page
        self.ui, self.reference_day = ui, reference_day
        self.parser = parser or page
        self.author_probes = 0
        self.author_deadline = None
        self.group_deadline = None
        self.safe_trace = []

    def trace(self, stage, frame, **numbers):
        # Fixed stage identifiers and geometry/counts only, never message data.
        self.safe_trace.append(dict(stage=stage, **frame_summary(frame), **numbers))
        del self.safe_trace[:-32]

    async def snapshot(self, code):
        result = await self.ui.call({'operation': 'ui'})
        frame = capture(result['output'], code, self.reference_day, self.parser)
        self.trace('snapshot', frame, **xml_shape(result['output']))
        return frame

    async def enrich(self, code, raw, frame=None, *, indices=None):
        # Existing explicit evidence survives only in the physically sewn frame.
        # Bottom navigation never probes authors. No cache is indexed by text.
        frame = raw if frame is None else frame
        import time
        from .whatsapp_author import explicit_sender
        enriched_rows = list(frame.rows)
        for index, row in enumerate(frame.rows):
            if indices is not None and index not in indices:
                continue
            if not row.visible or row.event[0] != 'text' or row.event[1] is not None:
                continue
            if sum(r.key == row.key and r.box == row.box and r.event == row.event
                   for r in raw.rows) != 1:
                continue  # Current raw capture must expose this complete occurrence.
            if indices is not None and self.author_deadline is None:
                self.author_deadline = self.context_deadline
            limit = 6 if indices is not None else 24
            if self.author_probes >= limit or time.monotonic() >= self.author_deadline:
                raise ExportError('author_probe_budget_exhausted')
            # Probe validates uniqueness, complete body/hour and empty composer.
            self.author_probes += 1
            import asyncio
            try:
                arguments = {'reference_day': self.reference_day} if self.reference_day is not None else {}
                if indices is not None:
                    # Same absolute phase admission deadline, never now+15/45.
                    # explicit_sender times acquisition only; its SAME cleanup
                    # task is shielded/drained OUTSIDE that local timeout.
                    arguments['_acquisition_deadline'] = self.author_deadline
                    sender = await explicit_sender(self.ui, code, row.event[3], row.event[2], **arguments)
                else:
                    # Compatibility for private direct callers with own deadline.
                    sender = await asyncio.wait_for(explicit_sender(
                        self.ui, code, row.event[3], row.event[2], **arguments),
                        timeout=max(.1, self.author_deadline - time.monotonic()))
            except TimeoutError:
                raise ExportError('author_probe_deadline_exceeded') from None
            proof_deadline = getattr(self, '_proof_deadline', None)
            if proof_deadline is not None and time.monotonic() >= proof_deadline:
                raise ExportError('group_total_deadline_exceeded')
            fresh_result = await self.ui.call({'operation': 'ui'})
            fresh = capture(fresh_result['output'], code, self.reference_day, self.parser)
            if proof_deadline is not None and time.monotonic() >= proof_deadline:
                raise ExportError('group_total_deadline_exceeded')
            if fresh != raw:
                raise ExportError('author_probe_occurrence_changed_after_cleanup')
            enriched = replace(row, event=('text', sender, *row.event[2:]))
            # Keep the raw fresh frame for all subsequent occurrence comparisons.
            enriched_rows[index] = enriched
        return replace(frame, rows=tuple(enriched_rows))

    async def _enrich_certified_tail(self, code, current, assembled, tail, indices):
        """Recover selected missing authors in the original COMPLETE raw tail.

        Current older viewport may clip a terminal occurrence. Return to EXACT
        original raw tail, validate the SAME certified pair, probe only mapped
        occurrences there, then rejoin via original physical guards. One pair
        only; longer history chains remain pending rather than guessed.
        """
        proof = assembled._opaque_join
        if (_certified_fence_day(assembled) is None or proof.older != current
                or proof.newer != tail or proof.older.opaque_prefixes
                or proof.newer.opaque_prefixes or len(assembled.opaque_prefixes) != 2):
            raise ExportError('certified_tail_single_pair_required')
        mapping = dict(proof.tail_indices)
        if not indices or any(i not in mapping for i in indices):
            raise ExportError('certified_tail_occurrence_map_missing')
        raw_tail = await self.bottom(code)
        if raw_tail != tail:
            raise ExportError('certified_tail_not_exactly_restored')
        if prepend_frames(current, raw_tail, proof.max_shift) != assembled:
            raise ExportError('certified_tail_pair_changed')
        selected = tuple(mapping[i] for i in indices)
        if len(set(selected)) != len(selected):
            raise ExportError('certified_tail_occurrence_map_ambiguous')
        enriched = await self.enrich(code, raw_tail, indices=selected)
        joined = prepend_frames(current, enriched, proof.max_shift)
        if (_certified_fence_day(joined) is None or len(joined.rows) != len(assembled.rows)
                or any(a.key != b.key or a.box != b.box or a.prefix != b.prefix
                       for a,b in zip(joined.rows, assembled.rows))):
            raise ExportError('certified_tail_rejoin_changed')
        self.trace('certified_tail_context', raw_tail, probes=self.author_probes)
        return joined

    async def bottom(self, code):
        import asyncio
        from .whatsapp_collector import center
        for _ in range(16):
            before = await self.snapshot(code)
            v = before.viewport
            if before.button:
                x, y = center(before.button)
                await self.ui.call({'operation': 'tap', 'x': x, 'y': y})
            else:
                x = (v[0] + v[2]) // 2
                await self.ui.call({'operation': 'swipe', 'x': x, 'y': v[3]-60,
                                    'x2': x, 'y2': v[1]+60, 'duration_ms': 500})
                await asyncio.sleep(.3)
                after = await self.snapshot(code)
                if after.button is None and same_tail(before, after):
                    return after
                if after.button is None and before == after:
                    self.trace('stationary_unproven_tail', after)
                    raise ExportError('stationary_viewport_without_text_tail_proof')
            await asyncio.sleep(.3)
        raise ExportError('live_tail_not_reached')

    async def compare(self, code, baseline):
        import asyncio
        import math
        import time
        now = time.monotonic()
        deadline = self.group_deadline
        if deadline is None:
            deadline = now + 240
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline) or deadline <= now):
            raise ExportError('group_total_deadline_exceeded')
        deadline = min(deadline, now + 240)
        # The SAME absolute group deadline from preflight covers final bottom,
        # not a renewed per-comparator240s or another tail budget after expiry.
        # Reply cleanup drains even if cancellation arrives; no result escapes
        # this scope after expiry. This is not a hard IPC/cleanup duration proof.
        try:
            async with asyncio.timeout_at(deadline):
                return await self._compare_with_deadline(code, baseline, deadline)
        except TimeoutError:
            raise ExportError('group_total_deadline_exceeded') from None

    async def _compare_with_deadline(self, code, baseline, deadline):
        import asyncio
        from .collector_evidence import Observation, reconcile
        import time
        self.author_probes = 0
        self.author_deadline = None
        self._proof_deadline = deadline
        self.context_deadline = deadline - 30
        await self.ui.inspect_group(code)
        tail = await self.bottom(code)
        assembled = tail
        current = tail
        decision = None
        reason = 'bounded_cursor_context_not_resolved'
        lookup_attempted = code in getattr(self, '_lookup_attempted_groups', set())
        # 64 small steps replace 12 jumps almost one viewport high.
        for _ in range(64):
            # PURE attempt of the exact established B suffix proof FIRST.
            # A missing author in an already excluded prefix cannot veto it.
            # Only a NON-pending reconcile decision bypasses demand acquisition;
            # bool(proven), one record, or a pending match never suffices.
            try:
                known, known_excluded = suffix_records(assembled.events())
                known_decision = reconcile(code, baseline,
                    Observation(code, known, True, True, True))
                if known_decision['state'] != 'pending_reconciliation':
                    known_decision['excluded_prefix_events'] = known_excluded
                    decision = known_decision
                    break  # Original final bottom/same_tail/deadline still mandatory.
            except ExportError:
                pass  # Existing B proof incomplete: use the unchanged D path below.
            # One alternative only, after the unchanged PURE B path failed.
            # IPS text-only initial tail; no notice/media/opaque bypass for others.
            if not lookup_attempted:
                from .whatsapp_bounded_lookup import eligible_tail, eligible_source, lookup
                if eligible_tail(code, tail) and eligible_source(baseline):
                    lookup_attempted = True
                    self._lookup_attempted_groups = getattr(self, '_lookup_attempted_groups', set()) | {code}
                    try:
                        decision, self.lookup_raw_frames = await lookup(self, code, baseline, tail, deadline)
                        reason = decision.get('reason', reason)
                    except ExportError as exc:
                        reason, decision = str(exc), None
                    break  # Original final bottom/same_tail/deadline still runs.
            tail_context_replayed = False
            all_explicit = all(row.event[0] != 'text' or row.event[1] for row in assembled.rows)
            if not all_explicit and time.monotonic() >= self.context_deadline:
                reason, decision = 'context_budget_reserved_for_tail', None
                break
            try:
                if all_explicit:
                    # Exact ACTIVE B proof path when all authors already exist.
                    # In particular the REAL joined opaque prefix fence retains
                    # its jointly observed day under suffix_records semantics.
                    # No planner may veto or reinterpret that established proof.
                    proven, excluded = suffix_records(assembled.events())
                else:
                    indices, day, planning_reason = _context_candidate(assembled, baseline)
                    if not indices:
                        raise ExportError(planning_reason)
                    try:
                        if _certified_fence_day(assembled) is not None:
                            assembled = await self._enrich_certified_tail(
                                code, current, assembled, tail, indices)
                            tail_context_replayed = True
                        else:
                            assembled = await self.enrich(code, current, assembled, indices=indices)
                    except ExportError as exc:
                        reason, decision = str(exc), None
                        self.trace('context_probe_refused', current, probes=self.author_probes)
                        break  # No retry on shifted/uncertain UI. Final tail still checked.
                    proven, excluded = suffix_records(_candidate_events(assembled, indices, day))
                    excluded += len(assembled.rows) - len(indices)
                decision = reconcile(code, baseline,
                    Observation(code, proven, True, True, True))
                decision['excluded_prefix_events'] = excluded
                if decision['state'] != 'pending_reconciliation':
                    break
                reason = decision['reason']
                if tail_context_replayed:
                    break  # Now at raw tail; no stale-history continuation after a failed match.
            except ExportError as exc:
                reason = str(exc)
            if time.monotonic() >= self.context_deadline:
                reason, decision = 'context_budget_reserved_for_tail', None
                break
            v = current.viewport
            x, y = (v[0] + v[2]) // 2, (v[1] + v[3]) // 2
            step = history_step(current)
            self.trace('history_gesture', current, requested_step=step)
            await self.ui.call({'operation': 'swipe', 'x': x, 'y': y,
                                'x2': x, 'y2': y+step, 'duration_ms': 500})
            await asyncio.sleep(.3)
            older = await self.snapshot(code)
            if older == current:
                reason = 'history_boundary_without_context'
                break
            try:
                assembled = prepend_frames(older, assembled, max_shift=step*2)
                if assembled.opaque_prefixes:
                    self.trace('opaque_prefix_fence', assembled,
                               opaque_observation_count=len(assembled.opaque_prefixes),
                               opaque_observation_rows=sum(len(p) for p in assembled.opaque_prefixes))
            except ExportError as exc:
                self.trace('seam_rejected', older, requested_step=step, **seam_shape(older, assembled))
                reason, decision = str(exc), None
                break
            current = older
            if len(assembled.rows) > 200:
                reason, decision = 'excessive_context', None
                break
        if time.monotonic() >= deadline:
            raise ExportError('group_total_deadline_exceeded')
        if not same_tail(tail, await self.bottom(code)):
            raise ExportError('live_tail_changed_during_capture')
        if time.monotonic() >= deadline:
            raise ExportError('group_total_deadline_exceeded')
        if decision and decision['state'] != 'pending_reconciliation':
            return decision
        return {'state': 'pending_reconciliation', 'may_export': False, 'reason': reason}


def _join_generic_notice_frames(older,newer,max_shift):
    """Local physical seam only; no day, source or global occurrence is inferred.

    Full generic notices get transient identity keys only inside this proof.
    Every notice must be unique in BOTH complete frames. Existing text overlap,
    context, intersection, displacement and ambiguity guards still apply, plus
    two complete explicit-author text anchors bracketing every matched notice.
    All notice keys are removed in the result, retaining their logical barrier.
    """
    from .whatsapp_notice import GenericDeletedNotice
    def generic(row):
        return row.event[0]=='notice' and isinstance(row.event[1],GenericDeletedNotice)
    old_notices=[r for r in older.rows if generic(r)]
    new_notices=[r for r in newer.rows if generic(r)]
    if not old_notices or not new_notices:return None
    if len(old_notices)!=1 or len(new_notices)!=1:
        raise ExportError('generic_notice_identity_missing_or_ambiguous')
    if older.viewport!=newer.viewport:raise ExportError('viewport_changed_during_capture')
    if max_shift<=0:raise ExportError('generic_notice_shift_unproved')
    budget=sum(len(p) for p in older.opaque_prefixes+newer.opaque_prefixes)
    if len(older.rows)>200 or len(newer.rows)>200:raise ExportError('excessive_context')
    if any(not r.visible or r.key is not None or r.event[1].visibility!='full' for r in old_notices+new_notices):
        raise ExportError('generic_notice_visibility_unproved')
    old_ids=Counter(r.event[1].identity() for r in old_notices)
    new_ids=Counter(r.event[1].identity() for r in new_notices)
    if old_ids!=new_ids or any(n!=1 for n in old_ids.values()):
        raise ExportError('generic_notice_identity_missing_or_ambiguous')
    def local(frame):
        return replace(frame,rows=tuple(replace(r,key=('notice_local',r.event[1].identity())) if generic(r) else r for r in frame.rows))
    a,b,old_trim,new_trim=_normalize_overlap_edges(local(older),local(newer))
    # Native measured shift is14px; do not expand this local proof to large jumps.
    cap=min(max_shift,48)
    joined=_prepend_identified_frames(a,b,cap)
    size=len(a.rows)+len(b.rows)-len(joined.rows)
    pairs=list(zip(a.rows[-size:],b.rows[:size]))
    positions=[i for i,(x,y) in enumerate(pairs) if generic(x) or generic(y)]
    if not positions:raise ExportError('generic_notice_outside_proved_seam')
    counts_a=Counter(r.key for r in a.rows if r.visible)
    counts_b=Counter(r.key for r in b.rows if r.visible)
    anchors=[];moves=[]
    for i,(x,y) in enumerate(pairs):
        if x.key is None or y.key is None or x.key!=y.key:
            raise ExportError('generic_notice_internal_barrier')
        if x.key[0] not in {'text','notice_local','day','divider'}:
            # Never certify an opaque media/unknown identity by matching its class.
            raise ExportError('generic_notice_internal_barrier')
        if x.box[0::2]!=y.box[0::2]:raise ExportError('generic_notice_horizontal_geometry_changed')
        motion=_movement(x,y,a.viewport,cap)
        if motion is not None:moves.append(motion)
        if generic(x):
            if not generic(y) or motion is None:raise ExportError('generic_notice_movement_unproved')
            # Meaningful descendant boxes must prove the SAME physical movement.
            if x.event[1].meaningful_box[0::2]!=y.event[1].meaningful_box[0::2]:
                raise ExportError('generic_notice_horizontal_geometry_changed')
            content=_movement(replace(x,box=x.event[1].meaningful_box),replace(y,box=y.event[1].meaningful_box),a.viewport,cap)
            if content is None or abs(content-motion)>8:raise ExportError('generic_notice_content_movement_unproved')
            moves.append(content)
        elif (x.event[0]==y.event[0]=='text' and x.event==y.event and x.event[1]
                and x.visible and y.visible and counts_a[x.key]==counts_b[y.key]==1
                and motion is not None):
            anchors.append(i)
    if len(anchors)<2 or any(not any(i<notice for i in anchors) or not any(i>notice for i in anchors) for notice in positions):
        raise ExportError('generic_notice_two_sided_anchors_unproved')
    if not moves or max(moves)-min(moves)>8:raise ExportError('inconsistent_row_movement')
    if budget+len(joined.rows)+len(old_trim)+len(new_trim)>200:raise ExportError('excessive_context')
    return replace(joined,rows=tuple(replace(r,key=None) if generic(r) else r for r in joined.rows),opaque_prefixes=older.opaque_prefixes+newer.opaque_prefixes)
