"""Conversation-scoped Android operations; privileged work stays in the broker."""
from __future__ import annotations

import asyncio
import json
import logging
import math
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
# Poll admission budget, NOT a hard broker/cleanup deadline. An in-flight call
# is still awaited/drained; only explicit complete unit state can prove OFF.
STOP_POLL_BUDGET = 60.

def _all_android_units_stopped(output):
    if not isinstance(output, str):raise ValueError('android_units_state_incomplete')
    records=[];current={}
    for line in output.splitlines()+['']:
        if not line.strip():
            if current:records.append(current);current={}
            continue
        key,sep,value=line.partition('=')
        if not sep or key not in {'Id','ActiveState'} or key in current:raise ValueError('android_units_state_incomplete')
        current[key]=value
    if (len(records)!=len(UNITS) or any(set(v)!={'Id','ActiveState'} for v in records)
            or {v['Id'] for v in records}!=set(UNITS)):
        raise ValueError('android_units_state_incomplete')
    # Preserve native policy for systemd non-running states, now for ALL IDs.
    return all(v['ActiveState'] in {'inactive','failed'} for v in records)

OPERATIONS = ["status", "start", "stop", "open", "play", "ui", "apps", "is-installed", "back", "home", "tap", "type", "screenshot", "swipe", "long_press", "orientation", "pinch", "observe_nc"]
ANDROID_TOOL_SPEC = {
    "type": "function", "name": "android_control", "deferLoading": False,
    "description": "Usa o Android SASOCQ na conversa atual. start/open/play/ui ligam o Waydroid sob demanda pelo broker e aguardam o boot. A sessão é exclusiva de uma conversa; stop libera o uso. Ao terminar o turno, desliga quando não há visor aberto. Use play para instalações pela Google Play; não instala APK externo. screenshot retorna uma imagem pela ferramenta para validar visualmente e orientar tap. Para testes pelo Codex, use screenshot; viewer_url é destinado ao operador autenticado, não à identidade interna Playwright. swipe e long_press usam coordenadas da captura e duration_ms (100–5000); orientation aceita portrait/landscape/auto e muda somente a rotação lógica Android. pinch injeta dois ponteiros simultâneos em torno de x/y, com start_radius/end_radius em pixels; reduzir raio aproxima dedos, ampliar afasta. O próximo uso começa em auto. status não liga o Android. observe_nc aceita somente event_id/dependency_id registrados e coleta a sessão NC no próprio Android já reservado pelo mesmo turno; não liga o ambiente, não autentica e não comprova entrega funcional. Não peça ao usuário para abrir outra conversa no Sistema para ativá-lo.",
    "inputSchema": {"type": "object", "additionalProperties": False,
        "properties": {"operation": {"type": "string", "enum": OPERATIONS},
            "event_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "dependency_id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,100}$"}, "package": {"type": "string"}, "x": {"type": "integer", "minimum": 0, "maximum": 8192},
            "y": {"type": "integer", "minimum": 0, "maximum": 8192},
            "start_radius": {"type": "integer", "minimum": 1, "maximum": 4096}, "end_radius": {"type": "integer", "minimum": 1, "maximum": 4096}, "x2": {"type": "integer", "minimum": 0, "maximum": 8192}, "y2": {"type": "integer", "minimum": 0, "maximum": 8192}, "duration_ms": {"type": "integer", "minimum": 100, "maximum": 5000}, "orientation": {"type": "string", "enum": ["portrait", "landscape", "auto"]}, "text": {"type": "string", "maxLength": 2000}}, "required": ["operation"]},
}

ANDROID_TOOL_SPEC['inputSchema']['properties']['purpose'] = {
    'type':'string','pattern':'^[a-z][a-z0-9_-]{0,63}$',
    'description':'Finalidade estável do emulador isolado, por exemplo teste-pwa. Use o mesmo nome para manter seus dados neste projeto.'}
ANDROID_TOOL_SPEC['inputSchema']['properties']['operation']['enum'] = OPERATIONS + ['capabilities','acquire','yield']
ANDROID_TOOL_SPEC['description'] += ' capabilities com purpose consulta recursos do emulador e pode iniciar a instância; não comprova compartilhamento disponível.'
ANDROID_TOOL_SPEC['description'] += ' Para Android Emulator isolado por finalidade, informe purpose em todas as operações; até duas instâncias, condicionadas aos recursos. Sem purpose mantém o Waydroid e as sessões legadas. Não migre autenticações existentes automaticamente. O emulador atual usa Google APIs sem Play Store; play retorna indisponível. Pinch e observe_nc continuam exclusivos do legado. Para web comum, prefira Playwright em modo celular.'

