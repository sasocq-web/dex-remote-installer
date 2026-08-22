from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict

from .control_plane import request as control_request

SERVER_NAME = "sasocq-system-control"
SERVER_VERSION = "0.9.0"


def _tool(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("sasocq_host_overview", "Lê o estado do host, servidor, Steam, workers, armazenamento, temperatura e serviços."),
    _tool("sasocq_hardware_preflight", "Lê a compatibilidade e o dimensionamento detectado do hardware."),
    _tool("sasocq_resource_manage", "Consulta ou muda o perfil dinâmico de recursos.", {
        "operation": {"type": "string", "enum": ["status", "apply", "set-mode"]},
        "profile": {"type": "string"}, "mode": {"type": "string"},
    }),
    _tool("sasocq_gaming_manage", "Consulta, abre, encerra, reinicia ou sincroniza a Steam Machine e outras lojas.", {
        "operation": {"type": "string", "enum": ["status", "start", "stop", "restart", "sync-library", "open-store"]},
        "store": {"type": "string", "enum": ["heroic", "lutris", "bottles", "boilr"]},
    }),
    _tool("sasocq_vm_manage", "Consulta, controla ou reconcilia os recursos persistentes da VM sasocq-server.", {
        "operation": {"type": "string", "enum": ["status", "resource-status", "reconcile-resources", "start", "shutdown", "reboot", "destroy"]},
        "confirm": {"type": "boolean"},
    }, ["operation"]),
    _tool("sasocq_host_service", "Consulta ou controla um serviço permitido do host.", {
        "unit": {"type": "string"},
        "operation": {"type": "string", "enum": ["status", "start", "stop", "restart", "reload", "enable", "disable"]},
        "confirm": {"type": "boolean"},
    }, ["unit", "operation"]),
    _tool("sasocq_workers_manage", "Consulta, registra, prioriza, pausa, continua ou encerra cada worker de projeto independentemente, sem afetar o Control Plane.", {
        "operation": {"type": "string", "enum": ["status", "register", "unregister", "priority", "pause", "resume", "stop"]},
        "project_id": {"type": "string"}, "name": {"type": "string"}, "path": {"type": "string"},
        "priority": {"type": "string", "enum": ["background", "low", "normal", "high", "critical"]},
        "confirm": {"type": "boolean"},
    }),
    _tool("sasocq_backup_manage", "Consulta, executa, lista snapshots, valida ou restaura o backup criptografado da VM em uma conta OneDrive independente.", {
        "operation": {"type": "string", "enum": ["status", "run", "disable", "snapshots", "restore-status", "validate-restore", "restore"]},
        "snapshot_id": {"type": "string"}, "confirm": {"type": "boolean"},
    }),
    _tool("sasocq_recovery_export", "Compatibilidade: consulta ou exporta o pacote local de recuperação.", {
        "operation": {"type": "string", "enum": ["status", "export"]}, "confirm": {"type": "boolean"},
    }),
    _tool("sasocq_recovery_manage", "Consulta, prepara, repara, reaplica a receita, agenda a restauração local sem pendrive ou exporta credenciais de recuperação.", {
        "operation": {"type": "string", "enum": ["status", "prepare", "repair", "reapply", "factory-reset", "export"]},
        "confirm": {"type": "boolean"},
    }),
    _tool("sasocq_game_storage", "Consulta, adota ou prepara o SSD/HD/pendrive externo obrigatório para todos os jogos.", {
        "operation": {"type": "string", "enum": ["status", "ensure", "adopt", "prepare"]},
        "uuid": {"type": "string"}, "device": {"type": "string"}, "confirm": {"type": "boolean"},
    }),
    _tool("sasocq_emulation", "Consulta ou reindexa emuladores e jogos legalmente fornecidos pelo usuário, inclusive o adaptador de Nintendo Switch.", {
        "operation": {"type": "string", "enum": ["status", "scan"]},
    }),
    _tool("sasocq_physical_session", "Consulta ou controla o streaming privado das sessões físicas desktop e Steam/HDMI.", {
        "operation": {"type": "string", "enum": ["status", "start", "stop"]},
        "user": {"type": "string", "enum": ["desktop", "jogos"]},
    }),
    _tool("sasocq_host_exec", "Executa argv administrativo como root no host físico, sem expor shell interativo. Operações destrutivas exigem confirmação.", {
        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128},
        "cwd": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1, "maximum": 7200},
        "environment": {"type": "object", "additionalProperties": {"type": "string"}},
        "input_text": {"type": "string"}, "confirm": {"type": "boolean"},
    }, ["argv"]),
    _tool("sasocq_host_read_file", "Lê um arquivo do host físico com limites e auditoria.", {
        "path": {"type": "string"}, "max_bytes": {"type": "integer"}, "binary": {"type": "boolean"},
    }, ["path"]),
    _tool("sasocq_host_write_file", "Grava atomicamente um arquivo do host físico. Boot, discos e políticas exigem confirmação.", {
        "path": {"type": "string"}, "content": {"type": "string"}, "mode": {"type": "string"},
        "encoding": {"type": "string", "enum": ["utf-8", "base64"]}, "confirm": {"type": "boolean"},
    }, ["path", "content"]),
    _tool("sasocq_authd_manage", "Prepara e configura login gráfico do Ubuntu por Microsoft Entra/Authd, sem vincular a conta OneDrive e sem conceder sudo ao desktop.", {
        "operation": {"type": "string", "enum": ["status", "prepare", "configure"]},
        "tenant_id": {"type": "string"}, "client_id": {"type": "string"}, "owner_email": {"type": "string"},
        "register_device": {"type": "boolean"}, "force_online_check": {"type": "boolean"}, "confirm": {"type": "boolean"},
    }),
    _tool("sasocq_watchdog_manage", "Consulta e administra a autorrecuperação do host: watchdog, panic/lockup, quarentena, manutenção e teste controlado de reboot.", {
        "operation": {"type": "string", "enum": ["status", "check", "install", "maintenance", "safe-mode", "clear-safe-mode", "clear-quarantine", "reboot-test"]},
        "minutes": {"type": "integer", "minimum": 0, "maximum": 240},
        "reason": {"type": "string"}, "resume_workers": {"type": "boolean"}, "confirm": {"type": "boolean"},
    }),
    _tool("sasocq_publication_manage", "Instala e ativa o Cloudflare Tunnel da VM para publicar somente hostnames de sasocq.com sem abrir portas de entrada.", {
        "operation": {"type": "string", "enum": ["status", "install", "configure"]},
        "token": {"type": "string"}, "hostnames": {"type": "array", "items": {"type": "string"}}, "confirm": {"type": "boolean"},
    }),
    _tool("sasocq_packages", "Instala ou remove pacotes do host usando APT.", {
        "operation": {"type": "string", "enum": ["install", "remove"]},
        "packages": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 40},
        "confirm": {"type": "boolean"},
    }, ["operation", "packages"]),
    _tool("sasocq_host_power", "Atualiza, reinicia ou desliga o host. Reiniciar/desligar exigem confirmação explícita.", {
        "operation": {"type": "string", "enum": ["update", "reboot", "poweroff"]},
        "confirm": {"type": "boolean"},
    }, ["operation"]),
]


