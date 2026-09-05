from __future__ import annotations

import asyncio
import json
import logging
import os
from asyncio.subprocess import Process
from typing import Any, Awaitable, Callable, Dict, Hashable, Optional

from .config import Settings
from .events import EventHub

LOGGER = logging.getLogger(__name__)

# App-server uses JSONL and a thread/read or thread/resume response can contain
# the complete conversation in one line.  asyncio's 64 KiB default breaks the
# reader for ordinary conversations with a moderately sized history.
SUBPROCESS_STREAM_LIMIT = 64 * 1024 * 1024


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
        self.last_error: Optional[str] = None
        self.initialized = False
        self.generation = 0

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
                self.last_error = None
                await self.events.publish({"kind": "bridge_status", "workspace": self.label, "status": "ready"})
            except Exception:
                await self.stop()
                raise

    async def stop(self) -> None:
        self.initialized = False
        self._loaded_threads.clear()
        process = self.process
        self.process = None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
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
        return await self._request_no_start(method, params, timeout)

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def respond(self, request_id: Hashable, result: Dict[str, Any]) -> None:
        await self.start()
        await self._send({"id": request_id, "result": result})

    def mark_thread_loaded(self, thread_id: str) -> None:
        """Remember threads already loaded by this app-server process."""
        if thread_id:
            self._loaded_threads.add(thread_id)

    def forget_thread(self, thread_id: str) -> None:
        self._loaded_threads.discard(thread_id)

    async def ensure_thread_loaded(self, thread_id: str) -> Any:
        """Resume a persisted thread at most once per app-server process.

        Codex 0.147 can stall when ``thread/resume`` is sent for a thread that
        is already live in the same app-server.  Serializing this check also
        prevents the UI refresh and message submission paths from racing and
        issuing duplicate resumes.
        """
        await self.start()
        async with self._resume_lock:
            if thread_id in self._loaded_threads:
                return None
            result = await self._request_no_start(
                "thread/resume", {"threadId": thread_id}, timeout=120
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
        await self._send(message)
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except Exception:
            self._pending.pop(request_id, None)
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
        async with self._write_lock:
            process.stdin.write(payload)
            await process.stdin.drain()

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
                    await self.events.publish({"kind": "notification", "workspace": self.label, "notification": message})
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

    async def _wait_process(self) -> None:
        assert self.process
        process = self.process
        code = await process.wait()
        self.initialized = False
        self.last_error = f"Codex app-server encerrou com código {code}"
        self._fail_pending(RuntimeError(self.last_error))
        await self.events.publish({"kind": "bridge_status", "workspace": self.label, "status": "stopped", "returncode": code})

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