ANDROID_TOOL_SPEC['inputSchema']['properties']['mode'] = {
    'type':'string','enum':['exclusive','coordinated','auto','isolated-displays'],
    'description':'Com purpose, exclusive mantém instância exclusiva (padrão). coordinated/auto compartilham perfil dedicado da mesma finalidade e workspace por alternância explícita: start/acquire com package, status informa fila, yield libera tela, stop deixa o grupo. Repetir mode e purpose em todas as operações. isolated-displays ainda indisponível.'}
ANDROID_TOOL_SPEC['description'] += ' No modo coordinated/auto, somente screen_owned=true autoriza captura ou entrada. Feche o visor antes de yield/stop. acquire aguarda pela posição da fila sem roubar tela; não repetir ações incertas. Um pacote por conversa, sem migração de autenticação do perfil exclusivo. auto usa alternância coordenada; telas simultâneas ainda indisponíveis.'

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
        self.background_owner = None
        self.background_admission = lambda: False
        self.viewers = set()
        self.turn_active = False
        self.turn_id = ""
        self.last_used = 0.0
        self.cleanup_failed = marker.exists()
        self.native_probe_helper = HOST_ANDROID
        self.feedback_worker = None
        self.capture_proofs = {}

    async def collect_feedback_probe(self, worker, arguments, workspace, thread, turn_id):
        # Internal trusted channel only. No public tool operation accepts probe
        # facts or receipts; profile semantics still need producer qualification.
        from .feedback_native_binding import collect
        return await collect(self, worker, arguments, workspace, thread, turn_id)

    async def _call(self, action, params, *, _on_host_completion=None):
        # Private Python-only completion hook, never serialized or tool-visible.
        if _on_host_completion is not None and (action != 'host-admin' or
                params.get('operation') != 'exec' or not callable(_on_host_completion)):
            raise ValueError('private_completion_callback_scope_invalid')
        task = asyncio.create_task(asyncio.to_thread(self.broker, action, params, timeout=120))
        cancelled = False
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            # Preserve the SAME IPC task across repeated caller cancellations.
            while True:
                try:
                    response = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise  # No final response; never acknowledge completion.
                except Exception:
                    raise asyncio.CancelledError from None  # Still unknown IPC.
        if _on_host_completion is not None:
            # A concrete ordinary process exit is proof of
            # completion of THIS call. Bare errors and timeout124 remain unknown.
            final = False
            if isinstance(response, dict) and type(response.get('ok')) is bool:
                result = response.get('result')
                if isinstance(result, dict):
                    output = result.get('output')
                    codes = [item['returncode'] for item in (result, output)
                             if isinstance(item, dict) and 'returncode' in item]
                    # Only a concrete ordinary process exit. A bare okFalse/error
                    # may be a handler exception after dispatch, not a refusal.
                    final = bool(codes) and all(type(code) is int and 0 <= code < 124
                                                for code in codes)
                    final = final and len(set(codes)) == 1
                for item in (response, result, result.get('output') if isinstance(result, dict) else None):
                    if isinstance(item, dict) and (item.get('pending') is True or item.get('state') in {'queued','pending','running','timeout','timed_out','interrupted'} or item.get('timed_out') is True or item.get('execution_uncertain') is True):
                        final = False
            if final:
                # Synchronous event-loop closure. A persistence error propagates
                # fail-closed; no output/params/secrets are supplied to the hook.
                value = _on_host_completion()
                if value is not None:
                    if hasattr(value, 'close'):
                        value.close()
                    raise ValueError('private_completion_callback_must_be_sync')
        if cancelled:
            raise asyncio.CancelledError
        result = response.get("result", {})
        if not response.get("ok") or result.get("returncode", 0):
            raise RuntimeError(str(response.get("error") or result))
        value = result.get("output", {})
        if isinstance(value, dict) and value.get("returncode", 0):
            raise RuntimeError(str(value.get("output")))
        return value.get("output", "") if isinstance(value, dict) else value

    async def _service(self, operation):
        return await self._call("service", {"unit": TARGET, "operation": operation, **({"confirm": True} if operation == "stop" else {})})

    async def _host(self, argv, *, _on_host_completion=None):
        params = {"operation": "exec", "argv": [
            "runuser", "-u", "codex-worker", "--", *argv], "timeout": 100}
        if _on_host_completion is None:
            return await self._call("host-admin", params)  # Old callers unchanged.
        return await self._call("host-admin", params, _on_host_completion=_on_host_completion)

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
        from .android_jobs import _scheduled_boot_check
        _scheduled_boot_check(self)
        if self.owner and self.owner != key:
            raise ValueError("Android em uso por outra conversa. Aguarde a liberação.")
        if self.cleanup_failed:
            raise RuntimeError("Encerramento Android pendente; aguarde a recuperação auditável.")
        if self.owner is None:
            service = await self._service("status")
            if any("ActiveState=" + state in service for state in ("active", "activating", "deactivating", "reloading")):
                raise ValueError("Android já ativo fora desta reserva; encerre o uso anterior antes de assumir a sessão.")
            _scheduled_boot_check(self)
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.touch(mode=0o600)
            self.owner = key
            self.capture_proofs = {}
            try:
                _scheduled_boot_check(self)
                await self._adb_service("start")
                _scheduled_boot_check(self)
                await self._service("start")
                _scheduled_boot_check(self)
                await self._host(["timeout", "85", HOST_ANDROID, "status"])
                _scheduled_boot_check(self)
                await self._host(["timeout", "85", HOST_ANDROID, "orientation", "auto"])
                _scheduled_boot_check(self)
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
        # Only the authenticated broker fixed-stop terminal proof covers all
        # ten system units AND worker ADB. Unknown/legacy replies use old checks.
        fixed_verified = False
        try:
            proof = await self._service("stop")
            fixed_verified = proof == "Android all10+ADB terminal and empty"
        except Exception as exc:
            errors.append(str(exc))
        for action in (() if fixed_verified else (
            self._wait_stopped,
            lambda: self._adb_service("stop"),
        )):
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
        self.capture_proofs = {}

    async def _wait_stopped(self):
        # Service stop is not proof of all auxiliary units. Admission of polls
        # uses elapsed monotonic time, never 50*(command+queue+sleep). Do NOT
        # wrap this cleanup in a cancelling timeout or detach its IPC task.
        last = time.monotonic()
        if type(last) not in (int, float) or not math.isfinite(last) or last < 0:
            raise RuntimeError('android_cleanup_clock_invalid')
        deadline = last + STOP_POLL_BUDGET
        while True:
            now = time.monotonic()
            if type(now) not in (int, float) or not math.isfinite(now) or now < last:
                raise RuntimeError('android_cleanup_clock_invalid')
            last = now
            if now >= deadline:break
            state = await self._call("host-admin", {"operation": "exec", "argv": [
                "systemctl", "show", *UNITS, "--property=Id,ActiveState", "--no-pager"
            ], "timeout": 10})
            now = time.monotonic()
            if type(now) not in (int, float) or not math.isfinite(now) or now < last:
                raise RuntimeError('android_cleanup_clock_invalid')
            last = now
            if _all_android_units_stopped(state):
                return  # Late OFF is real OFF, not a claim about elapsed time.
            remaining = deadline - now
            if remaining <= 0:
                break
            await asyncio.sleep(min(2., remaining))
        raise RuntimeError("Unidades auxiliares Android sem confirmação integral de encerramento")

    async def recover(self):
        async with self.lock:
            if self.marker.exists():
                await self._stop()

    def command(self, args):
        op = args.get("operation")
        if op not in OPERATIONS or set(args) - {"operation", "package", "x", "y", "x2", "y2", "duration_ms", "orientation", "text", "start_radius", "end_radius", "event_id", "dependency_id"}:
            raise ValueError("Operação Android inválida")
        fields = {"observe_nc": {"event_id", "dependency_id"}, "open": {"package"}, "play": {"package"}, "is-installed": {"package"},
            "tap": {"x", "y"}, "swipe": {"x", "y", "x2", "y2", "duration_ms"},
            "long_press": {"x", "y", "duration_ms"}, "pinch": {"x", "y", "start_radius", "end_radius", "duration_ms"}, "orientation": {"orientation"}, "type": {"text"}}
        if set(args) - ({"operation"} | fields.get(op, set())):
            raise ValueError("Argumentos incompatíveis com a operação Android")
        if op == "observe_nc":
            from .feedback_native_binding import identifiers
            identifiers({k:v for k,v in args.items() if k!="operation"})
            return []
        argv = ["timeout", "85", HOST_ANDROID, op]
        if op in {"open", "play", "is-installed"}:
            package = args.get("package", "")
            if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package):
                raise ValueError("Pacote Android inválido")
            argv.append(package)
        if op in {"tap", "swipe", "long_press", "pinch"}:
            for name in (("x", "y", "x2", "y2") if op == "swipe" else ("x", "y")):
                value = args.get(name)
                if type(value) is not int or not 0 <= value <= 8192:
                    raise ValueError("Coordenadas inválidas")
                argv.append(str(value))
        if op == "pinch":
            radii = [args.get("start_radius"), args.get("end_radius")]
            if any(type(value) is not int or not 1 <= value <= 4096 for value in radii):
                raise ValueError("Raio inválido")
            if radii[0] == radii[1] or args["x"] - max(radii) < 0 or args["x"] + max(radii) > 8192:
                raise ValueError("Pinch fora dos limites")
            argv.extend(str(value) for value in radii)
        if op in {"swipe", "long_press", "pinch"}:
            duration = args.get("duration_ms", 1000 if op == "long_press" else 500)
            if type(duration) is not int or not 100 <= duration <= 5000:
                raise ValueError("Duração inválida")
            argv.append(str(duration))
        if op == "orientation":
            value = args.get("orientation")
            if not isinstance(value, str) or value not in {"portrait", "landscape", "auto"}:
                raise ValueError("Orientação inválida")
            argv.append(value)
        if op == "type":
            value = args.get("text")
            if not isinstance(value, str) or not value or len(value) > 2000 or any(c in value for c in "\x00\n\r"):
                raise ValueError("Texto inválido")
            # Host wrapper quotes once at the ADB shell boundary; no SSH shell.
            argv.append(value)
        return argv

    async def execute(self, workspace, thread, args, turn_active=True, turn_id="", *, _scheduler_binding=None):
        key = self._key(workspace, thread)
        argv = self.command(args)  # Validate before starting any runtime.
        if _scheduler_binding is not None and args['operation'] == 'observe_nc':
            raise ValueError('scheduled_android_operation_not_allowed')
        if args["operation"] == "observe_nc":
            if self.feedback_worker is None:raise ValueError("Leitor nativo NC não disponível")
            from .feedback_native_binding import collect
            return await collect(self,self.feedback_worker(),
                {k:v for k,v in args.items() if k!="operation"},workspace,thread,turn_id,
                publish_observation=True)
        async with self.lock:
            if _scheduler_binding is not None:
                from .android_jobs import _require_scheduled_binding
                _require_scheduled_binding(self, _scheduler_binding, key, args)
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
                if op == "screenshot":
                    image = json.loads(output)["image_url"]
                    if not isinstance(image, str) or not image.startswith("data:image/jpeg;base64,") or len(image) > 1_300_000:
                        raise RuntimeError("Captura Android inválida")
                    from .feedback_native_binding import require_owned_turn
                    from .feedback_capture_proof import decoded
                    proof=decoded(image,'',time.time())
                    if turn_id and self.turn_active:
                        proof['lease']=require_owned_turn(self,workspace,thread,turn_id)
                        orientation='portrait' if proof['height']>=proof['width'] else 'landscape'
                        self.capture_proofs[orientation]=proof
                    return {**self._status(key), "image_url": image}
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
            image = value.pop("image_url", None)
            items = [{"type": "inputText", "text": json.dumps(value, ensure_ascii=False)}]
            if image:
                items.append({"type": "inputImage", "imageUrl": image})
            return {"success": True, "contentItems": items}
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
            elif self.owner and self.owner != self.background_owner and not self.viewers and self.owner not in active_keys and time.monotonic() - self.last_used > 60:
                await self._stop()

    async def worker(self, events, active_keys):
        subscription = await events.subscribe()
        from .android_jobs import ScheduledWhatsApp
        scheduled = None
        try:
            try:
                await self.recover()
            except Exception:
                LOG.exception("Recuperação Android pendente; Dex continua disponível")
            scheduled = asyncio.create_task(ScheduledWhatsApp(self).worker(), name="clc-whatsapp-collector")
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
            if scheduled:
                scheduled.cancel()
                try:
                    await scheduled
                except asyncio.CancelledError:
                    pass
            await events.unsubscribe(subscription)