def _call(action: str, params: dict[str, Any]) -> Any:
    response = control_request(action, params, timeout=7200)
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or response.get("result") or "operação administrativa falhou"))
    result = response.get("result", {})
    if isinstance(result, dict) and "output" in result:
        return result.get("output")
    return result


def _dispatch(name: str, args: dict[str, Any]) -> Any:
    if name == "sasocq_host_overview":
        return _call("system", {"operation": "overview"})
    if name == "sasocq_hardware_preflight":
        return _call("system", {"operation": "hardware"})
    if name == "sasocq_resource_manage":
        return _call("resources", args or {"operation": "status"})
    if name == "sasocq_gaming_manage":
        return _call("gaming", args or {"operation": "status"})
    if name == "sasocq_vm_manage":
        return _call("vm", {"name": "sasocq-server", **args})
    if name == "sasocq_host_service":
        return _call("service", args)
    if name == "sasocq_workers_manage":
        return _call("workers", args or {"operation": "status"})
    if name == "sasocq_backup_manage":
        return _call("backup", args or {"operation": "status"})
    if name in {"sasocq_recovery_manage", "sasocq_recovery_export"}:
        return _call("recovery", args or {"operation": "status"})
    if name == "sasocq_game_storage":
        return _call("game-storage", args or {"operation": "status"})
    if name == "sasocq_emulation":
        return _call("emulation", args or {"operation": "status"})
    if name == "sasocq_physical_session":
        return _call("physical", args or {"operation": "status"})
    if name == "sasocq_host_exec":
        return _call("host-admin", {"operation": "exec", **args})
    if name == "sasocq_host_read_file":
        return _call("host-admin", {"operation": "read-file", **args})
    if name == "sasocq_host_write_file":
        return _call("host-admin", {"operation": "write-file", **args})
    if name == "sasocq_authd_manage":
        return _call("authd", args or {"operation": "status"})
    if name == "sasocq_watchdog_manage":
        return _call("watchdog", args or {"operation": "status"})
    if name == "sasocq_publication_manage":
        return _call("publication", args or {"operation": "status"})
    if name == "sasocq_packages":
        return _call("packages", args)
    if name == "sasocq_host_power":
        return _call("system", args)
    raise ValueError(f"ferramenta desconhecida: {name}")


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
            value = _dispatch(str(params.get("name") or ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2, default=str)}]}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"id": request_id, "result": result}
    return {"id": request_id, "error": {"code": -32601, "message": f"Método não implementado: {method}"}}


def main() -> int:
    for raw in sys.stdin:
        try:
            message = json.loads(raw)
            response = handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            print(f"system-mcp: {exc}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
