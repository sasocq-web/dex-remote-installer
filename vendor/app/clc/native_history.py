"""Read bounded history pages from app-server without hydrating the whole rollout."""
import base64
import json

PREFIX = "native1."
PAGE_ITEMS = 20
PAGE_BYTES = 1024 * 1024


def encode_cursor(value):
    return PREFIX + base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode_cursor(value, thread_id):
    if not value:
        return {"thread": thread_id, "offset": 0, "cursor": None}
    if not value.startswith(PREFIX) or len(value) > 32768:
        raise ValueError("Reabra a conversa para atualizar a paginação do histórico.")
    try:
        state = json.loads(base64.urlsafe_b64decode(value[len(PREFIX):]))
        if state["thread"] != thread_id or not isinstance(state["offset"], int) or state["offset"] < 0 or not isinstance(state.get("cursor"), (str, type(None))):
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Cursor do histórico inválido. Reabra a conversa.") from exc


async def read_native_page(rpc, thread_id, item_limit, before_item=None):
    state = decode_cursor(before_item, thread_id)
    result = await rpc("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = dict(result["thread"])
    limit = min(PAGE_ITEMS, item_limit)
    params = {"threadId": thread_id, "sortDirection": "desc", "limit": limit}
    if state["cursor"]:
        params["cursor"] = state["cursor"]
    while True:
        page = await rpc("thread/items/list", params)
        entries = page.get("data") or []
        # Keep every item intact. An individual large attachment remains a
        # one-item page; never silently truncate text, images or tool output.
        if len(entries) <= 1 or len(json.dumps(entries, ensure_ascii=False).encode()) <= PAGE_BYTES:
            break
        params["limit"] = max(1, len(entries) // 2)
    turns = []
    cursor = None
    seen = set()
    while True:
        turn_params = {"threadId": thread_id, "limit": 100, "itemsView": "notLoaded", "sortDirection": "asc"}
        if cursor:
            turn_params["cursor"] = cursor
        turn_page = await rpc("thread/turns/list", turn_params)
        turns.extend({**turn, "items": []} for turn in turn_page.get("data") or [])
        cursor = turn_page.get("nextCursor")
        if not cursor:
            break
        if cursor in seen:
            raise ValueError("A paginação do histórico não avançou.")
        seen.add(cursor)
    by_id = {turn["id"]: turn for turn in turns}
    boundary = state.get("boundary")
    agent_seen = bool(state.get("agent_seen"))
    for index, entry in enumerate(entries):
        turn_id = entry["turnId"]
        if turn_id != boundary:
            boundary, agent_seen = turn_id, False
        turn = by_id.get(turn_id)
        if turn is None:
            # A live turn may start between the metadata and item requests.
            turn = {"id": turn_id, "status": "inProgress", "items": []}
            turns.append(turn)
            by_id[turn_id] = turn
        item = entry["item"]
        completed = str(turn.get("status", "")).lower() == "completed"
        agent = item.get("type") in {"agentMessage", "assistantMessage", "agent_message"}
        final = completed and agent and (item.get("phase") == "final_answer" or (not item.get("phase") and not agent_seen))
        if agent:
            agent_seen = True
        turn["items"].append({**item, "startedAtMs": entry.get("startedAtMs", item.get("startedAtMs")),
            "completedAtMs": entry.get("completedAtMs", item.get("completedAtMs")),
            "_historyOrder": -(state["offset"] + index + 1), "_historyTurnId": turn_id,
            "_historyTurnComplete": completed, "_historyFinal": final})
    for turn in turns:
        turn["items"].reverse()
    next_cursor = page.get("nextCursor")
    offset = state["offset"] + len(entries)
    thread["turns"] = turns
    thread["_history"] = {"has_more": bool(next_cursor), "before_item": encode_cursor({
        "thread": thread_id, "cursor": next_cursor, "offset": offset,
        "boundary": boundary, "agent_seen": agent_seen}) if next_cursor else None,
        "loaded_items": len(entries), "transport": "native"}
    return {**result, "thread": thread}
