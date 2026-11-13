"""Resume unfinished Dex requests using the original execution context."""
import asyncio
import fcntl
import logging
import re
import sqlite3
import time
from pathlib import Path

LOG = logging.getLogger(__name__)
MARK = re.compile(r"(?im)^\s*Retomada:\s*(agora|onedrive|temporária|operador|concluída)\s*[.!]?\s*$")
MESSAGE = ("Retomada automática da solicitação original. Confira o último resultado e os recibos "
           "das operações em curso. Prossiga com as entregas autorizadas ainda pendentes. "
           "Se a dependência persistir, informe a condição. Preserve pausas, cancelamentos e aprovações. "
           "Não repita efeitos já aceitos. Se tudo estiver concluído, encerre.")

class Continuations:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS work(thread TEXT PRIMARY KEY, turn TEXT NOT NULL, mode TEXT NOT NULL, due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)")
    def db(self):
        return sqlite3.connect(self.path)
    def schedule(self, thread, turn, mode, delay=0):
        if not thread or not turn: return
        with self.db() as db:
            old = db.execute("SELECT turn,attempts FROM work WHERE thread=?", (thread,)).fetchone()
            if old and old[0] == turn: return
            attempts = old[1] if old else 0
            if mode == "temporária":
                delay = max(delay,min(900,60 * 2 ** min(attempts,4)))
            db.execute("INSERT OR REPLACE INTO work VALUES(?,?,?,?,?)",
                       (thread,turn,mode,time.time()+delay,attempts))
    def forget(self, thread):
        with self.db() as db:
            db.execute("DELETE FROM work WHERE thread=?", (thread,))
    def observe(self,event):
        n = event.get("notification") or {}
        method = n.get("method")
        if method not in {"turn/completed","thread/archived"}: return
        p = n.get("params") or {}
        turn = p.get("turn") or {}
        thread = p.get("threadId") or turn.get("threadId")
        if not thread: return
        if method == "thread/archived" or turn.get("status") != "completed":
            self.forget(thread)
            return
        msg = next((i.get("text","") for i in reversed(turn.get("items") or [])
                    if i.get("type")=="agentMessage" and i.get("phase") in (None,"final_answer")), "")
        markers = MARK.findall(msg.replace("*",""))
        if not markers:
            # item/completed may supply the final text separately; preserve its decision.
            if not msg: return
            self.forget(thread)
            return
        self.finish(thread,turn.get("id"),markers[-1].lower())
    def finish(self,thread,turn,mode):
        if mode in {"operador","concluída"}:
            self.forget(thread)
        else:
            self.schedule(thread,turn,mode,15 if mode=="agora" else 60)
    def later(self,thread,turn,delay,attempts):
        with self.db() as db:
            db.execute("UPDATE work SET due=?,attempts=? WHERE thread=? AND turn=?",
                       (time.time()+delay,attempts,thread,turn))
    def due(self):
        with self.db() as db:
            return db.execute("SELECT thread,turn,mode,attempts FROM work WHERE due<=? ORDER BY due LIMIT 4",(time.time(),)).fetchall()

def cloud_busy():
    path = Path.home()/".local/share/codex-linux-control/onedrive-account.lock"
    try:
        with path.open("rb") as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except FileNotFoundError: return False
    except OSError: return True
    return False

async def monitor(work, context):
    from . import main
    while True:
        await asyncio.sleep(10)
        for thread,turn,mode,attempt in work.due():
            try:
                if main._rollout_gate_closed() or any(t==thread for _,t in main._active_turns):
                    work.later(thread,turn,20,attempt)
                    continue
                if mode=="onedrive" and cloud_busy():
                    work.later(thread,turn,20,attempt)
                    continue
                ctx = context(thread,turn)
                if not ctx or ctx.get("collaboration_mode")=="plan":
                    work.later(thread,turn,300,attempt)
                    continue
                project=main.projects.get(ctx["project_id"])
                if not project or main._project_id_for_thread(thread)!=project.id:
                    work.later(thread,turn,300,attempt)
                    continue
                target=main._bridge_for_project(project)
                history=await main._rpc("thread/turns/list",{"threadId":thread,"limit":1,"itemsView":"notLoaded","sortDirection":"desc"},target=target)
                last=next(iter(history.get("data") or []),{})
                if last.get("id") != turn:
                    if last.get("id"):
                        with work.db() as db:
                            db.execute("DELETE FROM work WHERE thread=? AND turn=?",(thread,turn))
                    else: work.later(thread,turn,60,attempt)
                    continue
                if last.get("status")!="completed" or any(e.get("status") in {"queued","running","steering"} for e in main.operations.queue(thread)):
                    work.later(thread,turn,30,attempt)
                    continue
                # Persist a lease before dispatch. Always reconcile the newest native turn on retry.
                work.later(thread,turn,300,attempt+1)
                await main._run_claimed_queue_item(thread,{**ctx,"message":MESSAGE,"references":[]})
                LOG.info("Retomada aceita para %s",thread)
            except Exception as exc:
                work.later(thread,turn,min(900,30*2**min(attempt,5)),attempt+1)
                LOG.warning("Retomada %s aguarda nova verificação (%s)",thread,type(exc).__name__)
