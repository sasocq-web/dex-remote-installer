"""Conservative sequence comparison for a trusted official-UI adapter.

This module does not authenticate observations or run Android. The host must
recover and verify the canonical export, parse its records, confirm the actual
group identity, resolve day/author context, and prove it is viewing the live
tail before calling reconcile. Never feed it user/feedback-supplied flags.
Decisions authorize at most collection, never upload, completeness, or deletion.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import unicodedata


def normalized(value):
    if not isinstance(value, str):
        raise ValueError('non_text')
    return ' '.join(unicodedata.normalize('NFC', value).split())


def key(record, allow_media=False):
    stamp = datetime.fromisoformat(record['stamp'])
    if stamp.tzinfo is not None or stamp.second or stamp.microsecond:
        # This adapter contract resolves local export dates to minute precision.
        raise ValueError('unsupported_time_precision')
    sender, body = normalized(record['sender']), normalized(record['body'])
    if not sender or not body or sender == '[sistema]':
        raise ValueError('incomplete_participant')
    if body == 'Unsupported message':
        raise ValueError('unsupported_source_body')
    if not allow_media and any(marker in body for marker in ('<Media omitted>', '(file attached)')):
        # A file placeholder cannot prove newly received participant text.
        raise ValueError('media_requires_separate_evidence')
    return (stamp.isoformat(), sender, body)


@dataclass(frozen=True)
class CanonicalGap:
    """An unresolved EMPTY or literal Unsupported message historical body, retaining its known time/sender.

    Never a participant key or anchor. It may hide any body, so a possible
    alternative occurrence through it must still prevent unique anchoring.
    """
    stamp: str
    sender: str


def canonical_tokens(records):
    tokens = []
    for record in records:
        try:
            token = key(record, allow_media=True)
        except ValueError as exc:
            # Only empty/literal unsupported historical bodies get a typed barrier. Other
            # invalid timestamps, missing senders, system senders or types keep
            # the existing fail-closed behavior. UI keys never use this path.
            if (str(exc) not in {'incomplete_participant','unsupported_source_body'}
                    or normalized(record['body']) not in {'','Unsupported message'}
                    or not normalized(record['sender'])
                    or normalized(record['sender']) == '[sistema]'):
                raise
            stamp, sender, _ = key(dict(record, body='opaque-canonical-body'),
                                   allow_media=True)
            token = CanonicalGap(stamp, sender)
        tokens.append(token)
    return tokens


def token_stamp(token):
    return token.stamp if isinstance(token, CanonicalGap) else token[0]


def compatible_window(window, pattern):
    """Known mismatches disprove an alternative; an unknown body never does."""
    return len(window) == len(pattern) and all(
        (a.stamp == b[0] and a.sender == b[1])
        if isinstance(a, CanonicalGap) else a == b
        for a, b in zip(window, pattern))


@dataclass(frozen=True)
class Observation:
    group: str
    records: tuple
    identity_verified: bool
    at_live_tail: bool
    complete_context: bool


def reconcile(group, baseline, observation):
    """Find a unique baseline-tail anchor followed by new complete UI rows.

    Repeated occurrences remain ordered; equality never collapses messages.
    No-change only covers the visible text suffix, not historic edits or media.
    Ambiguous or unsupported evidence stays pending, without authorizing export.
    Return only hashes/counts/reasons so receipts need not expose message text.
    """
    pending = lambda reason: {'state': 'pending_reconciliation', 'reason': reason,
                              'may_export': False}
    if (observation.group != group or not observation.identity_verified
            or not observation.at_live_tail or not observation.complete_context):
        return pending('unverified_ui_context')
    try:
        # System notices are excluded on both sides by the trusted adapter.
        old = canonical_tokens(baseline)
        live = [key(r) for r in observation.records]
    except (ValueError, KeyError, TypeError):
        return pending('unsupported_or_incomplete_record')
    if len(old) < 2 or len(live) < 2 or len(live) > 200:
        return pending('insufficient_or_excessive_context')
    if any(token_stamp(a) > token_stamp(b) for rows in (old, live) for a, b in zip(rows, rows[1:])):
        return pending('unordered_records')
    anchors = []
    for size in range(2, min(32, len(old), len(live)) + 1):
        tail = old[-size:]
        if any(isinstance(token, CanonicalGap) for token in tail):
            continue  # Never anchor across an unresolved canonical occurrence.
        occurrences = sum(compatible_window(old[i:i+size], tail)
                          for i in range(len(old)-size+1))
        if occurrences != 1:
            continue
        for start in range(len(live)-size+1):
            if live[start:start+size] == tail:
                anchors.append((start+size, size))
    if len({end for end, _ in anchors}) != 1:
        return pending('missing_or_ambiguous_tail_anchor')
    end, size = max(anchors, key=lambda pair: pair[1])
    following = live[end:]
    # The shared anchor proves ordering even for identical same-minute repeats.
    evidence = hashlib.sha256(json.dumps(live[end-size:], ensure_ascii=False,
                                         separators=(',', ':')).encode()).hexdigest()
    return {'state': 'new_participant_sequence' if following else 'visible_tail_matches',
            'may_export': bool(following), 'anchor_records': size,
            'new_occurrences': len(following), 'evidence_sha256': evidence,
            'coverage': 'visible_text_suffix_only', 'media_coverage': 'unproven'}
