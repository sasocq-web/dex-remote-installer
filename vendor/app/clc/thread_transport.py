"""Lossless thread JSON transport without blocking the asyncio event loop."""
import asyncio
import gzip
import json
from starlette.responses import Response


def accepts_gzip(header: str) -> bool:
    weights = {}
    for entry in header.lower().split(','):
        parts = [part.strip() for part in entry.split(';')]
        if not parts[0]:
            continue
        weight = 1.0
        for param in parts[1:]:
            if param.startswith('q='):
                try:
                    weight = float(param[2:])
                except ValueError:
                    weight = 0.0
        weights[parts[0]] = weight
    return weights.get('gzip', weights.get('*', 0.0)) > 0


def _encode(payload, use_gzip: bool):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    headers = {'Vary': 'Accept-Encoding', 'Cache-Control': 'private, no-store'}
    if use_gzip and len(body) >= 1024:
        compressed = gzip.compress(body, compresslevel=1, mtime=0)
        if len(compressed) < len(body):
            body = compressed
            headers['Content-Encoding'] = 'gzip'
    return Response(body, media_type='application/json', headers=headers)


async def thread_json_response(payload, request):
    return await asyncio.to_thread(_encode, payload, accepts_gzip(request.headers.get('accept-encoding', '')))


def thread_window(result, item_limit=None, before_item=None):
    """Retain every turn's metadata; paginate items using a stable item ID."""
    if item_limit is None:
        return result
    thread = result.get('thread')
    if not isinstance(thread, dict):
        return result
    turns = thread.get('turns') or []
    items = [item for turn in turns for item in (turn.get('items') or [])]
    end = len(items)
    if before_item:
        end = next((i for i, item in enumerate(items) if item.get('id') == before_item), -1)
        if end < 0:
            raise ValueError('O histórico mudou. Reabra a conversa para carregar os itens anteriores.')
    start = max(0, end - item_limit)
    while start > 0 and not items[start].get('id'):
        start -= 1
    offset = 0
    selected_turns = []
    for turn in turns:
        entries = turn.get('items') or []
        completed = str(turn.get('status') or '').lower() == 'completed'
        final_index = next((i for i in range(len(entries)-1, -1, -1)
                            if entries[i].get('type') in {'agentMessage', 'assistantMessage', 'agent_message'}), -1) if completed else -1
        selected = []
        for i, item in enumerate(entries):
            position = offset + i
            anchor = item.get('type') in {'userMessage', 'user_message'} or i == final_index
            if start <= position < end or (not before_item and anchor):
                selected.append({**item, '_historyOrder': position,
                    '_historyTurnId': turn.get('id'), '_historyTurnComplete': completed,
                    '_historyFinal': i == final_index})
        selected_turns.append({**turn, 'items': selected})
        offset += len(entries)
    return {**result, 'thread': {**thread, 'turns': selected_turns, '_history': {
        'has_more': start > 0, 'before_item': items[start].get('id') if start < end else None,
        'total_items': len(items), 'remaining_items': start,
    }}}
