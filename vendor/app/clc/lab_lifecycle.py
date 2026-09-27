"""Conversation-bound laboratory tool; the project's terminal stays restricted."""
from __future__ import annotations
import asyncio, base64, json, logging, re, sqlite3, time
from pathlib import Path
from .control_plane import request

LOG = logging.getLogger(__name__)
HELPER = str(Path(__file__).resolve().parents[1] / "system/lab_conversation.py")
LAB_TOOL_SPEC = {
    "type": "function", "name": "lab_control", "deferLoading": False,
    "description": "Laboratório do projeto no host SASOCQ, pela conversa. Use esta ferramenta em vez do CLI sasocq-lab no terminal restrito. roblox abre a prévia file do projeto; exec/launch executam argv SOMENTE dentro do laboratório isolado. screenshot retorna imagem. name é opcional (Roblox: roblox, roblox-visual ou roblox-art); demais projetos usam o nome padrão informado pelo status. import-local importa perfil preservado no host. start cria área genérica offline (gui/internet/wine opcionais). stop preserva arquivos. Áreas reservadas são encerradas ao terminar o turno. Não usa a VM nem concede broker, root ou barramento ao projeto.",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "operation": {"type": "string", "enum": ["status","start","stop","roblox","exec","launch","screenshot","import-local"]},
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,39}$"},
        "file": {"type": "string"}, "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128},
        "gui": {"type": "boolean"}, "internet": {"type": "boolean"}, "wine": {"type": "boolean"},
        "minutes": {"type": "integer", "minimum": 1, "maximum": 120},
        "prefix": {"type": "string"}, "place": {"type": "string"}
    }, "required": ["operation"]}
}

def migrate_lab_tools(database):
    if not database.is_file(): return 0
    count = 0
    with sqlite3.connect(database, timeout=30) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"threads","thread_dynamic_tools"} <= tables: return 0
        for (thread,) in db.execute("SELECT id FROM threads").fetchall():
            schema = json.dumps(LAB_TOOL_SPEC["inputSchema"], ensure_ascii=False)
            row = db.execute("SELECT position FROM thread_dynamic_tools WHERE thread_id=? AND name=?", (thread, "lab_control")).fetchone()
            if row:
                db.execute("UPDATE thread_dynamic_tools SET description=?,input_schema=?,defer_loading=0,namespace=NULL WHERE thread_id=? AND name=?", (LAB_TOOL_SPEC["description"], schema, thread, "lab_control"))
            else:
                pos = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM thread_dynamic_tools WHERE thread_id=?", (thread,)).fetchone()[0]
                db.execute("INSERT INTO thread_dynamic_tools (thread_id,position,name,description,input_schema,defer_loading,namespace) VALUES (?,?,?,?,?,0,NULL)", (thread,pos,"lab_control",LAB_TOOL_SPEC["description"],schema))
                count += 1
    return count

