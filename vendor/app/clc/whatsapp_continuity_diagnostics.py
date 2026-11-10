"""Private-content-free cursor diagnostics and conservative gesture sizing.

Geometry chooses a gesture; ONLY the unchanged physical seam can accept it.
No content, names, dates, filenames, keys or hashes are returned to status.
"""
from collections import Counter
import xml.etree.ElementTree as ET


def xml_shape(xml):
    """Only a fixed vocabulary of native row/control types, never attributes."""
    accepted = {'conversation_row_text','conversation_row_image','conversation_row_audio',
                'conversation_row_video','conversation_row_document','conversation_row_sticker',
                'conversation_row_date_divider'}
    controls = {'read_more', 'scroll_bottom', 'message_text', 'date', 'name_in_group_tv'}
    rows, flags = Counter(), set()
    for node in ET.fromstring(xml).iter('node'):
        rid = node.get('resource-id', '').split('/')[-1]
        if rid in accepted:
            rows[rid] += 1
        elif rid.startswith('conversation_row_'):
            rows['other_conversation_row'] += 1
        if rid in controls:
            flags.add(rid)
    return {'native_row_counts':dict(rows), 'native_controls_present':sorted(flags)}


def history_step(frame):
    counts = Counter(row.key for row in frame.rows if row.visible)
    heights = [row.box[3]-row.box[1] for row in frame.rows
               if row.visible and row.event[0] == 'text'
               and row.key is not None and row.key[0] == 'text'
               and counts[row.key] == 1
               and frame.viewport[1] < row.box[1] < row.box[3] < frame.viewport[3]]
    cap = min(120, (frame.viewport[3]-frame.viewport[1])//6)
    image_step = _single_image_following_text_step(frame, counts, cap)
    if image_step is not None:
        return image_step
    # A long unique text identity can propose a measurable physical seam
    # while a short neighbour moves. This is a gesture proposal only: the same
    # exact identity/order/positive/intersecting seam must accept the result.
    # Opaque prefixes and notices require multiple anchors and retain the
    # conservative short step. No event is upgraded and no guard is relaxed.
    # Clipped bodies remain identities only, as in the existing frame proof.
    # A free edge with two-step headroom can propose movement without claiming
    # completeness. Both-edge-clamped rows provide no measurable edge.
    geometric_heights = [row.box[3]-row.box[1] for row in frame.rows
                         if row.visible and row.key is not None
                         and row.key[0] == 'text' and counts[row.key] == 1
                         and row.event[0] in {'text','clipped'}
                         and any(frame.viewport[1] < edge < frame.viewport[3]-2*cap
                                 for edge in (row.box[1],row.box[3]))]
    long_text_run = (geometric_heights and max(geometric_heights) >=
                     (frame.viewport[3]-frame.viewport[1])//2
                     and not frame.opaque_prefixes
                     and all(row.key is not None and row.key[0] == 'text'
                             and row.event[0] in {'text','clipped'}
                             for row in frame.rows if row.visible))
    sizing = max(geometric_heights) if long_text_run else min(heights) if heights else None
    return min(cap, max(24, sizing//3)) if sizing is not None else cap


def seam_shape(older, newer):
    """Raw pre-trimming overlap counters, never a substitute for seam proof."""
    aligned, pairs, intersecting = 0, 0, 0
    old_counts = Counter(row.key for row in older.rows if row.visible)
    new_counts = Counter(row.key for row in newer.rows if row.visible)
    for size in range(1, min(len(older.rows), len(newer.rows), 200)+1):
        candidate = list(zip(older.rows[-size:], newer.rows[:size]))
        if any(a.key is None or a.key != b.key for a,b in candidate):
            continue
        aligned += 1
        for a,b in candidate:
            if (a.visible and b.visible and a.key[0] == 'text'
                    and old_counts[a.key] == new_counts[a.key] == 1):
                pairs += 1
                intersecting += min(a.box[3],b.box[3]) > max(a.box[1],b.box[1])
    return {'raw_identity_suffix_candidates':aligned,
            'raw_unique_text_pairs':pairs,
            'raw_intersecting_text_pairs':intersecting,
            'same_viewport':older.viewport == newer.viewport}


def frame_summary(frame):
    known = {'text', 'day', 'clipped', 'unsupported', 'notice'}
    kinds = Counter(row.event[0] if row.event[0] in known else 'other'
                    for row in frame.rows)
    keys = Counter(row.key for row in frame.rows if row.visible and row.key is not None)
    tail = frame.rows[-1] if frame.rows else None
    def key_kind(row):
        value = row.key[0] if row and row.key is not None else 'none'
        return value if value in {'text','barrier','divider','day','none'} else 'other'
    return {
        'row_count':len(frame.rows), 'event_counts':dict(kinds),
        'visible_rows':sum(row.visible for row in frame.rows),
        'unique_visible_text_keys':sum(row.visible and key_kind(row) == 'text'
                                       and keys[row.key] == 1 for row in frame.rows),
        'tail_event':tail.event[0] if tail and tail.event[0] in known else 'other',
        'tail_key_kind':key_kind(tail),
        'tail_has_explicit_author':bool(tail and tail.event[0] == 'text' and tail.event[1]),
        'tail_day_prefix_count':len(tail.prefix) if tail else 0,
        'scroll_bottom_present':frame.button is not None,
        'viewport_height':frame.viewport[3]-frame.viewport[1],
        'suggested_history_step':history_step(frame),
    }


def _single_image_following_text_step(frame, counts, cap):
    """Proposal only for the native demonstrated one-image/text region.

    No image identity/anchor is produced. Existing seam guards alone accept the
    resulting capture. Clipped events never become complete participant events.
    """
    if frame.opaque_prefixes or not frame.rows or any(not r.visible for r in frame.rows):
        return None
    images=[i for i,r in enumerate(frame.rows) if r.key ==
            ('barrier','com.whatsapp.w4b:id/conversation_row_image')
            and r.event[0] in {'unsupported','clipped'}]
    if len(images)!=1:return None
    image=images[0];lo,hi=frame.viewport[1],frame.viewport[3]
    left,right=0,len(frame.rows)
    def sliver(r):
        return (getattr(r,'edge_text_sliver',False) and r.key is None
                and r.event==('clipped',) and not r.prefix)
    while left<right and sliver(frame.rows[left]) and frame.rows[left].box[1]<=lo:left+=1
    while right>left and sliver(frame.rows[right-1]) and frame.rows[right-1].box[3]>=hi:right-=1
    after=[];full=[]
    for i,r in enumerate(frame.rows[left:right],left):
        if i==image:continue
        if (r.key is None or r.key[0]!='text' or counts[r.key]!=1
                or r.event[0] not in {'text','clipped'}):return None
        if r.event[0]=='clipped' and lo<r.box[1] and r.box[3]<hi:return None
        if i>image:
            after.append(r)
            if r.event[0]=='text' and lo<r.box[1]<r.box[3]<hi:
                full.append(r.box[3]-r.box[1])
    # Later native pair has147full plus86identified/clipped; do not require the
    # clipped row to upgrade. Single identified text alone is outside evidence.
    if len(after)<2 or not full or max(full)<72:return None
    return min(cap,60,max(full)-12)
