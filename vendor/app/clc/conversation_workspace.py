"""Conversation navigation, lineage and explicit immutable artifact snapshots.

No background scans, new workers or changes to agent permissions. All RPC calls
use the same project bridge and authentication boundary as the normal composer.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .workbench import JsonStore, _relative_file, ARTIFACT_EXTENSIONS


class Bookmark(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=160)
    enabled: bool = True


class Branch(BaseModel):
    turn_id: str = Field(min_length=1, max_length=200)
    item_id: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=8, max_length=100)


class Snapshot(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    thread_id: str = Field(min_length=1, max_length=200)


class ReviewComment(BaseModel):
    turn_id: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=4096)
    line: int = Field(ge=1, le=10_000_000)
    side: str = Field(pattern="^(old|new)$")
    body: str = Field(min_length=1, max_length=20_000)


def item_text(item):
    for name in ("text", "message", "content"):
        value = item.get(name)
        if isinstance(value, str):
            return value
    content = item.get("content", [])
    if isinstance(content, list):
        text = "\n".join(str(p.get("text") or p.get("output_text") or p.get("input_text") or "")
                         for p in content if isinstance(p, dict))
        if text:
            return text
    return str(item.get("aggregatedOutput") or item.get("output") or "")


def thread_records(thread):
    result = []
    for turn in thread.get("turns", []):
        items = turn.get("items", [])
        assistants = [i for i in items if i.get("type") in ("agentMessage", "assistantMessage", "agent_message")]
        final_id = assistants[-1].get("id") if assistants and turn.get("status") == "completed" else None
        for item in items:
            kind = item.get("type", "")
            text = item_text(item)
            if kind in ("userMessage", "user_message") and "Solicitação atual:\n" in text:
                text = text.rsplit("Solicitação atual:\n", 1)[1]
            result.append({"id": item.get("id"), "turn_id": turn.get("id"), "type": kind,
                           "completed": turn.get("status") == "completed", "text": text,
                           "final": bool(item.get("id") == final_id),
                           "request": kind in ("userMessage", "user_message")})
    return result


def install_conversation_workspace(app, *, session_guard, project_lookup, project_for_thread,
                                   rpc, bridge_for_thread, profiles, operations, config_dir):
    router = APIRouter(prefix="/api/conversation")
    store = JsonStore(Path(config_dir) / "conversation-workspace.json")
    snapshots = Path(config_dir) / "conversation-versions"
    fork_lock = asyncio.Lock()

    def observe_plan(event):
        msg = event.get("notification") or {}
        if msg.get("method") == "turn/plan/updated":
            params = msg.get("params") or {}
            operations.record_request_plan(params.get("threadId"), params.get("turnId"), params.get("plan"))

        elif msg.get("method") == "item/completed":
            params = msg.get("params") or {}
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                if item.get("phase") == "commentary":
                    operations.record_progress_note(params.get("threadId"), params.get("turnId"), item.get("text"))
                operations.record_reported_progress(params.get("threadId"), params.get("turnId"), item.get("text"))

    @app.on_event("startup")
    async def subscribe_request_progress():
        from .main import events
        events.add_critical_observer(observe_plan)

    @app.on_event("shutdown")
    async def unsubscribe_request_progress():
        from .main import events
        events.remove_critical_observer(observe_plan)

    def guard(request, project_id, thread_id=None, mutate=False):
        session_guard(request, mutate=mutate)
        project = project_lookup(project_id)
        if thread_id and project_for_thread(thread_id) != project.id:
            raise HTTPException(403, "A conversa não pertence a este projeto")
        return project

    async def read(thread_id):
        value = await rpc("thread/read", {"threadId": thread_id, "includeTurns": True}, target=bridge_for_thread(thread_id))
        return value.get("thread", value)

    def save_context(thread_id, turn_id, value):
        # Only the effective envelope; no message, file contents or credentials.
        with store.lock:
            data = store.read()
            turns = data.setdefault("contexts", {}).setdefault(thread_id, {})
            turns[turn_id] = {**value, "sent_at": time.time()}
            while len(turns) > 100:
                del turns[next(iter(turns))]
            store.write(data)

    @router.get("/{project_id}/threads/{thread_id}/index")
    async def index(request: Request, project_id: str, thread_id: str, q: str = "", offset: int = 0, bookmarks_only: bool = False):
        guard(request, project_id, thread_id)
        if len(q) > 500 or offset < 0:
            raise HTTPException(400, "Busca inválida")
        thread = await read(thread_id)
        def build():
            records = thread_records(thread)
            marked = store.read().get("bookmarks", {}).get(thread_id, {})
            selected = []
            for record in records:
                text = record.pop("text")
                position = text.casefold().find(q.casefold()) if q else -1
                bookmark = marked.get(record["id"])
                if bookmarks_only and not bookmark:
                    continue
                if q and position < 0:
                    continue
                if not q and not (record["request"] or record["final"] or bookmark):
                    continue
                start = max(0, position - 80) if q else 0
                record.update(preview=text[start:start + 420], bookmark=bookmark, match=q)
                selected.append(record)
            return {"items": selected[offset:offset + 100], "total": len(selected),
                    "offset": offset, "next_offset": offset + 100 if offset + 100 < len(selected) else None,
                    "lineage": store.read().get("lineage", {}).get(thread_id)}
        return await asyncio.to_thread(build)

    @router.post("/{project_id}/threads/{thread_id}/bookmark")
    async def bookmark(request: Request, project_id: str, thread_id: str, payload: Bookmark):
        guard(request, project_id, thread_id, True)
        records = thread_records(await read(thread_id))
        if not any(r["id"] == payload.item_id for r in records):
            raise HTTPException(404, "Mensagem não encontrada")
        with store.lock:
            data = store.read()
            marks = data.setdefault("bookmarks", {}).setdefault(thread_id, {})
            if payload.enabled:
                if len(marks) >= 500 and payload.item_id not in marks:
                    raise HTTPException(409, "Limite de 500 marcadores nesta conversa")
                marks[payload.item_id] = {"label": payload.label, "created_at": time.time()}
            else:
                marks.pop(payload.item_id, None)
            store.write(data)
        return {"ok": True}

    @router.get("/{project_id}/threads/{thread_id}/item")
    async def locate(request: Request, project_id: str, thread_id: str, item_id: str):
        guard(request, project_id, thread_id)
        source = await read(thread_id)
        order = 0
        for turn in source.get("turns", []):
            items = turn.get("items", [])
            final_id = next((i.get("id") for i in reversed(items)
                             if i.get("type") in ("agentMessage", "assistantMessage", "agent_message")), None)
            for item in items:
                if item.get("id") == item_id:
                    return {"item": {**item, "_historyOrder": order, "_historyTurnId": turn.get("id"),
                                     "_historyTurnComplete": turn.get("status") == "completed",
                                     "_historyFinal": turn.get("status") == "completed" and item_id == final_id}}
                order += 1
        raise HTTPException(404, "Mensagem não encontrada")

    @router.post("/{project_id}/threads/{thread_id}/branch")
    async def branch(request: Request, project_id: str, thread_id: str, payload: Branch):
        guard(request, project_id, thread_id, True)
        # A retry uses the same key. Never create two forks for a successful request.
        async with fork_lock:
            data = store.read()
            key = f"{project_id}:{thread_id}:{payload.key}"
            prior = data.get("fork_requests", {}).get(key)
            signature = {"turn_id": payload.turn_id, "item_id": payload.item_id}
            if prior:
                if prior.get("signature") != signature:
                    raise HTTPException(409, "A chave já foi usada para outra origem")
                if prior.get("thread_id"):
                    return {"thread": {"id": prior["thread_id"]}}
                raise HTTPException(409, "Ramificação em reconciliação; não repita com outra chave")
            source = await read(thread_id)
            turn = next((t for t in source.get("turns", []) if t.get("id") == payload.turn_id), None)
            if not turn or turn.get("status") != "completed" or not any(i.get("id") == payload.item_id for i in turn.get("items", [])):
                raise HTTPException(409, "Escolha uma mensagem de um turno concluído")
            with store.lock:
                data = store.read()
                data.setdefault("fork_requests", {})[key] = {"signature": signature, "created_at": time.time()}
                store.write(data)
            result = await rpc("thread/fork", {"threadId": thread_id, "lastTurnId": payload.turn_id}, target=bridge_for_thread(thread_id))
            fork = result.get("thread") or result.get("forkedThread") or {}
            fork_id = str(fork.get("id") or "")
            if not fork_id:
                raise HTTPException(502, "O servidor não retornou a conversa ramificada")
            profiles.save_thread(fork_id, project_id, profiles.effective(project_id, thread_id))
            lineage = {"thread_id": thread_id, **signature, "project_id": project_id, "created_at": time.time()}
            with store.lock:
                data = store.read()
                data.setdefault("lineage", {})[fork_id] = lineage
                data["fork_requests"][key]["thread_id"] = fork_id
                store.write(data)
            operations.set_metadata("threads", fork_id, {"source_conversation": lineage})
            return result

    @router.get("/{project_id}/threads/{thread_id}/context")
    async def context(request: Request, project_id: str, thread_id: str):
        guard(request, project_id, thread_id)
        return {"turns": store.read().get("contexts", {}).get(thread_id, {}),
                "profile": profiles.effective(project_id, thread_id).as_dict()}

    @router.get("/{project_id}/threads/{thread_id}/changes")
    async def changes(request: Request, project_id: str, thread_id: str):
        guard(request, project_id, thread_id)
        source = await read(thread_id)
        turns = source.get("turns", [])
        turn = turns[-1] if turns else {}
        result = []
        remaining = 1_500_000
        for item in turn.get("items", []):
            if item.get("type") != "fileChange":
                continue
            for change in item.get("changes", []):
                diff = str(change.get("diff") or "")
                result.append({"path": str(change.get("path") or change.get("filePath") or ""),
                               "diff": diff[:remaining]})
                remaining = max(0, remaining - len(diff))
                if len(result) >= 500 or remaining == 0:
                    break
            if len(result) >= 500 or remaining == 0:
                break
        return {"turn_id": turn.get("id"), "status": turn.get("status"), "changes": result,
                "truncated": remaining == 0 or len(result) >= 500}

    @router.get("/{project_id}/threads/{thread_id}/review-comments")
    async def review_comments(request: Request, project_id: str, thread_id: str):
        guard(request, project_id, thread_id)
        return {"comments": store.read().get("reviews", {}).get(thread_id, [])}

    @router.post("/{project_id}/threads/{thread_id}/review-comments")
    async def review_comment(request: Request, project_id: str, thread_id: str, payload: ReviewComment):
        guard(request, project_id, thread_id, True)
        source = await read(thread_id)
        turn = next((t for t in source.get("turns", []) if t.get("id") == payload.turn_id), {})
        paths = {str(change.get("path") or change.get("filePath") or "")
                 for item in turn.get("items", []) if item.get("type") == "fileChange"
                 for change in item.get("changes", [])}
        if payload.path not in paths:
            raise HTTPException(404, "Arquivo não registrado neste turno")
        if not payload.body.strip():
            raise HTTPException(400, "Escreva um comentário")
        with store.lock:
            data = store.read()
            comments = data.setdefault("reviews", {}).setdefault(thread_id, [])
            if len(comments) >= 500:
                raise HTTPException(409, "Limite de 500 comentários por conversa")
            comment = {**payload.model_dump(), "id": uuid.uuid4().hex, "created_at": time.time()}
            comments.append(comment)
            store.write(data)
        return {"comment": comment}

    @router.get("/{project_id}/versions")
    async def versions(request: Request, project_id: str, thread_id: str):
        guard(request, project_id, thread_id)
        records = store.read().get("versions", {}).get(project_id, [])
        return {"versions": [r for r in records if r["thread_id"] == thread_id]}

    def snapshot_file(project, payload):
        root = Path(project.path).resolve()
        path = _relative_file(root, payload.path)
        if path.suffix.lower() not in ARTIFACT_EXTENSIONS or any(p.startswith(".") for p in path.relative_to(root).parts):
            raise HTTPException(400, "Este tipo de arquivo não pode ser versionado pela biblioteca")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise HTTPException(413, "Máximo de 10 MiB por arquivo")
        with path.open("rb") as stream:
            content = stream.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "O arquivo excede 10 MiB")
        digest = hashlib.sha256(content).hexdigest()
        relative = str(path.relative_to(root))
        with store.lock:
            data = store.read()
            records = data.setdefault("versions", {}).setdefault(project.id, [])
            prior = next((r for r in records if r["path"] == relative and r["sha256"] == digest and r["thread_id"] == payload.thread_id), None)
            if prior:
                return prior
            if len(records) >= 500 or sum(r["size"] for r in records) + len(content) > 50 * 1024 * 1024:
                raise HTTPException(409, "A biblioteca atingiu seu limite de 500 versões ou 50 MiB por projeto")
            version_id = uuid.uuid4().hex
            snapshots.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = snapshots / version_id
            with target.open("xb") as stream:
                os.chmod(target, 0o600)
                stream.write(content)
            record = {"id": version_id, "path": relative, "thread_id": payload.thread_id,
                      "size": len(content), "sha256": digest, "created_at": time.time()}
            records.append(record)
            try:
                store.write(data)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return record

    @router.post("/{project_id}/versions")
    async def snapshot(request: Request, project_id: str, payload: Snapshot):
        project = guard(request, project_id, payload.thread_id, True)
        return {"version": await asyncio.to_thread(snapshot_file, project, payload)}

    @router.get("/{project_id}/versions/{version_id}")
    async def version(request: Request, project_id: str, version_id: str, text: bool = False):
        guard(request, project_id)
        record = next((r for r in store.read().get("versions", {}).get(project_id, []) if r["id"] == version_id), None)
        if not record:
            raise HTTPException(404, "Versão não encontrada")
        guard(request, project_id, record["thread_id"])
        target = snapshots / record["id"]
        if not target.is_file():
            raise HTTPException(404, "Conteúdo indisponível")
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if text:
            if target.stat().st_size > 1_000_000:
                raise HTTPException(413, "Comparação de texto limitada a 1 MB")
            raw = await asyncio.to_thread(target.read_bytes)
            try:
                value = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(415, "Compare arquivos de texto UTF-8; baixe outros formatos")
            return PlainTextResponse(value, headers=headers)
        return FileResponse(target, filename=Path(record["path"]).name, media_type="application/octet-stream", headers=headers)

    app.include_router(router)
    return {"save_context": save_context}
