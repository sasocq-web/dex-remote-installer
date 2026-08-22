from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable

SERVER_NAME = "sasocq-server-admin"
SERVER_VERSION = "0.9.0"
PROJECT_ROOT = Path("/srv/sasocq/projects").resolve()
SAFE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")
SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PROTECTED = ("/proc", "/sys", "/dev", "/run", "/boot/efi")


def _run(argv: Iterable[str], timeout: int = 900, input_text: str | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(list(argv), input=input_text, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
        return {"returncode": completed.returncode, "output": completed.stdout[-20000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 124, "output": str(exc)}


def _ssh(command: str, timeout: int = 900, input_text: str | None = None) -> dict[str, Any]:
    return _run(["ssh", "-o", "BatchMode=yes", "sasocq-server", command], timeout, input_text)


def _safe_path(value: str) -> str:
    path = os.path.normpath(str(value or "").strip())
    if not path.startswith("/") or path == "/" or len(path) > 4096 or "\x00" in path or any(path == item or path.startswith(item + "/") for item in PROTECTED):
        raise ValueError("caminho do servidor inválido ou protegido")
    return path


def _tool(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}


TOOLS = [
    _tool("server_status", "Consulta disponibilidade, sistema, disco, memória e serviços essenciais da VM."),
    _tool("server_exec", "Executa comando administrativo dentro da VM Ubuntu Server. Não concede acesso ao host físico.", {"command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 5, "maximum": 7200}}, ["command"]),
    _tool("server_read_file", "Lê um arquivo da VM.", {"path": {"type": "string"}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 8000000}}, ["path"]),
    _tool("server_write_file", "Cria ou substitui arquivo na VM.", {"path": {"type": "string"}, "content": {"type": "string"}, "mode": {"type": "string"}}, ["path", "content"]),
    _tool("server_service", "Consulta ou controla serviço systemd da VM.", {"unit": {"type": "string"}, "operation": {"type": "string", "enum": ["status", "start", "stop", "restart", "reload", "enable", "disable"]}}, ["unit", "operation"]),
    _tool("server_deploy", "Publica um projeto local em /srv/sites/<slug>/releases e atualiza o symlink current.", {"project_path": {"type": "string"}, "site_slug": {"type": "string"}, "service_unit": {"type": "string"}, "health_url": {"type": "string"}}, ["project_path", "site_slug"]),
]


def _status(_: dict[str, Any]) -> Any:
    command = "hostname; uname -a; free -h; df -h / /srv; systemctl --no-pager --failed; systemctl show nginx --property=ActiveState,SubState,Result"
    return _ssh(command, 60)


def _exec(args: dict[str, Any]) -> Any:
    command = str(args.get("command") or "").strip()
    if not command or len(command) > 20000 or "\x00" in command:
        raise ValueError("comando inválido")
    timeout = max(5, min(int(args.get("timeout") or 900), 7200))
    result = _ssh("sudo -n /bin/bash -lc " + shlex.quote(command), timeout)
    result["command_sha256"] = hashlib.sha256(command.encode()).hexdigest()
    return result


def _read(args: dict[str, Any]) -> Any:
    path = _safe_path(str(args.get("path") or ""))
    limit = max(1, min(int(args.get("max_bytes") or 2_000_000), 8_000_000))
    result = _ssh(f"sudo -n head -c {limit} -- {shlex.quote(path)} | base64 -w0", 120)
    if result["returncode"] != 0:
        return result
    raw = base64.b64decode(result["output"], validate=True)
    return {"returncode": 0, "path": path, "size": len(raw), "content": raw.decode("utf-8", errors="replace")}


def _write(args: dict[str, Any]) -> Any:
    path = _safe_path(str(args.get("path") or ""))
    content = str(args.get("content") or "")
    raw = content.encode("utf-8")
    if len(raw) > 8_000_000:
        raise ValueError("arquivo grande demais")
    mode = str(args.get("mode") or "0644")
    if not re.fullmatch(r"0?[0-7]{3,4}", mode):
        raise ValueError("modo inválido")
    remote = f"sudo -n mkdir -p {shlex.quote(os.path.dirname(path))} && base64 -d | sudo -n tee {shlex.quote(path)} >/dev/null && sudo -n chmod {shlex.quote(mode)} {shlex.quote(path)}"
    result = _ssh(remote, 180, base64.b64encode(raw).decode("ascii"))
    result.update({"path": path, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return result


def _service(args: dict[str, Any]) -> Any:
    unit = str(args.get("unit") or "")
    operation = str(args.get("operation") or "status")
    if not SAFE_UNIT_RE.fullmatch(unit):
        raise ValueError("unidade inválida")
    if operation == "status":
        command = f"sudo -n systemctl show {shlex.quote(unit)} --property=ActiveState,SubState,UnitFileState,Result --no-pager"
    elif operation in {"enable", "disable"}:
        command = f"sudo -n systemctl {operation} --now {shlex.quote(unit)}"
    elif operation in {"start", "stop", "restart", "reload"}:
        command = f"sudo -n systemctl {operation} {shlex.quote(unit)}"
    else:
        raise ValueError("operação inválida")
    return _ssh(command, 180)


def _deploy(args: dict[str, Any]) -> Any:
    source = Path(str(args.get("project_path") or "")).expanduser().resolve()
    try:
        source.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError(f"projeto fora de {PROJECT_ROOT}") from exc
    if not source.is_dir():
        raise ValueError("projeto não existe")
    slug = str(args.get("site_slug") or "").casefold()
    if not SAFE_SLUG_RE.fullmatch(slug):
        raise ValueError("slug inválido")
    service = str(args.get("service_unit") or "")
    health = str(args.get("health_url") or "")
    if service and not SAFE_UNIT_RE.fullmatch(service):
        raise ValueError("serviço inválido")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    staging = f"/home/codex-project/.sasocq-deploy/{slug}-{stamp}"
    release = f"/srv/sites/{slug}/releases/{stamp}"
    init = _ssh(f"mkdir -p {shlex.quote(staging)}", 60)
    if init["returncode"] != 0:
        return init
    rsync = _run(["rsync", "-a", "--delete", "--safe-links", "--exclude", ".git/", "--exclude", ".env", "-e", "ssh -o BatchMode=yes", f"{source}/", f"sasocq-server:{staging}/"], 1800)
    if rsync["returncode"] != 0:
        return rsync
    steps = [
        f"sudo -n mkdir -p /srv/sites/{slug}/releases",
        f"sudo -n mv {shlex.quote(staging)} {shlex.quote(release)}",
        f"sudo -n chown -R www-data:www-data {shlex.quote(release)}",
        f"sudo -n ln -sfn {shlex.quote(release)} /srv/sites/{slug}/current",
    ]
    if service:
        steps.append(f"sudo -n systemctl restart {shlex.quote(service)}")
    if health:
        if not health.startswith(("https://", "http://")):
            raise ValueError("URL de saúde inválida")
        steps.append(f"curl --fail --silent --show-error --max-time 30 {shlex.quote(health)} >/dev/null")
    result = _ssh(" && ".join(steps), 600)
    result.update({"site": slug, "release": release})
    return result


HANDLERS = {"server_status": _status, "server_exec": _exec, "server_read_file": _read, "server_write_file": _write, "server_service": _service, "server_deploy": _deploy}


def handle(message: Dict[str, Any]) -> Dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        requested = str(params.get("protocolVersion") or "2025-06-18")
        return {"id": request_id, "result": {"protocolVersion": requested, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method in {"notifications/initialized", "initialized"}:
        return None
    if method == "tools/list":
        return {"id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            handler = HANDLERS[str(params.get("name") or "")]
            value = handler(params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2, default=str)}], "isError": bool(isinstance(value, dict) and value.get("returncode", 0) != 0)}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"id": request_id, "result": result}
    return {"id": request_id, "error": {"code": -32601, "message": f"Método não implementado: {method}"}}


def main() -> int:
    for raw in sys.stdin:
        try:
            response = handle(json.loads(raw))
            if response is not None:
                print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
        except Exception as exc:
            print(f"server-mcp: {exc}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
