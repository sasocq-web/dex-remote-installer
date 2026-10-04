from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
import socket
import uuid
from pathlib import Path
from typing import Any

DEFAULT_SOCKET = Path("/run/sasocq-control/broker.sock")


class ControlPlaneError(RuntimeError):
    pass



def _worker_freeze_observation() -> dict[str, Any]:
    """Read the kernel's current state without changing workers or broker policy."""
    import pwd
    import time
    observation = {"state": "unknown", "paused": None, "checked_at": time.time()}
    try:
        uid = pwd.getpwnam("codex-worker").pw_uid
        base = Path("/sys/fs/cgroup")
        group = base / f"user.slice/user-{uid}.slice/user@{uid}.service/codex.slice/codex-workers.slice"
        events = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
        if events.get("frozen") == "1":
            observation.update(state="frozen", paused=True)
        elif events.get("frozen") == "0":
            requested = any((parent / "cgroup.freeze").read_text().strip() == "1"
                            for parent in (group, *group.parents) if parent != base and base in parent.parents)
            observation.update(state="freezing" if requested else "running", paused=False)
    except (OSError, KeyError, ValueError):
        pass
    return observation


def _reconcile_quarantine_status(response: dict[str, Any]) -> dict[str, Any]:
    """Keep administrative restrictions; never treat an old pause attempt as live state."""
    observation = None
    def walk(value):
        nonlocal observation
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            # Retain audit records exactly as the broker returned them.
            if key in {"previous", "last_snapshot_before_reboot", "unclean_boot_events", "workers", "games"}:
                continue
            if key in {"quarantine", "watchdog_quarantine"} and isinstance(child, dict) and child:
                if observation is None:
                    observation = _worker_freeze_observation()
                child.setdefault("recorded_workers_paused", child.get("workers_paused"))
                child["workers_paused"] = observation["paused"]
                child["workers_pause_state"] = observation["state"]
                child["workers_pause_checked_at"] = observation["checked_at"]
                child["workers_pause_source"] = "linux-cgroup-v2"
                # active/requires_manual_clear/workers_quarantined remain administrative policy.
            else:
                walk(child)
    walk(response)
    return response


def request(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    socket_path: str | Path = DEFAULT_SOCKET,
    timeout: float = 60.0,
) -> dict[str, Any]:
    payload = {
        "request_id": str(uuid.uuid4()),
        "action": action,
        "params": params or {},
    }
    path = Path(socket_path)
    if not path.exists():
        raise ControlPlaneError("O broker administrativo SASOCQ ainda não está instalado ou ativo")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = client.recv(8192)
                if not chunk:
                    break
                data += chunk
                if len(data) > 2_000_000:
                    raise ControlPlaneError("Resposta administrativa excedeu o limite seguro")
    except (OSError, TimeoutError) as exc:
        raise ControlPlaneError(f"Falha ao acessar o broker administrativo: {exc}") from exc
    if not data:
        raise ControlPlaneError("O broker administrativo não retornou resposta")
    try:
        response = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPlaneError("Resposta administrativa inválida") from exc
    if not isinstance(response, dict):
        raise ControlPlaneError("Resposta administrativa inválida")
    if action in {"watchdog", "resources", "system", "gaming", "steam"} and (params or {}).get("operation", "status") in {"status", "overview", "check"}:
        return _reconcile_quarantine_status(response)
    return response


def status(socket_path: str | Path = DEFAULT_SOCKET) -> dict[str, Any]:
    try:
        ping = request("ping", socket_path=socket_path, timeout=4)
        if not ping.get("ok"):
            return {"available": False, "error": ping.get("error", "broker indisponível")}
        return {"available": True, "socket": str(socket_path), "ping": ping.get("result", {})}
    except ControlPlaneError as exc:
        return {"available": False, "socket": str(socket_path), "error": str(exc)}


# Administrative commands may wait for minutes. They must not consume the
# default executor used by conversation transport, policies and file reads.
_admin_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="clc-admin")

async def async_request(action, params=None, *, socket_path=DEFAULT_SOCKET, timeout=60.0):
    return await asyncio.get_running_loop().run_in_executor(
        _admin_executor, partial(request, action, params, socket_path=socket_path, timeout=timeout))


async def async_status(socket_path=DEFAULT_SOCKET):
    return await asyncio.get_running_loop().run_in_executor(
        _admin_executor, partial(status, socket_path))
