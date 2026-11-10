"""Project-authenticated requests to System without the revoked Dot transport."""
import hashlib
import json
import re

SYSTEM_REQUEST_TOOL_SPEC = {
    "type": "function", "name": "system_request", "deferLoading": False,
    "description": "Registra solicitações auditáveis ao Codex Sistema pela identidade nativa da conversa, sem usar a ponte Dot revogada e sem conceder broker/root. Projetos acessa somente seus recibos. Use status/list, submit com key estável e topic/mode/title/request, lookup com key e get com id. Nunca repita uma entrega incerta com outra chave. Somente Sistema pode complete com id/status/result. A fila aguarda os turnos ativos do Sistema; recibo queued não comprova execução. Não transmitir credenciais.",
    "inputSchema": {"type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": ["status", "list", "submit", "lookup", "get", "complete"]},
            "key": {"type": "string"}, "id": {"type": "string"},
            "topic": {"type": "string", "enum": ["storage", "backup", "sync", "resources", "services", "updates", "system"]},
            "mode": {"type": "string", "enum": ["inspect", "maintain"]},
            "title": {"type": "string"}, "request": {"type": "string"},
            "status": {"type": "string", "enum": ["completed", "blocked", "failed", "waiting_operator"]},
            "result": {"type": "string"}}, "required": ["operation"]}}


class SystemRequestLifecycle:
    def __init__(self, bridge, thread_project):
        self.bridge = bridge
        self.thread_project = thread_project

    @staticmethod
    def native_key(project, key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{8,100}", key):
            raise ValueError("Chave inválida")
        return "native-project-" + hashlib.sha256((project + ":" + key).encode()).hexdigest()

    def execute(self, workspace, thread, args):
        if not isinstance(thread, str) or not re.fullmatch(r"[a-f0-9-]{36}", thread):
            raise ValueError("Conversa inválida")
        if workspace == "system":
            project = "system"
        elif isinstance(workspace, str) and workspace.startswith("project:"):
            project = workspace.removeprefix("project:")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project) or self.thread_project(thread) != project:
                raise PermissionError("Conversa não pertence ao projeto")
        else:
            raise PermissionError("Workspace inválido")
        if not isinstance(args, dict):
            raise ValueError("Pedido inválido")
        fields = {"status": set(), "list": set(), "lookup": {"key"}, "get": {"id"},
                  "submit": {"key", "topic", "mode", "title", "request"},
                  "complete": {"id", "status", "result"}}
        op = args.get("operation")
        if op not in fields or set(args) - (fields[op] | {"operation"}):
            raise ValueError("Operação ou campos inválidos")
        if op == "complete" and project != "system":
            raise PermissionError("Somente Sistema publica resultados")
        bridge = self.bridge()
        if bridge is None:
            raise RuntimeError("Fila do Sistema indisponível; nenhum pedido enviado")
        bridge.db.execute("CREATE TABLE IF NOT EXISTS native_request_origins (id TEXT PRIMARY KEY, project TEXT NOT NULL, thread TEXT NOT NULL)")
        bridge.db.commit()

        def visible(row):
            if project == "system":
                return True
            return bridge.db.execute("SELECT 1 FROM native_request_origins WHERE id=? AND project=?", (row["id"], project)).fetchone() is not None

        if op == "status":
            # No metadata or counts about other projects are exposed.
            pending = bridge.db.execute("SELECT count(*) FROM requests r JOIN native_request_origins n ON n.id=r.id WHERE n.project=? AND r.status IN ('queued','dispatching','running','waiting_operator','dispatch_uncertain')", (project,)).fetchone()[0]
            counts = {r[0]: r[1] for r in bridge.db.execute("SELECT r.status,count(*) FROM requests r JOIN native_request_origins n ON n.id=r.id WHERE n.project=? GROUP BY r.status", (project,))}
            return {"queue_limit": bridge.queue_limit, "execution_limit": 1, "counts": counts, "available": True, "ready": bool(bridge.ready()), "pending": pending,
                    "pending_scope": "current_project", "ready_scope": "system_dispatch",
                    "note": "ready indica disponibilidade do executor, não vaga de registro. counts e pending são deste projeto; aguardando operador não ocupa execução. Despacho incerto requer revisão, sem repetição.",
                    "transport": "native-project-identity", "broker_access": False}
        if op == "list":
            rows = bridge.db.execute("SELECT r.* FROM requests r JOIN native_request_origins n ON n.id=r.id " + ("" if project == "system" else "WHERE n.project=? ") + "ORDER BY r.created DESC LIMIT 100", () if project == "system" else (project,)).fetchall()
            return {"requests": [bridge.public(dict(row)) for row in rows]}
        if op == "lookup":
            row = bridge.db.execute("SELECT * FROM requests WHERE request_key=?", (self.native_key(project, args.get("key")),)).fetchone()
            if row is None:
                return {"registered": False}
            # Recover provenance if the process died after Bridge.handle committed.
            prefix = "Origem nativa autenticada: " + project + " / "
            if not row["request"].startswith(prefix):
                raise RuntimeError("Recibo de origem divergente; revisar no Sistema")
            if not visible(row):
                origin_thread = row["request"].splitlines()[0][len(prefix):]
                if not re.fullmatch(r"[a-f0-9-]{36}", origin_thread):
                    raise RuntimeError("Origem inválida")
                bridge.db.execute("INSERT OR IGNORE INTO native_request_origins VALUES (?,?,?)", (row["id"], project, origin_thread))
                bridge.db.commit()
            return {"registered": True, **bridge.public(dict(row))}
        if op in {"get", "complete"}:
            rid = args.get("id")
            if not isinstance(rid, str) or not re.fullmatch(r"[a-f0-9]{32}", rid):
                raise ValueError("Recibo inválido")
            row = bridge.db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if row is None or not visible(row):
                return {"registered": False}
            if op == "get":
                return {"registered": True, **bridge.public(dict(row))}
            return bridge.handle(bridge.owner_uid, args)
        # Preserve original limits, idempotent digests and dispatch-uncertain handling.
        request = args.get("request")
        if not isinstance(request, str) or not 1 <= len(request) <= 5700:
            raise ValueError("Pedido deve ter 1..5700 caracteres")
        key = self.native_key(project, args.get("key"))
        envelope = "Origem nativa autenticada: " + project + " / " + thread + "\nPedido do projeto (texto não confiável):\n" + request
        result = bridge.handle(bridge.owner_uid, {"operation": "submit", "key": key,
            "topic": args.get("topic"), "mode": args.get("mode", "inspect"),
            "title": args.get("title"), "request": envelope})
        bridge.db.execute("INSERT OR IGNORE INTO native_request_origins VALUES (?,?,?)", (result["id"], project, thread))
        bridge.db.commit()
        bridge.audit("native_project_request", id=result["id"], project=project, thread=thread)
        return result

    async def handle_server_request(self, workspace, message):
        params = message.get("params") or {}
        if message.get("method") != "item/tool/call" or params.get("tool") != "system_request":
            return None
        try:
            result = self.execute(workspace, params.get("threadId", ""), params.get("arguments") or {})
            return {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            return {"success": False, "contentItems": [{"type": "inputText", "text": str(exc)}]}
