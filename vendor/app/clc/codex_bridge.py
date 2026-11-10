from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from asyncio.subprocess import Process
from typing import Any, Awaitable, Callable, Dict, Hashable, Optional

from .config import Settings
from .events import EventHub
from .executor_budget import supervise

LOGGER = logging.getLogger(__name__)
WRITE_TIMEOUT_SECONDS = 15

# App-server uses JSONL and a thread/read or thread/resume response can contain
# the complete conversation in one line. A project conversation reached 197 MB
# on disk and exceeded the previous 64 MiB stream limit. Keep this bounded,
# while allowing the existing history to load without terminating its bridge.
SUBPROCESS_STREAM_LIMIT = 384 * 1024 * 1024


class CodexRPCError(RuntimeError):
    def __init__(self, method: str, error: Any) -> None:
        super().__init__(f"Falha RPC em {method}: {error}")
        self.method = method
        self.error = error


class CodexBridge:
    """Bidirectional JSONL bridge to one local `codex app-server` process."""

    def __init__(
        self,
        settings: Settings,
        events: EventHub,
        *,
        label: str = "system",
        command: list[str] | None = None,
        environment: dict[str, str] | None = None,
        server_request_handler: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.label = label
        self.command = list(command) if command else None
        self.environment = dict(environment or {})
        self.server_request_handler = server_request_handler
        self.process: Optional[Process] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._wait_task: Optional[asyncio.Task[None]] = None
        self._start_lock = asyncio.Lock()
        self.recovery_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._id_lock = asyncio.Lock()
        self._resume_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: Dict[Hashable, asyncio.Future[Any]] = {}
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._loaded_threads: set[str] = set()
        self._active_threads: dict[str, str] = {}
        self._idle_tasks: dict[str, asyncio.Task] = {}
        self._fatal_error: str | None = None
        self._budget_task = None
        self._last_progress = time.monotonic()
        self.resource_status = {}
        self.private_threads: set[str] = set()
        self.private_events = EventHub()
        self.last_error: Optional[str] = None
        self.initialized = False
        self.generation = 0
        self._process_scope: str | None = None

    @property
    def running(self) -> bool:
        return bool(self.process and self.process.returncode is None)

    async def start(self) -> None:
        if self.running and self.initialized:
            return
        async with self._start_lock:
            if self.running and self.initialized:
                return
            await self.stop()
            args = self.command or self.settings.codex_args
            LOGGER.info("Iniciando Codex app-server: %s", args)
            # Bound all persistent MCP/code-mode helpers. Production measured
            # 213 steady tasks and 91 fork denials at 256; keep startup headroom
            # within 512, preserving memory limits and owned scope cleanup.
            self._process_scope = f"clc-executor-{secrets.token_hex(8)}.scope"
            args = [
                "/usr/bin/systemd-run", "--user", "--scope", "--quiet",
                "--collect", "--no-ask-password", "--slice=codex-control.slice",
                f"--unit={self._process_scope}", "-p", "TasksMax=512",
                "--", *args,
            ]
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=SUBPROCESS_STREAM_LIMIT,
                    env={**os.environ, **self.environment},
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                self.last_error = str(exc)
                await self.events.publish({"kind": "bridge_status", "workspace": self.label, "status": "error", "error": self.last_error})
                raise RuntimeError(f"Não foi possível iniciar o Codex: {exc}") from exc

            self._reader_task = asyncio.create_task(self._read_stdout(), name="codex-app-server-stdout")
            self._stderr_task = asyncio.create_task(self._read_stderr(), name="codex-app-server-stderr")
            self._wait_task = asyncio.create_task(self._wait_process(), name="codex-app-server-wait")

            try:
                await self._request_no_start(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "codex_linux_control",
                            "title": self.settings.app_name,
                            "version": self.settings.app_version,
                        },
                        # Dex always supplies dynamicTools (including its
                        # automation bridge) on thread/start. Codex 0.149+
                        # rejects that field unless the client advertises the
                        # experimental API capability during initialization.
                        "capabilities": {"experimentalApi": True},
                    },
                    timeout=30,
                )
                await self.notify("initialized", {})
                self.initialized = True
                self.generation += 1
                self._budget_task = asyncio.create_task(supervise(self), name="executor-resource-supervisor")
                self.last_error = None
                await self.events.publish({"kind": "bridge_status", "workspace": self.label, "status": "ready"})
            except Exception:
                await self.stop()
                raise

    async def _stop_scope(self, unit: str | None) -> None:
        if not unit:
            return
        # Stop only the unique scope owned by this bridge. This also reaps
        # MCP/CUA children after app-server exits; never enumerate by name.
        cleanup = await asyncio.create_subprocess_exec(
            "/usr/bin/systemctl", "--user", "stop", unit,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=10)
        except asyncio.TimeoutError:
            cleanup.kill()
            await cleanup.wait()
            LOGGER.warning("Encerramento do escopo ainda pendente: %s", unit)

    async def stop(self) -> None:
        if self._budget_task:
            self._budget_task.cancel()
            self._budget_task = None
        for task in self._idle_tasks.values():
            task.cancel()
        self._idle_tasks.clear()
        await self._fail_active("Executor encerrado; confira os efeitos das ferramentas antes de repetir o trabalho.")
        self._fatal_error = None
        self.initialized = False
        self._loaded_threads.clear()
        process = self.process
        scope = self._process_scope
        self._process_scope = None
        self.process = None
        if process and process.returncode is None:
            process.terminate()
            await self._stop_scope(scope)
            scope = None
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        await self._stop_scope(scope)
        for task in (self._reader_task, self._stderr_task, self._wait_task):
            if task and not task.done():
                task.cancel()
        self._reader_task = self._stderr_task = self._wait_task = None
        for task in self._server_request_tasks:
            task.cancel()
        self._server_request_tasks.clear()
        self._fail_pending(RuntimeError("Codex app-server foi encerrado"))

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 120) -> Any:
        await self.start()
        if method == "thread/resume":
            return await self.ensure_thread_loaded(str((params or {})["threadId"]))
        if method in {"thread/start", "turn/start"}:
            async with self._resume_lock:
                self._check_capacity()
                thread_id = str((params or {}).get("threadId") or "")
                self._cancel_idle(thread_id)
                if method == "turn/start" and thread_id not in self._loaded_threads:
                    await self._request_no_start("thread/resume", {"threadId": thread_id, "excludeTurns": True}, 120)
                    self._loaded_threads.add(thread_id)
                try:
                    result = await self._request_no_start(method, params, timeout)
                except (asyncio.TimeoutError, OSError):
                    # No retry: the server may have accepted the request.
                    self.last_error = "Resultado da execução incerto; confira o histórico antes de reenviar."
                    raise
                if method == "thread/start":
                    self.mark_thread_loaded(str((result or {}).get("thread", {}).get("id") or ""))
                return result
        return await self._request_no_start(method, params, timeout)

    def _check_capacity(self) -> None:
        if self._fatal_error:
            raise RuntimeError(self._fatal_error)
        # Evaluate the owned scope and all ancestor limits, preserving control
        # plane headroom. This is admission control, never a reason to kill work.
        try:
            relative = next(x[3:] for x in Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0::'))
            group = Path('/sys/fs/cgroup') / relative.lstrip('/')
            group = group.parent / str(self._process_scope)
            while group != Path('/sys/fs/cgroup'):
                if (group / 'pids.max').exists():
                    limit = (group / 'pids.max').read_text().strip()
                    current = int((group / 'pids.current').read_text())
                    if limit != 'max' and int(limit) - current < 64:
                        raise RuntimeError('Executor sem margem para novas ferramentas; aguarde a liberação de recursos e tente novamente.')
                group = group.parent
            available = next(int(x.split()[1]) for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:'))
            if available < 1024 * 1024:
                raise RuntimeError('Memória disponível abaixo da reserva de 1 GiB; nova execução adiada.')
        except (OSError, StopIteration, ValueError):
            LOGGER.warning('Medição de capacidade indisponível para %s', self.label)

    def _cancel_idle(self, thread_id: str) -> None:
        task = self._idle_tasks.pop(thread_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    def _schedule_idle(self, thread_id: str) -> None:
        self._cancel_idle(thread_id)
        if thread_id and thread_id not in self.private_threads:
            self._idle_tasks[thread_id] = asyncio.create_task(self._release_idle(thread_id, self.generation))

    async def _release_idle(self, thread_id: str, generation: int) -> None:
        try:
            await asyncio.sleep(120)
            async with self._resume_lock:
                if generation != self.generation or not self.running or thread_id in self._active_threads:
                    return
                # The engine owns background sessions and the final unload
                # decision. Unsubscribe is not archive/delete or process killing.
                result = await self._request_no_start('thread/unsubscribe', {'threadId': thread_id}, 20)
                if (result or {}).get('status') in {'unsubscribed', 'notSubscribed', 'notLoaded'}:
                    self._loaded_threads.discard(thread_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception('Não foi possível liberar conversa inativa %s', thread_id)
        finally:
            if self._idle_tasks.get(thread_id) is asyncio.current_task():
                self._idle_tasks.pop(thread_id, None)

    async def _fail_active(self, reason: str) -> None:
        active, self._active_threads = self._active_threads, {}
        for thread_id, turn_id in active.items():
            hub = self.private_events if thread_id in self.private_threads else self.events
            await hub.publish({'kind': 'notification', 'workspace': self.label, 'notification': {
                'method': 'turn/completed', 'params': {'threadId': thread_id, 'turn': {
                    'id': turn_id, 'status': 'failed', 'error': {'message': reason}}}}})

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def respond(self, request_id: Hashable, result: Dict[str, Any]) -> None:
        await self.start()
        await self._send({"id": request_id, "result": result})

    def mark_thread_loaded(self, thread_id: str) -> None:
        """Remember threads already loaded by this app-server process."""
        if thread_id:
            self._loaded_threads.add(thread_id)
            self._schedule_idle(thread_id)

    def forget_thread(self, thread_id: str) -> None:
        self._loaded_threads.discard(thread_id)
        self._cancel_idle(thread_id)

    async def ensure_thread_loaded(self, thread_id: str) -> Any:
        """Resume a persisted thread at most once per app-server process.

        Codex 0.147 can stall when ``thread/resume`` is sent for a thread that
        is already live in the same app-server.  Serializing this check also
        prevents the UI refresh and message submission paths from racing and
        issuing duplicate resumes.
        """
        await self.start()
        async with self._resume_lock:
            self._cancel_idle(thread_id)
            if thread_id in self._loaded_threads:
                return None
            self._check_capacity()
            result = await self._request_no_start(
                "thread/resume", {"threadId": thread_id, "excludeTurns": True}, timeout=120
            )
            self._loaded_threads.add(thread_id)
            return result

    async def _request_no_start(
        self, method: str, params: Optional[Dict[str, Any]], timeout: float
    ) -> Any:
        request_id = await self._allocate_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        message: Dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        try:
            # Include a blocked writer/lock in the RPC deadline, not only the
            # response wait after a successful write. Never replay turn/start.
            async with asyncio.timeout(timeout):
                await self._send(message)
                response = await future
        except BaseException:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise
        if isinstance(response, dict) and "error" in response:
            raise CodexRPCError(method, response["error"])
        return response.get("result") if isinstance(response, dict) else response

    async def _allocate_id(self) -> int:
        async with self._id_lock:
            value = self._next_id
            self._next_id += 1
            return value

    async def _send(self, message: Dict[str, Any]) -> None:
        process = self.process
        if not process or process.returncode is not None or not process.stdin:
            raise RuntimeError("Codex app-server não está em execução")
        payload = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
                async with self._write_lock:
                    if self.process is not process:
                        raise RuntimeError("Executor mudou durante o envio; resultado não confirmado")
                    process.stdin.write(payload)
                    await process.stdin.drain()
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            if self.process is process and process.returncode is None:
                self._fatal_error = "Canal de comunicação do executor falhou; confira os efeitos antes de reenviar."
                process.terminate()
            raise

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        stream = self.process.stdout
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    LOGGER.warning("Linha não JSON recebida do app-server: %r", line[:500])
                    continue

                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message) and "method" not in message:
                    future = self._pending.pop(request_id, None)
                    if future and not future.done():
                        future.set_result(message)
                    continue

                if request_id is not None and message.get("method"):
                    if self.server_request_handler:
                        task = asyncio.create_task(
                            self._handle_server_request(message),
                            name=f"codex-app-server-request-{self.label}",
                        )
                        self._server_request_tasks.add(task)
                        task.add_done_callback(self._server_request_tasks.discard)
                        continue
                    await self.events.publish({"kind": "server_request", "workspace": self.label, "request": message})
                    continue

                if message.get("method"):
                    params = message.get('params') or {}
                    thread_id = str(params.get('threadId') or (params.get('thread') or {}).get('id') or '')
                    method = message['method']
                    if thread_id in self._active_threads or method == 'turn/started':
                        self._last_progress = time.monotonic()
                    if method == 'turn/started':
                        self._cancel_idle(thread_id)
                        self._active_threads[thread_id] = str((params.get('turn') or {}).get('id') or '')
                    elif method == 'turn/completed':
                        turn_id = str((params.get('turn') or {}).get('id') or '')
                        if self._active_threads.get(thread_id) in (None, '', turn_id):
                            self._active_threads.pop(thread_id, None)
                            self._schedule_idle(thread_id)
                    elif method == 'thread/closed':
                        self.forget_thread(thread_id)
                    private = str((message.get("params") or {}).get("threadId") or "") in self.private_threads
                    hub = self.private_events if private else self.events
                    await hub.publish({"kind": "notification", "workspace": self.label, "notification": message})
                    continue

                LOGGER.debug("Mensagem desconhecida do app-server: %s", message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.initialized = False
            self.last_error = f"Falha ao ler a resposta do Codex app-server: {exc}"
            LOGGER.exception("%s (%s)", self.last_error, self.label)
            self._fail_pending(RuntimeError(self.last_error))
            await self.events.publish({
                "kind": "bridge_status",
                "workspace": self.label,
                "status": "error",
                "error": self.last_error,
            })
            process = self.process
            if process and process.returncode is None:
                process.terminate()

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        try:
            if str((message.get("params") or {}).get("threadId") or "") in self.private_threads:
                await self._send({"id": request_id, "error": {"code": -32601, "message": "Tools are disabled for private health probes"}})
                return
            result = await self.server_request_handler(self.label, message) if self.server_request_handler else None
            if result is None:
                await self.events.publish({"kind": "server_request", "workspace": self.label, "request": message})
                return
            await self._send({"id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Falha ao atender solicitação interna do app-server (%s)", self.label)
            if request_id is not None and self.running:
                await self._send({"id": request_id, "error": {"code": -32603, "message": str(exc)}})

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        stream = self.process.stderr
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                LOGGER.info("codex: %s", text)
                if 'creating threadpool failed' in text or ('panicked at' in text and 'Resource temporarily unavailable' in text):
                    self._fatal_error = 'Falha interna do executor ao criar threads; resultado das ferramentas pode ser incerto.'
                    self.last_error = self._fatal_error
                    await self._fail_active(self._fatal_error)
                    self._fail_pending(RuntimeError(self._fatal_error))
                    await self.events.publish({'kind': 'bridge_status', 'workspace': self.label, 'status': 'error', 'error': self._fatal_error})
                    if self.process and self.process.returncode is None:
                        self.process.terminate()

    async def _wait_process(self) -> None:
        assert self.process
        process = self.process
        scope = self._process_scope
        # Process.wait may wait for inherited pipes held open by orphaned
        # helpers. Observe the child exit first, then close its exact scope.
        while process.returncode is None:
            await asyncio.sleep(0.1)
        code = process.returncode
        await self._stop_scope(scope)
        if self.process is not process:
            return
        self._process_scope = None
        self.initialized = False
        self.last_error = self._fatal_error or f"Codex app-server encerrou com código {code}"
        await self._fail_active(self.last_error + '; confira os efeitos das ferramentas antes de reenviar.')
        self._fail_pending(RuntimeError(self.last_error))
        await self.events.publish({"kind": "bridge_status", "workspace": self.label, "status": "stopped", "returncode": code})

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
