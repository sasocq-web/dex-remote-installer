from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any


STORAGE_SCAN_ARGV = [
    "du", "-x", "-B1", "--max-depth=2",
    "/var", "/home", "/opt", "/srv", "/usr", "/root", "/etc",
]
PROCESS_SCAN_ARGV = [
    "ps", "-eo", "pid=,ppid=,user:32=,comm:48=,pcpu=,pmem=,rss=,etimes=",
    "--sort=-rss",
]

TOP_LEVEL_LABELS = {
    "/var": "Sistema, VM e aplicativos",
    "/home": "Usuários e conversas",
    "/opt": "Componentes opcionais",
    "/usr": "Ubuntu e programas",
    "/srv": "Dados dos serviços SASOCQ",
    "/root": "Administração do host",
    "/etc": "Configurações",
}

PATH_LABELS = {
    "/var/lib/libvirt": "Servidor Ubuntu (VM)",
    "/var/lib/sasocq-system-image": "Imagem local de recuperação",
    "/opt/sasocq-android-arm": "Emulador Android ARM",
    "/var/lib/flatpak": "Aplicativos Flatpak",
    "/var/log": "Registros do sistema",
    "/var/cache": "Caches do sistema",
    "/var/lib/snapd": "Aplicativos Snap",
    "/var/lib/waydroid": "Waydroid",
    "/var/lib/sasocq-android-arm": "Dados do Android ARM",
    "/opt/codex-linux-control": "Dex e releases do Control Plane",
    "/srv/sasocq": "Sites e dados SASOCQ",
    "/home/codex": "Codex do Sistema",
    "/home/codex-worker": "Codex de Projetos",
    "/home/jogos": "Perfil de jogos",
    "/home/desktop": "Desktop Ubuntu",
}

SCOPE_DEFINITIONS = {
    "server": {"label": "Servidor Ubuntu", "description": "VM permanente que hospeda sasocq.com", "protected": True},
    "control": {"label": "Dex e Control Plane", "description": "Esta conversa, navegador e administração", "protected": True},
    "projects": {"label": "Codex de Projetos", "description": "Workers e aplicativos dos projetos", "protected": False},
    "games": {"label": "Jogos e HDMI", "description": "Steam e sessão dedicada de jogos", "protected": False},
    "desktop": {"label": "Desktop Ubuntu", "description": "Sessão gráfica convencional", "protected": False},
    "system": {"label": "Sistema Ubuntu", "description": "Kernel e serviços essenciais do host", "protected": True},
}


def parse_du_output(raw: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in str(raw or "").splitlines():
        size_text, separator, path = line.partition("\t")
        if not separator:
            continue
        try:
            size = max(0, int(size_text.strip()))
        except ValueError:
            continue
        normalized = str(PurePosixPath(path.strip()))
        if normalized.startswith("/"):
            values[normalized] = size
    return values


def storage_snapshot(raw: str, filesystem: dict[str, Any]) -> dict[str, Any]:
    sizes = parse_du_output(raw)
    categories = [
        {"path": path, "label": label, "size": sizes.get(path, 0)}
        for path, label in TOP_LEVEL_LABELS.items()
        if sizes.get(path, 0) > 0
    ]
    categories.sort(key=lambda item: item["size"], reverse=True)

    candidates: list[dict[str, Any]] = []
    for path, size in sizes.items():
        if path in TOP_LEVEL_LABELS or path not in PATH_LABELS or size <= 0:
            continue
        candidates.append({
            "path": path,
            "label": PATH_LABELS[path],
            "size": size,
            "recognized": True,
        })
    candidates.sort(key=lambda item: item["size"], reverse=True)

    total = int(filesystem.get("total") or 0)
    used = int(filesystem.get("used") or 0)
    free = int(filesystem.get("free") or 0)
    return {
        "filesystem": {**filesystem, "total": total, "used": used, "free": free},
        "categories": categories,
        "largest": candidates[:18],
        "scanned_bytes": sum(item["size"] for item in categories),
    }


def _process_scope(user: str, command: str) -> str:
    normalized_user = user.casefold()
    normalized_command = command.casefold()
    if normalized_user.startswith("libvirt") or normalized_command.startswith("qemu-system"):
        return "server"
    if normalized_user == "codex-worker":
        return "projects"
    if normalized_user == "jogos":
        return "games"
    if normalized_user == "desktop":
        return "desktop"
    if normalized_user == "codex":
        return "control"
    return "system"


def process_snapshot(raw: str, *, limit: int = 80) -> dict[str, Any]:
    processes: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        fields = line.split()
        if len(fields) != 8:
            continue
        pid_text, ppid_text, user, command, cpu_text, memory_text, rss_text, elapsed_text = fields
        try:
            pid = int(pid_text)
            ppid = int(ppid_text)
            cpu = max(0.0, float(cpu_text.replace(",", ".")))
            memory_percent = max(0.0, float(memory_text.replace(",", ".")))
            memory_bytes = max(0, int(rss_text)) * 1024
            elapsed = max(0, int(elapsed_text))
        except ValueError:
            continue
        if pid <= 0 or memory_bytes <= 0:
            continue
        scope = _process_scope(user, command)
        definition = SCOPE_DEFINITIONS[scope]
        processes.append({
            "pid": pid,
            "ppid": ppid,
            "user": user,
            "command": command,
            "cpu_percent": cpu,
            "memory_percent": memory_percent,
            "memory_bytes": memory_bytes,
            "elapsed_seconds": elapsed,
            "scope": scope,
            "scope_label": definition["label"],
            "protected": bool(definition["protected"]),
        })

    processes.sort(key=lambda item: (item["memory_bytes"], item["cpu_percent"]), reverse=True)
    groups: dict[str, dict[str, Any]] = {}
    aggregate: dict[str, dict[str, float | int]] = defaultdict(lambda: {"process_count": 0, "memory_bytes": 0, "cpu_percent": 0.0})
    for process in processes:
        bucket = aggregate[process["scope"]]
        bucket["process_count"] = int(bucket["process_count"]) + 1
        bucket["memory_bytes"] = int(bucket["memory_bytes"]) + int(process["memory_bytes"])
        bucket["cpu_percent"] = float(bucket["cpu_percent"]) + float(process["cpu_percent"])
    for scope, values in aggregate.items():
        groups[scope] = {
            "scope": scope,
            **SCOPE_DEFINITIONS[scope],
            "process_count": int(values["process_count"]),
            "memory_bytes": int(values["memory_bytes"]),
            "cpu_percent": round(float(values["cpu_percent"]), 1),
        }
    ordered_groups = sorted(groups.values(), key=lambda item: item["memory_bytes"], reverse=True)
    return {"groups": ordered_groups, "processes": processes[: max(1, min(limit, 200))]}
