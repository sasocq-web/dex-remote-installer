"""Audited build tool without granting host capabilities to project terminals."""
from __future__ import annotations
import asyncio, base64, json, re
from pathlib import Path
from .control_plane import request
HELPER=str(Path(__file__).resolve().parents[1]/"system/build_conversation.py")
BUILD_TOOL_SPEC={"type":"function","name":"build_control","deferLoading":False,
"description":"Constrói e testa imagens rootless no host SASOCQ pela conversa de Projetos. Use em vez do CLI sasocq-build no terminal restrito. operation: build, status, run, stop, export. name identifica a tarefa dentro do projeto (até 20 caracteres). build recebe context relativo ao projeto e tag sasocq/app:versao, file Dockerfile e target opcionais. Consulte status até phase=complete; export copia o .tar para destination dentro do projeto, para scp à VM e docker load/compose up --no-build. run executa argv somente em imagem concluída do projeto. Prazo limitado, sem autostart; tarefas podem concluir entre turnos. Não usa VM para desenvolvimento nem concede root, broker ou D-Bus ao projeto.",
"inputSchema":{"type":"object","additionalProperties":False,"properties":{
"operation":{"type":"string","enum":["build","status","run","stop","export"]},
"name":{"type":"string","pattern":"^[a-z][a-z0-9-]{0,19}$"},
"context":{"type":"string"},"file":{"type":"string"},"tag":{"type":"string"},"target":{"type":"string"},
"minutes":{"type":"integer","minimum":1,"maximum":120},"image":{"type":"string"},
"argv":{"type":"array","items":{"type":"string"},"maxItems":128},"destination":{"type":"string"}},"required":["operation"]}}
def migrate_build_tools(database):
    import sqlite3
    if not database.is_file(): return 0
    with sqlite3.connect(database,timeout=30) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"threads","thread_dynamic_tools"}<=tables:return 0
        count=0
        for (thread,) in db.execute("SELECT id FROM threads").fetchall():
            row=db.execute("SELECT position FROM thread_dynamic_tools WHERE thread_id=? AND name=? AND namespace IS NULL",(thread,"build_control")).fetchone()
            schema=json.dumps(BUILD_TOOL_SPEC["inputSchema"],ensure_ascii=False)
            if row:
                db.execute("UPDATE thread_dynamic_tools SET description=?,input_schema=?,defer_loading=0 WHERE thread_id=? AND name=? AND namespace IS NULL",(BUILD_TOOL_SPEC["description"],schema,thread,"build_control"))
            else:
                pos=db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM thread_dynamic_tools WHERE thread_id=?",(thread,)).fetchone()[0]
                db.execute("INSERT INTO thread_dynamic_tools(thread_id,position,name,description,input_schema,defer_loading,namespace) VALUES(?,?,?,?,?,0,NULL)",(thread,pos,"build_control",BUILD_TOOL_SPEC["description"],schema));count+=1
        return count
class BuildLifecycle:
    def __init__(self,roots_for_workspace,broker=request):
        self.roots_for_workspace,self.broker=roots_for_workspace,broker
    async def execute(self,workspace,thread,args):
        if not isinstance(workspace,str) or not re.fullmatch(r"project:[A-Za-z0-9_.:-]{1,200}",workspace):raise ValueError("Build requer conversa de Projetos; Sistema usa broker")
        if not isinstance(thread,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",thread):raise ValueError("Conversa inválida")
        if not isinstance(args,dict) or set(args)-set(BUILD_TOOL_SPEC["inputSchema"]["properties"]):raise ValueError("Pedido inválido")
        roots=self.roots_for_workspace(workspace)
        if not roots:raise ValueError("Projeto não encontrado")
        payload=base64.b64encode(json.dumps(dict(roots=roots,thread=thread,args=args)).encode()).decode()
        if len(payload)>7500:raise ValueError("Pedido excede o limite de transporte")
        params={"operation":"exec","argv":["runuser","-u","codex-worker","--","/usr/bin/python3",HELPER,payload],"timeout":110}
        task=asyncio.create_task(asyncio.to_thread(self.broker,"host-admin",params,timeout=120))
        try:response=await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        raw=response.get("result",{}).get("output",{})
        if not response.get("ok") or not isinstance(raw,dict) or raw.get("returncode",0):raise RuntimeError(str(response.get("error") or raw))
        result=json.loads(raw.get("output","{}"))
        if result.get("error"):raise ValueError(result["error"])
        return result
    async def handle_server_request(self,workspace,message):
        p=message.get("params") or {}
        if message.get("method")!="item/tool/call" or p.get("tool")!="build_control":return None
        try:
            result=await self.execute(workspace,p.get("threadId",""),p.get("arguments") or {})
            return {"success":not bool(result.get("returncode")),"contentItems":[{"type":"inputText","text":json.dumps(result,ensure_ascii=False)}]}
        except Exception as exc:
            return {"success":False,"contentItems":[{"type":"inputText","text":str(exc)}]}
