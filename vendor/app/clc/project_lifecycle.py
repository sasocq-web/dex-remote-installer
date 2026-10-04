"""Scoped native access to the existing idempotent Projects relay."""
import json
import re
import sqlite3

PROJECT_TOOL_SPEC = {
    "type": "function", "name": "project_control", "deferLoading": False,
    "description": "Consulta e cria conversas nativas no projeto atual pela fila auditável do Dex. Use list para identificar o projeto, read com thread_id, lookup com key para reconciliar tentativa sem recibo, submit com key estável e message, result com id. submit sem thread_id cria conversa; com thread_id retoma a existente. Preserve a mesma chave e conteúdo nas repetições; dispatch_uncertain exige revisão do Sistema, nunca outra chave. Não use o CLI sasocq-project-request na conversa restrita. Projetos só acessa o próprio projeto; Sistema informa project_id. Não concede shell, broker, permissões ou modelos adicionais.",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "operation": {"type": "string", "enum": ["list", "read", "lookup", "submit", "result"]},
        "project_id": {"type": "string"}, "thread_id": {"type": "string"},
        "key": {"type": "string"}, "message": {"type": "string"}, "id": {"type": "string"}
    }, "required": ["operation"]}
}

class ProjectLifecycle:
    def __init__(self, bridge, thread_project):
        self.bridge = bridge
        self.thread_project = thread_project

    async def execute(self, workspace, thread, args):
        if not isinstance(args, dict):
            raise ValueError("Pedido inválido")
        op = args.get("operation")
        fields = {"list": set(), "read": {"thread_id"}, "lookup": {"key"},
                  "submit": {"thread_id", "key", "message"}, "result": {"id"}}
        if op not in fields or set(args) - (fields[op] | {"operation", "project_id"}):
            raise ValueError("Operação ou campos inválidos")
        if not isinstance(thread, str) or not re.fullmatch(r"[a-f0-9-]{36}", thread):
            raise ValueError("Conversa inválida")
        if workspace == "system":
            project = args.get("project_id")
        elif isinstance(workspace, str) and workspace.startswith("project:"):
            project = workspace.split(":", 1)[1]
            if self.thread_project(thread) != project:
                raise PermissionError("Conversa não pertence ao projeto")
            if args.get("project_id", project) != project:
                raise PermissionError("Acesso limitado ao projeto atual")
        else:
            raise PermissionError("Workspace inválido")
        bridge = self.bridge()
        if bridge is None or bridge.project_relay is None:
            raise RuntimeError("Ponte ainda indisponível; nenhum pedido enviado")
        relay = bridge.project_relay
        if op == "list":
            result = await relay.handle(bridge.owner_uid, {"operation": "project-list"})
            if workspace != "system" or project:
                result = {"projects": [p for p in result["projects"] if p["id"] == project]}
            return result
        if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project):
            raise ValueError("Projeto obrigatório")
        # This validates existence and excludes System using the canonical handler.
        await relay.handler("validate", {"project_id": project, "thread_id": ""})
        if op in ("lookup", "result"):
            field = "request_key" if op == "lookup" else "id"
            value = args.get("key" if op == "lookup" else "id")
            pattern = r"[A-Za-z0-9_.-]{8,100}" if op == "lookup" else r"[a-f0-9]{32}"
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ValueError("Chave ou recibo inválido")
            row = relay.db.execute("SELECT * FROM requests WHERE " + field + "=?", (value,)).fetchone()
            if row is None or json.loads(row["payload"])["project_id"] != project:
                return {"registered": False, "project_id": project}
            return {"registered": True, **relay.public(row)}
        data = {k: v for k, v in args.items() if k != "project_id"}
        data.update(operation="project-" + op, project_id=project)
        return await relay.handle(bridge.owner_uid, data)

    async def handle_server_request(self, workspace, message):
        params = message.get("params") or {}
        if message.get("method") != "item/tool/call" or params.get("tool") != "project_control":
            return None
        try:
            result = await self.execute(workspace, params.get("threadId", ""), params.get("arguments") or {})
            return {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            return {"success": False, "contentItems": [{"type": "inputText", "text": str(exc)}]}


def migrate_project_tools(database):
    if not database.is_file():
        return 0
    count = 0
    with sqlite3.connect(database, timeout=30) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"threads", "thread_dynamic_tools"} <= tables:
            return 0
        schema = json.dumps(PROJECT_TOOL_SPEC["inputSchema"], ensure_ascii=False)
        for (thread,) in db.execute("SELECT id FROM threads").fetchall():
            row = db.execute("SELECT position FROM thread_dynamic_tools WHERE thread_id=? AND name='project_control' AND namespace IS NULL", (thread,)).fetchone()
            if row:
                db.execute("UPDATE thread_dynamic_tools SET description=?,input_schema=?,defer_loading=0 WHERE thread_id=? AND name='project_control' AND namespace IS NULL", (PROJECT_TOOL_SPEC["description"], schema, thread))
            else:
                pos = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM thread_dynamic_tools WHERE thread_id=?", (thread,)).fetchone()[0]
                db.execute("INSERT INTO thread_dynamic_tools(thread_id,position,name,description,input_schema,defer_loading,namespace) VALUES(?,?,?,?,?,0,NULL)", (thread, pos, "project_control", PROJECT_TOOL_SPEC["description"], schema))
                count += 1
    return count

