"""Conversation-scoped Android operations; privileged work stays in the broker."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

from .control_plane import request

LOG = logging.getLogger(__name__)
TARGET = "sasocq-android-waydroid.target"
HOST_ANDROID = str(Path(__file__).resolve().parents[1] / "system/android-host-adb.py")
SOCKET = Path("/run/user/1000/sasocq-android-vnc.sock")
UNITS = [TARGET, "waydroid-container.service", *[
    "sasocq-android-" + name for name in (
        "network.service", "session.service", "wayland.service", "waydroid-gate.service",
        "ui.service", "binder.service", "keepalive.timer", "keepalive.service",
    )
]]
OPERATIONS = ["status", "start", "stop", "open", "play", "ui", "apps", "is-installed", "back", "home", "tap", "type"]
ANDROID_TOOL_SPEC = {
    "type": "function", "name": "android_control", "deferLoading": False,
    "description": "Usa o Android SASOCQ na conversa atual. start/open/play/ui ligam o Waydroid sob demanda pelo broker e aguardam o boot. A sessão é exclusiva de uma conversa; stop libera o uso. Ao terminar o turno, desliga quando não há visor aberto. Use play para instalações pela Google Play; não instala APK externo. status não liga o Android. Não peça ao usuário para abrir outra conversa no Sistema para ativá-lo.",
    "inputSchema": {"type": "object", "additionalProperties": False,
        "properties": {"operation": {"type": "string", "enum": OPERATIONS},
            "package": {"type": "string"}, "x": {"type": "integer", "minimum": 0, "maximum": 8192},
            "y": {"type": "integer", "minimum": 0, "maximum": 8192},
            "text": {"type": "string", "maxLength": 2000}}, "required": ["operation"]},
}

def migrate_android_tools(database: Path) -> int:
    if not database.is_file():
        return 0
    count = 0
    with sqlite3.connect(database, timeout=30) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"threads", "thread_dynamic_tools"} <= tables:
            return 0
        for (thread,) in db.execute("SELECT id FROM threads").fetchall():
            row = db.execute("SELECT position FROM thread_dynamic_tools WHERE thread_id=? AND name=?", (thread, ANDROID_TOOL_SPEC["name"])).fetchone()
            schema = json.dumps(ANDROID_TOOL_SPEC["inputSchema"], ensure_ascii=False)
            if row:
                db.execute("UPDATE thread_dynamic_tools SET description=?,input_schema=?,defer_loading=0,namespace=NULL WHERE thread_id=? AND name=?", (ANDROID_TOOL_SPEC["description"], schema, thread, ANDROID_TOOL_SPEC["name"]))
            else:
                position = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM thread_dynamic_tools WHERE thread_id=?", (thread,)).fetchone()[0]
                db.execute("INSERT INTO thread_dynamic_tools (thread_id,position,name,description,input_schema,defer_loading,namespace) VALUES (?,?,?,?,?,0,NULL)", (thread, position, ANDROID_TOOL_SPEC["name"], ANDROID_TOOL_SPEC["description"], schema))
                count += 1
    return count

class AndroidLifecycle:
    def __init__(self, marker: Path, broker=request):
        self.marker = marker
        self.broker = broker
        self.lock = asyncio.Lock()
        self.owner = None
        self.viewers = set()
        self.turn_active = False
        self.turn_id = ""
        self.last_used = 0.0
        self.cleanup_failed = marker.exists()

    async def _call(self, action, params):
        task = asyncio.create_task(asyncio.to_thread(self.broker, action, params, timeout=120))
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancelled HTTP/tool request must not leave a broker start still
            # running concurrently with rollback in another thread.
            await task
            raise
        result = response.get("result", {})
        if not response.get("ok") or result.get("returncode", 0):
            raise RuntimeError(str(response.get("error") or result))
        value = result.get("output", {})
        if isinstance(value, dict) and value.get("returncode", 0):
            raise RuntimeError(str(value.get("output")))
        return value.get("output", "") if isinstance(value, dict) else value

    async def _service(self, operation):
        return await self._call("service", {"unit": TARGET, "operation": operation, **({"confirm": True} if operation == "stop" else {})})

    async def _host(self, argv):
        return await self._call("host-admin", {"operation": "exec", "argv": [
            "runuser", "-u", "codex-worker", "--", *argv], "timeout": 100})

    async def _adb_service(self, operation):
        return await self._host(["env", "XDG_RUNTIME_DIR=/run/user/1001",
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus",
            "systemctl", "--user", operation, "sasocq-android-adb-host.service"])

    def _key(self, workspace, thread):
        if not (workspace == "system" or re.fullmatch(r"project:[A-Za-z0-9_.:-]{1,200}", workspace)) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", thread):
            raise ValueError("Conversa Android inválida")
        return workspace, thread

    def _status(self, key):
        mine = self.owner == key
        return {"owned": mine, "busy": self.owner is not None and not mine,
            "running": self.owner is not None, "cleanup_pending": self.cleanup_failed,
            "viewer_url": "/remote-viewer.html?" + urlencode({"target": "android", "thread_id": key[1]}) if mine else ""}

    async def _acquire(self, key, turn_active, turn_id=""):
        if self.owner and self.owner != key:
            raise ValueError("Android em uso por outra conversa. Aguarde a liberação.")
        if self.cleanup_failed:
            raise RuntimeError("Encerramento Android pendente; aguarde a recuperação auditável.")
        if self.owner is None:
            service = await self._service("status")
            if any("ActiveState=" + state in service for state in ("active", "activating", "deactivating", "reloading")):
                raise ValueError("Android já ativo fora desta reserva; encerre o uso anterior antes de assumir a sessão.")
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.touch(mode=0o600)
            self.owner = key
            try:
                await self._adb_service("start")
                await self._service("start")
                await self._host(["timeout", "85", HOST_ANDROID, "status"])
                if not SOCKET.is_socket():
                    raise RuntimeError("O visor Android não ficou disponível")
            except BaseException:
                await self._stop()
                raise
        self.turn_active = self.turn_active or turn_active
        if turn_id:
            self.turn_id = turn_id
        self.last_used = time.monotonic()

    async def _stop(self):
        # Keep the ownership marker until EVERY stop succeeds, so a crash or
        # failure is retried after restart rather than leaving Android running.
        self.cleanup_failed = True
        errors = []
        for action in (
            lambda: self._service("stop"),
            self._wait_stopped,
            lambda: self._adb_service("stop"),
        ):
            try:
                await action()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("Falha ao encerrar Android: " + "; ".join(errors))
        self.marker.unlink(missing_ok=True)
        self.owner = None
        self.viewers.clear()
        self.turn_active = False
        self.turn_id = ""
        self.cleanup_failed = False

    async def _wait_stopped(self):
        # Stopping a target can return before all PartOf units finish stopping.
        for _ in range(50):
            state = await self._call("host-admin", {"operation": "exec", "argv": [
                "systemctl", "show", *UNITS, "--property=Id,ActiveState", "--no-pager"
            ], "timeout": 10})
            if not any("ActiveState=" + value in state for value in ("active", "activating", "deactivating", "reloading")):
                return
            await asyncio.sleep(2)
        raise RuntimeError("Unidades auxiliares Android ainda não encerraram")

    async def recover(self):
        async with self.lock:
            if self.marker.exists():
                await self._stop()

    def command(self, args):
        op = args.get("operation")
        if op not in OPERATIONS or set(args) - {"operation", "package", "x", "y", "text"}:
            raise ValueError("Operação Android inválida")
        argv = ["timeout", "85", HOST_ANDROID, op]
        if op in {"open", "play", "is-installed"}:
            package = args.get("package", "")
            if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package):
                raise ValueError("Pacote Android inválido")
            argv.append(package)
        if op == "tap":
            for name in ("x", "y"):
                value = args.get(name)
                if type(value) is not int or not 0 <= value <= 8192:
                    raise ValueError("Coordenadas inválidas")
                argv.append(str(value))
        if op == "type":
            value = args.get("text")
            if not isinstance(value, str) or not value or len(value) > 2000 or any(c in value for c in "\x00\n\r"):
                raise ValueError("Texto inválido")
            # Host wrapper quotes once at the ADB shell boundary; no SSH shell.
            argv.append(value)
        return argv

    async def execute(self, workspace, thread, args, turn_active=True, turn_id=""):
        key = self._key(workspace, thread)
        argv = self.command(args)  # Validate before starting any runtime.
        async with self.lock:
            op = args["operation"]
            if op == "status":
                service = await self._service("status")
                return {**self._status(key), "running": "ActiveState=active" in service, "service": service}
            if op == "stop":
                if self.owner and self.owner != key:
                    raise ValueError("Outra conversa está usando o Android")
                if self.viewers:
                    raise ValueError("Feche o visor Android antes de liberar a sessão")
                if self.owner:
                    await self._stop()
                return self._status(key)
            await self._acquire(key, turn_active, turn_id)
            try:
                output = "Android pronto" if op == "start" else await self._host(argv)
                return {**self._status(key), "output": output}
            except BaseException:
                if not self.viewers:
                    await self._stop()
                raise

    async def handle_server_request(self, workspace, message):
        params = message.get("params") or {}
        if message.get("method") != "item/tool/call" or params.get("tool") != "android_control":
            return None
        try:
            value = await self.execute(workspace, str(params.get("threadId") or ""), params.get("arguments") or {}, turn_id=str(params.get("turnId") or ""))
            return {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(value, ensure_ascii=False)}]}
        except Exception as exc:
            return {"success": False, "contentItems": [{"type": "inputText", "text": str(exc)}]}

    async def viewer(self, key, socket, connected):
        async with self.lock:
            if self.owner != key or self.cleanup_failed:
                raise ValueError("Esta conversa não possui a sessão Android")
            if connected:
                self.viewers.add(socket)
            else:
                self.viewers.discard(socket)
                if not self.viewers and not self.turn_active:
                    await self._stop()

    async def observe(self, event):
        if event.get("kind") != "notification":
            return
        note = event.get("notification") or {}
        params = note.get("params") or {}
        key = (event.get("workspace", "system"), params.get("threadId") or (params.get("turn") or {}).get("threadId"))
        async with self.lock:
            if key != self.owner:
                return
            event_turn = str((params.get("turn") or {}).get("id") or params.get("turnId") or "")
            if note.get("method") == "turn/started":
                if not self.turn_active:
                    self.turn_id = event_turn
                self.turn_active = True
            elif note.get("method") == "turn/completed":
                if self.turn_id and event_turn and self.turn_id != event_turn:
                    return  # A queued completion from an older turn cannot stop a newer lease.
                self.turn_active = False
                if not self.viewers:
                    await self._stop()

    async def sweep(self, active_keys):
        async with self.lock:
            if self.cleanup_failed:
                await self._stop()
            elif self.owner and not self.viewers and self.owner not in active_keys and time.monotonic() - self.last_used > 60:
                await self._stop()

    async def worker(self, events, active_keys):
        subscription = await events.subscribe()
        try:
            try:
                await self.recover()
            except Exception:
                LOG.exception("Recuperação Android pendente; Dex continua disponível")
            while True:
                try:
                    event = await asyncio.wait_for(subscription.get(), 5)
                    await self.observe(event)
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    LOG.exception("Falha ao liberar Android após evento")
                try:
                    await self.sweep(active_keys())
                except Exception:
                    LOG.exception("Falha na coleta Android; será tentada novamente")
        finally:
            await events.unsubscribe(subscription)
