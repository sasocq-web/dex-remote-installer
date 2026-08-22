from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any

DEFAULT_SOCKET = Path("/run/sasocq-control/broker.sock")


class ControlPlaneError(RuntimeError):
    pass


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
    return response


def status(socket_path: str | Path = DEFAULT_SOCKET) -> dict[str, Any]:
    try:
        ping = request("ping", socket_path=socket_path, timeout=4)
        if not ping.get("ok"):
            return {"available": False, "error": ping.get("error", "broker indisponível")}
        return {"available": True, "socket": str(socket_path), "ping": ping.get("result", {})}
    except ControlPlaneError as exc:
        return {"available": False, "socket": str(socket_path), "error": str(exc)}