class LabLifecycle:
    def __init__(self, marker, roots_for_workspace, broker=request):
        self.marker, self.roots_for_workspace, self.broker = marker, roots_for_workspace, broker
        self.lock = asyncio.Lock()
        self.corrupt_marker = False
        try:
            self.leases = json.loads(marker.read_text()) if marker.exists() else {}
            if not isinstance(self.leases, dict) or any(not isinstance(v, dict) or not {"roots", "key", "turn", "last_used"} <= set(v) for v in self.leases.values()):
                raise ValueError("invalid leases")
        except (OSError, ValueError):
            self.leases = {}
            self.corrupt_marker = True
        self.recovering = bool(self.leases) or self.corrupt_marker

    def save(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        temp = self.marker.with_suffix(".tmp")
        temp.write_text(json.dumps(self.leases, ensure_ascii=False))
        temp.chmod(0o600)
        temp.replace(self.marker)

    async def call(self, roots, args, validate_only=False):
        encoded = base64.b64encode(json.dumps({"roots": roots, "args": args, "validate_only": validate_only}).encode()).decode()
        params = {"operation": "exec", "argv": ["runuser","-u","codex-worker","--","/usr/bin/python3",HELPER,encoded], "timeout": 110}
        task = asyncio.create_task(asyncio.to_thread(self.broker, "host-admin", params, timeout=120))
        try: response = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        raw = response.get("result", {}).get("output", {})
        if not response.get("ok") or not isinstance(raw, dict) or raw.get("returncode", 0):
            raise RuntimeError(str(response.get("error") or raw))
        value = json.loads(raw.get("output", "{}"))
        if value.get("error"): raise ValueError(value["error"])
        return value

    async def stop(self, name):
        lease = self.leases[name]
        lease["cleanup_pending"] = True
        self.save()
        result = await self.call(lease["roots"], {"operation":"stop","name":name})
        if result.get("returncode"): raise RuntimeError(result["output"])
        status = await self.call(lease["roots"], {"operation":"status","name":name})
        if status.get("running"): raise RuntimeError("Laboratório ainda ativo")
        del self.leases[name]
        self.save()
        return result

    async def execute(self, workspace, thread, args, turn=""):
        if not isinstance(workspace,str) or not re.fullmatch(r"project:[A-Za-z0-9_.:-]{1,200}", workspace):
            raise ValueError("Ferramenta de laboratório requer conversa de Projetos; Sistema usa o broker")
        if not isinstance(thread,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", thread):
            raise ValueError("Conversa inválida")
        if not isinstance(args,dict) or set(args) - set(LAB_TOOL_SPEC["inputSchema"]["properties"]):
            raise ValueError("Pedido inválido")
        roots = self.roots_for_workspace(workspace)
        if not roots: raise ValueError("Projeto não encontrado")
        async with self.lock:
            if self.recovering: raise RuntimeError("Recuperação de laboratório pendente")
            validated = await self.call(roots, args, validate_only=True)
            name = validated["name"]
            op = args["operation"]
            key = [workspace,thread]
            lease = self.leases.get(name)
            if op == "status":
                result = await self.call(roots, {"operation":"status","name":name})
                return {**result, "owned": bool(lease and lease["key"] == key),
                        "busy": bool(result["running"] and (not lease or lease["key"] != key))}
            if lease and lease["key"] != key:
                raise ValueError("Laboratório reservado por outra conversa")
            if lease and lease.get("cleanup_pending") and op != "stop":
                raise RuntimeError("Encerramento do laboratório pendente; aguarde a recuperação")
            if op == "stop":
                if lease: return await self.stop(name)
                state = await self.call(roots, {"operation":"status","name":name})
                if state["running"]: raise ValueError("Área ativa fora desta conversa; preserve a sessão anterior")
                return {**state, "stopped": True}
            if not lease:
                if op not in {"start","roblox","import-local"}:
                    raise ValueError("Abra o laboratório nesta conversa antes de controlá-lo")
                state = await self.call(roots, {"operation":"status","name":name})
                if state["running"]: raise ValueError("Área ativa fora desta conversa; preserve a sessão anterior")
                if op == "import-local":
                    return await self.call(roots, args)
                lease = {"key": key, "roots": roots, "turn": turn, "last_used": time.time()}
                self.leases[name] = lease
                self.save()  # Persist BEFORE any start that could outlive a disconnected request.
            lease["turn"] = turn or lease["turn"]
            lease["last_used"] = time.time()
            self.save()
            try:
                result = await self.call(roots, args)
                if result.get("returncode") and op in {"start","roblox"}:
                    await self.stop(name)
                return result
            except BaseException:
                if op in {"start","roblox"} and name in self.leases:
                    await self.stop(name)
                raise

    async def handle_server_request(self, workspace, message):
        p = message.get("params") or {}
        if message.get("method") != "item/tool/call" or p.get("tool") != "lab_control": return None
        try:
            value = await self.execute(workspace, p.get("threadId",""), p.get("arguments") or {}, p.get("turnId",""))
            url = value.pop("image_url", None)
            items = [{"type":"inputText", "text":json.dumps(value,ensure_ascii=False)}]
            if url: items.append({"type":"inputImage","imageUrl":url})
            return {"success":not bool(value.get("returncode")), "contentItems":items}
        except Exception as exc:
            return {"success":False,"contentItems":[{"type":"inputText","text":str(exc)}]}

    async def recover(self):
        if self.corrupt_marker:
            raise RuntimeError("Registro de laboratório inválido; recuperação pelo Sistema necessária")
        async with self.lock:
            for name in list(self.leases):
                await self.stop(name)
            self.recovering = False

    async def observe(self, event):
        if event.get("kind") != "notification": return
        note = event.get("notification") or {}
        p = note.get("params") or {}
        if note.get("method") != "turn/completed": return
        key = [event.get("workspace","system"),p.get("threadId") or (p.get("turn") or {}).get("threadId")]
        turn = str((p.get("turn") or {}).get("id") or p.get("turnId") or "")
        async with self.lock:
            for name, lease in list(self.leases.items()):
                if lease["key"] == key and (not lease["turn"] or not turn or turn == lease["turn"]):
                    await self.stop(name)

    async def sweep(self, active):
        if self.recovering:
            await self.recover()
            return
        async with self.lock:
            for name, lease in list(self.leases.items()):
                if lease.get("cleanup_pending") or (tuple(lease["key"]) not in active and time.time() - lease["last_used"] > 60):
                    await self.stop(name)

    async def worker(self, events, active_keys):
        sub = await events.subscribe()
        try:
            while True:
                try:
                    await self.sweep(active_keys())
                    event = await asyncio.wait_for(sub.get(),5)
                    await self.observe(event)
                except asyncio.TimeoutError: pass
                except Exception:
                    LOG.exception("Encerramento de laboratório pendente; será repetido")
                    await asyncio.sleep(5)
        finally:
            await events.unsubscribe(sub)
