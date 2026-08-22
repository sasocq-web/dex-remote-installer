from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import struct
try:
    import fcntl
    import pty
    import termios
except ModuleNotFoundError:  # pragma: no cover - Windows development/test host
    fcntl = None
    pty = None
    termios = None
from dataclasses import dataclass
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect


LOGGER = logging.getLogger(__name__)
MAX_INPUT_BYTES = 64 * 1024


@dataclass(frozen=True)
class TerminalSpec:
    command: tuple[str, ...]
    cwd: Path
    label: str
    privileged: bool = False


def _resize(fd: int, cols: int, rows: int) -> None:
    if fcntl is None or termios is None:
        return
    cols = max(40, min(int(cols or 100), 400))
    rows = max(12, min(int(rows or 30), 150))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _spawn(spec: TerminalSpec) -> tuple[int, int]:
    if pty is None:
        raise RuntimeError("O terminal web requer um host Linux com suporte a PTY")
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(spec.cwd)
        environment = dict(os.environ)
        environment.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
        os.execvpe(spec.command[0], list(spec.command), environment)
    os.set_blocking(fd, False)
    _resize(fd, 110, 32)
    return pid, fd


async def serve_terminal(websocket: WebSocket, spec: TerminalSpec) -> None:
    """Bridge one authenticated browser WebSocket to a short-lived Unix PTY."""
    await websocket.accept()
    pid, fd = _spawn(spec)
    LOGGER.warning("Terminal web iniciado: label=%s privileged=%s pid=%s", spec.label, spec.privileged, pid)

    async def output_loop() -> None:
        while True:
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                await asyncio.sleep(0.025)
                continue
            except OSError:
                return
            if not chunk:
                return
            await websocket.send_json({"type": "output", "data": chunk.decode("utf-8", errors="replace")})

    output_task = asyncio.create_task(output_loop(), name=f"web-terminal-output-{pid}")
    try:
        await websocket.send_json({
            "type": "ready",
            "label": spec.label,
            "privileged": spec.privileged,
        })
        while True:
            message = await websocket.receive_text()
            payload = json.loads(message)
            kind = payload.get("type")
            if kind == "resize":
                _resize(fd, payload.get("cols", 110), payload.get("rows", 32))
                continue
            if kind != "input":
                continue
            data = str(payload.get("data", "")).encode("utf-8")
            if len(data) > MAX_INPUT_BYTES:
                await websocket.send_json({"type": "error", "message": "Entrada grande demais"})
                continue
            os.write(fd, data)
    except (WebSocketDisconnect, json.JSONDecodeError, OSError):
        pass
    finally:
        output_task.cancel()
        try:
            os.killpg(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            await asyncio.to_thread(os.waitpid, pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        LOGGER.warning("Terminal web encerrado: label=%s privileged=%s pid=%s", spec.label, spec.privileged, pid)
