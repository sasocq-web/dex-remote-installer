"""Native, audited DNS repair without credentials or host privileges in Projects."""
from __future__ import annotations
import base64
import json
from pathlib import Path
import re
import sqlite3
from .control_plane import async_request

HELPER = str(Path(__file__).resolve().parents[1] / "system/publication_dns.py")
PUBLICATION_TOOL_SPEC = {
    "type": "function", "name": "publication_control", "deferLoading": False,
    "description": "Diagnostica e repara DNS de sites SASOCQ pela conversa. Use status com hostname e path opcional para consultar o registro real Cloudflare e comparar origem da VM com endereço público. repair cria ou corrige um CNAME proxied somente para o túnel residencial fixo, após verificar a origem, com backup e auditoria. Usa a credencial preservada no Sistema sem expô-la ao projeto; não requer login pelo navegador enquanto válida. Não altera proxy, autenticação, serviços, outros tipos de DNS nem hostnames reservados dex/server. Corrija app/proxy por ssh sasocq-server. Após repair, valide o fluxo público real no Playwright; DNS/HTTP não prova conclusão.",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "operation": {"type": "string", "enum": ["status", "repair"]},
        "hostname": {"type": "string", "description": "Hostname exato do site em sasocq.com"},
        "path": {"type": "string", "description": "Caminho GET para verificar, padrão /; sem sessão ou redirecionamentos"}
    }, "required": ["operation", "hostname"]}
}

def migrate_publication_tools(database):
    if not database.is_file():
        return 0
    count = 0
    with sqlite3.connect(database, timeout=30) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"threads", "thread_dynamic_tools"} <= tables:
            return 0
        schema = json.dumps(PUBLICATION_TOOL_SPEC["inputSchema"], ensure_ascii=False)
        for (thread,) in db.execute("SELECT id FROM threads").fetchall():
            row = db.execute("SELECT position FROM thread_dynamic_tools WHERE thread_id=? AND name='publication_control' AND namespace IS NULL", (thread,)).fetchone()
            if row:
                db.execute("UPDATE thread_dynamic_tools SET description=?,input_schema=?,defer_loading=0 WHERE thread_id=? AND name='publication_control' AND namespace IS NULL", (PUBLICATION_TOOL_SPEC["description"], schema, thread))
            else:
                pos = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM thread_dynamic_tools WHERE thread_id=?", (thread,)).fetchone()[0]
                db.execute("INSERT INTO thread_dynamic_tools(thread_id,position,name,description,input_schema,defer_loading,namespace) VALUES(?,?,?,?,?,0,NULL)", (thread, pos, "publication_control", PUBLICATION_TOOL_SPEC["description"], schema))
                count += 1
    return count

class PublicationLifecycle:
    def __init__(self, roots_for_workspace, broker=async_request):
        self.roots_for_workspace = roots_for_workspace
        self.broker = broker

    async def execute(self, workspace, thread, args):
        if workspace != "system":
            if not isinstance(workspace, str) or not re.fullmatch(r"project:[A-Za-z0-9_.:-]{1,200}", workspace) or not self.roots_for_workspace(workspace):
                raise ValueError("Projeto não encontrado")
        if not isinstance(thread, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", thread):
            raise ValueError("Conversa inválida")
        if not isinstance(args, dict) or set(args) - set(PUBLICATION_TOOL_SPEC["inputSchema"]["properties"]):
            raise ValueError("Pedido inválido")
        payload = base64.b64encode(json.dumps({"workspace": workspace, "thread": thread, "args": args}).encode()).decode()
        if len(payload) > 7500:
            raise ValueError("Pedido excede o limite")
        response = await self.broker("host-admin", {
            "operation": "exec", "argv": ["runuser", "-u", "codex", "--", "/usr/bin/python3", HELPER, payload], "timeout": 110
        }, timeout=120)
        raw = response.get("result", {}).get("output", {})
        if not response.get("ok") or not isinstance(raw, dict):
            raise RuntimeError("Falha no transporte administrativo da publicação")
        try:
            result = json.loads(raw.get("output", "{}"))
        except (ValueError, TypeError):
            raise RuntimeError("Resposta de publicação inválida") from None
        if raw.get("returncode") or result.get("error"):
            raise RuntimeError(result.get("error") or "Publicação falhou; consulte status antes de repetir")
        return result

    async def handle_server_request(self, workspace, message):
        params = message.get("params") or {}
        if message.get("method") != "item/tool/call" or params.get("tool") != "publication_control":
            return None
        try:
            result = await self.execute(workspace, params.get("threadId", ""), params.get("arguments") or {})
            return {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            return {"success": False, "contentItems": [{"type": "inputText", "text": str(exc)}]}
