from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from .config import Settings, persist_settings


URL_RE = re.compile(r"https?://[^\s<>\]\[\)\(\"']+")
TAILSCALE_LOGIN_RE = re.compile(r"https://login\.tailscale\.com/[^\s<>\]\[\)\(\"']+")


@dataclass
class SetupTask:
    id: str
    kind: str
    title: str
    status: str = "queued"
    message: str = "Aguardando…"
    logs: list[str] = field(default_factory=list)
    action_url: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def append(self, line: str) -> None:
        clean = line.rstrip()
        if not clean:
            return
        self.logs.append(clean)
        self.logs = self.logs[-400:]
        for url in URL_RE.findall(clean):
            candidate = url.rstrip(".,;")
            allowed = (
                "tailscale.com" in candidate
                or "chatgpt.com" in candidate
                or "openai.com" in candidate
                or "accounts.google.com" in candidate
                or "login.microsoftonline.com" in candidate
                or candidate.startswith("http://127.0.0.1:")
                or candidate.startswith("http://localhost:")
            )
            if allowed:
                self.action_url = candidate
                self.message = "Abra a página de autorização e conclua o acesso."
        self.updated_at = time.time()

    def set_message(self, message: str) -> None:
        self.message = message
        self.updated_at = time.time()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "logs": self.logs,
            "action_url": self.action_url,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


Worker = Callable[[SetupTask], Awaitable[Dict[str, Any] | None]]


class SetupTaskManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, SetupTask] = {}
        self._running: Dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def start(self, kind: str, title: str, worker: Worker) -> SetupTask:
        async with self._lock:
            for task in self._tasks.values():
                if task.kind == kind and task.status in {"queued", "running"}:
                    return task
            record = SetupTask(id=uuid.uuid4().hex, kind=kind, title=title)
            self._tasks[record.id] = record
            runner = asyncio.create_task(self._run(record, worker), name=f"setup-{kind}-{record.id[:8]}")
            self._running[record.id] = runner
            return record

    async def _run(self, record: SetupTask, worker: Worker) -> None:
        record.status = "running"
        record.set_message("Em andamento…")
        try:
            result = await worker(record)
            record.result = result or {}
            record.status = "succeeded"
            if record.message in {"Em andamento…", "Aguardando…"}:
                record.set_message("Concluído.")
        except asyncio.CancelledError:
            record.status = "cancelled"
            record.set_message("Operação cancelada.")
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the graphical client
            record.status = "failed"
            record.error = str(exc)
            record.set_message(str(exc))
            record.append(f"ERRO: {exc}")
        finally:
            record.updated_at = time.time()
            self._running.pop(record.id, None)

    def get(self, task_id: str) -> Optional[SetupTask]:
        return self._tasks.get(task_id)

    def recent(self, limit: int = 20) -> list[SetupTask]:
        return sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)[:limit]


async def run_streaming_command(
    record: SetupTask,
    argv: Iterable[str],
    *,
    timeout: float = 900,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[Path] = None,
    redact: Optional[Iterable[str]] = None,
) -> tuple[int, str]:
    args = [str(item) for item in argv]
    secrets_to_hide = sorted(
        {str(secret) for secret in (redact or []) if str(secret)},
        key=len,
        reverse=True,
    )

    def redact_text(value: str) -> str:
        clean = value
        for secret in secrets_to_hide:
            clean = clean.replace(secret, "••••••••")
        return clean

    display_args = [redact_text(item) for item in args]
    record.append("$ " + shlex.join(display_args))
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        cwd=str(cwd) if cwd else None,
    )
    captured: list[str] = []

    async def consume() -> None:
        assert process.stdout
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = redact_text(line.decode("utf-8", errors="replace").rstrip())
            captured.append(text)
            record.append(text)

    try:
        await asyncio.wait_for(asyncio.gather(consume(), process.wait()), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        raise RuntimeError("A operação excedeu o tempo permitido") from exc

    output = "\n".join(captured)
    return int(process.returncode or 0), output


def _quick_command(argv: Iterable[str], timeout: float = 8) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            [str(item) for item in argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, completed.stdout.strip()


def _candidate_codex_paths() -> list[Path]:
    found = shutil.which("codex")
    candidates = [Path(found)] if found else []
    candidates.extend(
        [
            Path.home() / ".local" / "bin" / "codex",
            Path.home() / ".local" / "share" / "codex" / "bin" / "codex",
        ]
    )
    unique: list[Path] = []
    for candidate in candidates:
        expanded = candidate.expanduser()
        if expanded not in unique:
            unique.append(expanded)
    return unique


def detect_codex() -> Dict[str, Any]:
    for candidate in _candidate_codex_paths():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            code, output = _quick_command([str(candidate), "--version"])
            return {
                "installed": code == 0,
                "path": str(candidate.resolve()),
                "version": output.splitlines()[0] if output else "Instalado",
                "error": "" if code == 0 else output,
            }
    return {"installed": False, "path": "", "version": "", "error": "Codex não encontrado"}


def _parse_tailscale_json(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    self_node = data.get("Self") or {}
    user_id = str(self_node.get("UserID", ""))
    users = data.get("User") or {}
    user = users.get(user_id) or users.get(int(user_id)) if user_id.isdigit() else users.get(user_id)
    if not isinstance(user, dict):
        user = {}
    dns_name = str(self_node.get("DNSName") or "").rstrip(".")
    return {
        "backend_state": str(data.get("BackendState") or ""),
        "connected": str(data.get("BackendState") or "").casefold() == "running",
        "login": str(user.get("LoginName") or ""),
        "display_name": str(user.get("DisplayName") or ""),
        "dns_name": dns_name,
        "external_url": f"https://{dns_name}" if dns_name else "",
        "tailscale_ips": self_node.get("TailscaleIPs") or [],
    }


def detect_tailscale() -> Dict[str, Any]:
    binary = shutil.which("tailscale")
    if not binary:
        return {
            "installed": False,
            "path": "",
            "version": "",
            "connected": False,
            "login": "",
            "dns_name": "",
            "external_url": "",
            "serve": "",
        }
    version_code, version_output = _quick_command([binary, "version"])
    status_code, status_output = _quick_command([binary, "status", "--json"], timeout=12)
    parsed = _parse_tailscale_json(status_output) if status_code == 0 else {}
    serve_code, serve_output = _quick_command([binary, "serve", "status"], timeout=8)
    return {
        "installed": True,
        "path": binary,
        "version": version_output.splitlines()[0] if version_code == 0 and version_output else "Instalado",
        "connected": bool(parsed.get("connected")),
        "backend_state": parsed.get("backend_state", ""),
        "login": parsed.get("login", ""),
        "display_name": parsed.get("display_name", ""),
        "dns_name": parsed.get("dns_name", ""),
        "external_url": parsed.get("external_url", ""),
        "tailscale_ips": parsed.get("tailscale_ips", []),
        "serve": serve_output if serve_code == 0 else "",
        "error": "" if status_code == 0 else status_output,
    }


def _read_os_release() -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def service_state(settings: Settings) -> Dict[str, Any]:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"available": False, "active": False, "enabled": False}
    active_code, active_output = _quick_command([systemctl, "--user", "is-active", settings.service_name])
    enabled_code, enabled_output = _quick_command([systemctl, "--user", "is-enabled", settings.service_name])
    linger_path = Path("/var/lib/systemd/linger") / (os.environ.get("USER") or Path.home().name)
    return {
        "available": True,
        "active": active_code == 0 and active_output == "active",
        "enabled": enabled_code == 0 and enabled_output == "enabled",
        "linger_enabled": linger_path.exists(),
        "active_text": active_output,
        "enabled_text": enabled_output,
    }


def system_state(settings: Settings) -> Dict[str, Any]:
    os_release = _read_os_release()
    codex = detect_codex()
    tailscale = detect_tailscale()
    return {
        "app": {
            "name": settings.app_name,
            "version": settings.app_version,
            "install_mode": settings.install_mode,
            "package_mode": settings.package_mode,
            "setup_completed": settings.setup_completed,
            "start_at_login": settings.start_at_login,
        },
        "system": {
            "name": os_release.get("PRETTY_NAME") or os_release.get("NAME") or "Linux",
            "id": os_release.get("ID", "linux"),
            "version": os_release.get("VERSION_ID", ""),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
            "zenity": bool(shutil.which("zenity")),
            "pkexec": bool(shutil.which("pkexec")),
        },
        "codex": codex,
        "tailscale": tailscale,
        "service": service_state(settings),
        "configuration": {
            "allowed_tailscale_login": settings.allowed_tailscale_login,
            "external_url": settings.external_url or tailscale.get("external_url", ""),
            "remote_enabled": settings.remote_enabled,
            "project_roots": [str(path) for path in settings.project_roots],
            "browser_control_enabled": settings.browser_control_enabled,
            "desktop_control_enabled": settings.desktop_control_enabled,
            "remote_desktop_enabled": settings.remote_desktop_enabled,
            "device_auth_required": settings.device_auth_required,
        },
        "full_experience": full_experience_state(settings),
    }


async def choose_directory(title: str = "Escolha a pasta do projeto") -> Path:
    zenity = shutil.which("zenity")
    if not zenity:
        raise RuntimeError("O seletor gráfico de pastas (Zenity) não está instalado")
    env = os.environ.copy()
    if not any(env.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY")):
        raise RuntimeError("A sessão gráfica não foi detectada. Abra o aplicativo pelo ícone do menu do Linux.")
    process = await asyncio.create_subprocess_exec(
        zenity,
        "--file-selection",
        "--directory",
        f"--title={title}",
        f"--filename={Path.home()}/",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await process.communicate()
    if process.returncode == 1:
        raise RuntimeError("Seleção cancelada")
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "Não foi possível abrir o seletor de pastas")
    selected = Path(stdout.decode("utf-8", errors="replace").strip()).expanduser().resolve()
    if not selected.is_dir():
        raise RuntimeError("A pasta selecionada não existe")
    return selected


async def choose_deb_file() -> Path:
    zenity = shutil.which("zenity")
    if not zenity:
        raise RuntimeError("O seletor gráfico de arquivos (Zenity) não está instalado")
    env = os.environ.copy()
    if not any(env.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY")):
        raise RuntimeError("A sessão gráfica não foi detectada. Abra o aplicativo pelo ícone do menu do Linux.")
    process = await asyncio.create_subprocess_exec(
        zenity,
        "--file-selection",
        "--title=Escolha a atualização .deb do Codex Linux Control",
        "--file-filter=Pacotes Debian (*.deb) | *.deb",
        "--file-filter=Todos os arquivos | *",
        f"--filename={Path.home()}/Downloads/",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await process.communicate()
    if process.returncode == 1:
        raise RuntimeError("Seleção cancelada")
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "Não foi possível abrir o seletor")
    selected = Path(stdout.decode("utf-8", errors="replace").strip()).expanduser().resolve()
    if not selected.is_file() or selected.suffix.casefold() != ".deb":
        raise RuntimeError("Selecione um arquivo .deb válido")
    return selected


async def install_codex(record: SetupTask, settings: Settings) -> Dict[str, Any]:
    record.set_message("Baixando o instalador oficial do Codex…")
    with tempfile.TemporaryDirectory(prefix="clc-codex-") as temporary:
        installer = Path(temporary) / "install-codex.sh"
        code, _ = await run_streaming_command(
            record,
            ["curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2", "https://chatgpt.com/codex/install.sh", "-o", str(installer)],
            timeout=120,
        )
        if code != 0:
            raise RuntimeError("Não foi possível baixar o instalador oficial do Codex")
        installer.chmod(0o700)
        record.set_message("Instalando ou atualizando o Codex para o seu usuário…")
        code, _ = await run_streaming_command(record, ["sh", str(installer)], timeout=900)
        if code != 0:
            raise RuntimeError("O instalador oficial do Codex terminou com erro")
    detected = detect_codex()
    if not detected.get("installed"):
        raise RuntimeError("A instalação terminou, mas o executável do Codex não foi localizado")
    command = shlex.join([detected["path"], "app-server"])
    persist_settings(settings, codex_command=command)
    record.set_message("Codex instalado e pronto para autenticação.")
    return detected


def _admin_command(settings: Settings, action: str, *args: str) -> list[str]:
    helper = Path(settings.privileged_helper)
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise RuntimeError("O componente administrativo do pacote não foi encontrado. Reinstale o arquivo .deb.")
    sudo = shutil.which("sudo")
    if sudo:
        probe = subprocess.run(
            [sudo, "-n", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if probe.returncode == 0:
            return [sudo, "-n", "--", str(helper), action, *args]
    pkexec = shutil.which("pkexec")
    if not pkexec:
        raise RuntimeError("A autorização administrativa sem senha ou a autorização gráfica (pkexec) não está disponível")
    return [pkexec, str(helper), action, *args]


async def install_tailscale(record: SetupTask, settings: Settings) -> Dict[str, Any]:
    record.set_message("Autorize a instalação na janela do sistema…")
    code, _ = await run_streaming_command(record, _admin_command(settings, "install-tailscale"), timeout=1200)
    if code != 0:
        raise RuntimeError("A instalação gráfica do Tailscale não foi concluída")
    detected = detect_tailscale()
    if not detected.get("installed"):
        raise RuntimeError("O Tailscale não foi localizado após a instalação")
    record.set_message("Tailscale instalado.")
    return detected


async def connect_tailscale(record: SetupTask, settings: Settings) -> Dict[str, Any]:
    record.set_message("Autorize a conexão e abra o endereço que aparecer…")
    code, _ = await run_streaming_command(record, _admin_command(settings, "tailscale-up"), timeout=1200)
    if code != 0:
        detected = detect_tailscale()
        if not detected.get("connected"):
            raise RuntimeError("A conexão com o Tailscale não foi concluída")
    detected = detect_tailscale()
    if not detected.get("connected"):
        raise RuntimeError("Este computador ainda não aparece conectado ao Tailscale")
    record.set_message("Computador conectado à sua rede privada.")
    return detected


async def configure_tailscale_serve(record: SetupTask, settings: Settings) -> Dict[str, Any]:
    record.set_message("Configurando o endereço HTTPS privado…")
    code, _ = await run_streaming_command(
        record,
        _admin_command(settings, "tailscale-serve", str(settings.port)),
        timeout=900,
    )
    if code != 0:
        raise RuntimeError("Não foi possível ativar o Tailscale Serve")
    detected = detect_tailscale()
    login = str(detected.get("login") or "").strip()
    external_url = str(detected.get("external_url") or "").strip()
    if not login:
        raise RuntimeError("O Tailscale está conectado, mas não foi possível identificar automaticamente o seu login")
    if not external_url:
        raise RuntimeError("O endereço privado do servidor não foi identificado")
    user = os.environ.get("USER") or Path.home().name
    record.set_message("Ativando o serviço automático após reinicializações…")
    linger_code, linger_output = await run_streaming_command(
        record,
        _admin_command(settings, "enable-user-linger", user),
        timeout=120,
    )
    if linger_code != 0:
        raise RuntimeError(linger_output or "Não foi possível manter o serviço disponível após reiniciar o Linux")
    persist_settings(
        settings,
        allowed_tailscale_login=login,
        external_url=external_url,
        remote_enabled=True,
        start_at_login=True,
    )
    if shutil.which("systemctl"):
        _quick_command(["systemctl", "--user", "enable", settings.service_name], timeout=20)
    record.set_message("Acesso externo privado configurado e serviço preparado para iniciar após o boot.")
    return {**detected, "allowed_login": login, "external_url": external_url, "linger_enabled": True}


async def disable_tailscale_serve(record: SetupTask, settings: Settings) -> Dict[str, Any]:
    record.set_message("Desativando o endereço remoto privado…")
    code, _ = await run_streaming_command(record, _admin_command(settings, "tailscale-serve-reset"), timeout=120)
    if code != 0:
        raise RuntimeError("Não foi possível desativar o Tailscale Serve")
    persist_settings(settings, allowed_tailscale_login="", external_url="", remote_enabled=False)
    record.set_message("Acesso externo desativado; o uso local continua disponível.")
    return {"remote_enabled": False}


async def install_deb_update(record: SetupTask, settings: Settings, package_path: Path) -> Dict[str, Any]:
    record.set_message("Autorize a atualização na janela do sistema…")
    code, output = await run_streaming_command(
        record,
        _admin_command(settings, "install-deb", str(package_path)),
        timeout=1800,
    )
    if code != 0:
        raise RuntimeError("A atualização do pacote .deb não foi concluída")
    record.set_message("Atualização instalada. O aplicativo será reiniciado.")
    return {"package": str(package_path), "output": output[-4000:]}


def set_autostart(settings: Settings, enabled: bool) -> Dict[str, Any]:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise RuntimeError("O gerenciador de serviços systemd não está disponível")
    action = "enable" if enabled else "disable"
    code, output = _quick_command([systemctl, "--user", action, settings.service_name], timeout=20)
    if code != 0:
        raise RuntimeError(output or "Não foi possível alterar a inicialização automática")
    persist_settings(settings, start_at_login=enabled)
    return service_state(settings)


def schedule_service_restart(settings: Settings, delay_seconds: float = 1.5) -> None:
    command = f"sleep {max(delay_seconds, 0.5):.1f}; systemctl --user restart {shlex.quote(settings.service_name)}"
    subprocess.Popen(  # noqa: S603 - command is fixed and service name is package-controlled
        ["sh", "-c", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def application_logs(settings: Settings, lines: int = 250) -> str:
    lines = max(20, min(lines, 1000))
    journalctl = shutil.which("journalctl")
    if not journalctl:
        return "O journalctl não está disponível neste sistema."
    code, output = _quick_command(
        [journalctl, "--user", "-u", settings.service_name, "-n", str(lines), "--no-pager", "-o", "short-iso"],
        timeout=20,
    )
    return output if output else f"Nenhum registro disponível (código {code})."


def diagnostic_report(settings: Settings) -> str:
    state = system_state(settings)
    state["logs"] = application_logs(settings, 180)
    return json.dumps(state, ensure_ascii=False, indent=2) + "\n"

# ---------------------------------------------------------------------------
# Complete graphical experience: developer tools, Playwright and desktop I/O
# ---------------------------------------------------------------------------


def _managed_node_binary(settings: Settings) -> Path:
    return settings.resolved_tools_dir / "node" / "bin" / "node"


def _managed_npm_binary(settings: Settings) -> Path:
    return settings.resolved_tools_dir / "node" / "bin" / "npm"


def full_experience_state(settings: Settings) -> Dict[str, Any]:
    node = _managed_node_binary(settings)
    playwright_cli = settings.resolved_tools_dir / "playwright" / "node_modules" / "@playwright" / "mcp" / "cli.js"
    browsers = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "codex-linux-control" / "ms-playwright"
    desktop_backend = ""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").casefold()
    if session_type == "x11" and shutil.which("xdotool"):
        desktop_backend = "xdotool"
    else:
        socket = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / ".ydotool_socket"
        if shutil.which("ydotool") and socket.exists():
            desktop_backend = "ydotool"
    return {
        "installed": bool(settings.full_experience_installed and node.is_file() and playwright_cli.is_file()),
        "node": {
            "installed": node.is_file() and os.access(node, os.X_OK),
            "path": str(node),
            "version": _quick_command([str(node), "--version"])[1] if node.is_file() else "",
        },
        "playwright": {
            "installed": playwright_cli.is_file(),
            "path": str(playwright_cli),
            "browsers_path": str(browsers),
            "browser_downloaded": browsers.exists() and any(browsers.iterdir()) if browsers.exists() else False,
            "enabled": settings.browser_control_enabled,
        },
        "desktop": {
            "enabled": settings.desktop_control_enabled,
            "session_type": session_type or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11" if os.environ.get("DISPLAY") else "unknown"),
            "input_backend": desktop_backend or "unavailable",
            "at_spi": bool(shutil.which("gdbus")) and _python_module_available("pyatspi"),
            "screenshot": next((name for name in ("grim", "gnome-screenshot", "scrot", "import") if shutil.which(name)), "unavailable"),
            "uinput_present": Path("/dev/uinput").exists(),
            "uinput_access": os.access("/dev/uinput", os.R_OK | os.W_OK),
            "ydotoold_active": _user_service_active("codex-linux-control-ydotoold.service"),
        },
        "packages": {
            "git": bool(shutil.which("git")),
            "bubblewrap": bool(shutil.which("bwrap")),
            "ripgrep": bool(shutil.which("rg")),
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "chromium": bool(shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")),
        },
        "remote_workspace": {
            "enabled": settings.remote_desktop_enabled,
            "xvnc": shutil.which("Xtigervnc") or shutil.which("Xvnc") or "",
            "novnc": next((str(path) for path in (Path("/usr/share/novnc"), Path("/usr/share/noVNC"), Path("/usr/lib/novnc")) if (path / "vnc.html").is_file()), ""),
            "openbox": shutil.which("openbox-session") or "",
            "xauth": shutil.which("xauth") or "",
            "private_socket_supported": bool(shutil.which("Xtigervnc") or shutil.which("Xvnc")),
        },
    }


def _python_module_available(name: str) -> bool:
    try:
        return subprocess.run(
            [shutil.which("python3") or "python3", "-c", f"import {name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _user_service_active(name: str) -> bool:
    if not shutil.which("systemctl"):
        return False
    code, output = _quick_command(["systemctl", "--user", "is-active", name], timeout=5)
    return code == 0 and output == "active"


def _write_bundled_skills(settings: Settings) -> None:
    root = Path.home() / ".codex" / "skills"
    browser = root / "clc-browser"
    desktop = root / "clc-desktop"
    system_admin = root / "clc-system-admin"
    browser.mkdir(parents=True, exist_ok=True)
    desktop.mkdir(parents=True, exist_ok=True)
    system_admin.mkdir(parents=True, exist_ok=True)

    (browser / "SKILL.md").write_text(
        """---\nname: clc-browser\ndescription: Navegação, automação e teste de páginas web com o Playwright MCP gerenciado pelo Codex Linux Control.\n---\n\n# Navegador supervisionado\n\nUse o servidor MCP `playwright` para abrir páginas, inspecionar acessibilidade/DOM, console e rede, preencher formulários e testar aplicações.\n\nRegras obrigatórias:\n- O pedido de navegar, acessar ou testar uma página já autoriza as ações reversíveis de navegador diretamente necessárias. Informe o próximo passo e prossiga sem pedir uma aprovação redundante. Peça nova confirmação apenas para compras, pagamentos, envios, publicações, alterações irreversíveis ou ações externas sensíveis não abrangidas pelo pedido.\n- O processo Chromium é compartilhado, mas cada conversa recebe um `BrowserContext` isolado. Páginas, workers e caches continuam separados; cookies, localStorage e IndexedDB autenticados são persistidos em um estado-base protegido e ficam disponíveis aos novos contextos de todas as conversas e projetos.\n- Sempre que houver atividade Playwright, o cartão do navegador na conversa oferece **Ver e controlar ao vivo**. O visor mostra também o cursor decorado das ações executadas pelo Playwright.\n- Prefira snapshots de acessibilidade e seletores determinísticos.\n- Use a captura de tela quando o estado visual for relevante.\n- Não confirme compras, pagamentos, contratos, publicações, mensagens ou alterações irreversíveis sem aprovação explícita do usuário.\n- Não reutilize credenciais ou copie segredos para a conversa.\n- Explique o próximo passo antes de uma ação externa sensível.\n- O perfil do navegador é persistente e exclusivo deste aplicativo.\n""",
        encoding="utf-8",
    )
    (browser / "SKILL.json").write_text(
        json.dumps({"interface": {"displayName": "Navegador Playwright", "shortDescription": "Navegação e testes web supervisionados"}, "dependencies": {"tools": [{"type": "mcp", "value": "playwright"}]}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (desktop / "SKILL.md").write_text(
        """---\nname: clc-desktop\ndescription: Inspeção e controle supervisionado da interface gráfica Linux pelo MCP do Codex Linux Control.\n---\n\n# Desktop Linux supervisionado\n\nUse o servidor MCP `linux_desktop` para ler a árvore AT-SPI, listar janelas, capturar a tela, focalizar janelas, abrir aplicativos, clicar, digitar, usar atalhos ou alterar a área de transferência. Quando o pedido do usuário exigir controle da interface, esse pedido já é a autorização para as ações reversíveis diretamente necessárias: descreva-as e prossiga sem pedir novamente. Solicite nova confirmação somente para ações externas sensíveis, destrutivas, irreversíveis ou fora do escopo.\n\nProcedimento:\n1. Leia `desktop_status`.\n2. Prefira `desktop_accessibility_tree` e `desktop_list_windows`; use coordenadas somente quando necessário.\n3. Capture a tela antes e depois de ações visuais importantes.\n4. Descreva claramente qualquer ação de entrada.\n5. Nunca envie, publique, compre, exclua ou confirme algo irreversível sem autorização explícita.\n6. Interrompa se a janela esperada, o texto ou o resultado não corresponder ao planejado.\n\nEste recurso aproxima o Computer Use no Linux, mas não é o Computer Use oficial da OpenAI.\n""",
        encoding="utf-8",
    )
    (desktop / "SKILL.json").write_text(
        json.dumps({"interface": {"displayName": "Controle do desktop Linux", "shortDescription": "Tela, janelas, mouse e teclado com aprovação"}, "dependencies": {"tools": [{"type": "mcp", "value": "linux_desktop"}]}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (system_admin / "SKILL.md").write_text(
        """---
name: clc-system-admin
description: Administração integral do computador Linux pelo Codex do Sistema.
---

# Administração do sistema

Esta skill só deve ser usada no perfil **Sistema + Projetos**. O usuário de serviço
do Sistema possui `sudo` sem senha, conforme escolha explícita feita na instalação.

Regras:
- leia estado e logs antes de modificar serviços;
- faça backup antes de alterações de risco;
- valide o fluxo real depois de cada mudança;
- confirme com o operador antes de apagar dados, formatar discos, reiniciar ou desligar.
""",
        encoding="utf-8",
    )
    (system_admin / "SKILL.json").write_text(
        json.dumps({"interface": {"displayName": "Administração do sistema", "shortDescription": "Administração Linux integral com sudo"}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for path in (
        browser / "SKILL.md", browser / "SKILL.json",
        desktop / "SKILL.md", desktop / "SKILL.json",
        system_admin / "SKILL.md", system_admin / "SKILL.json",
    ):
        os.chmod(path, 0o600)


async def _install_managed_node(record: SetupTask, settings: Settings) -> Dict[str, Any]:
    tools = settings.resolved_tools_dir
    tools.mkdir(parents=True, exist_ok=True)
    machine = os.uname().machine.casefold()
    arch = "x64" if machine in {"x86_64", "amd64"} else "arm64" if machine in {"aarch64", "arm64"} else ""
    if not arch:
        raise RuntimeError(f"Arquitetura ainda não suportada para o Node gerenciado: {machine}")

    with tempfile.TemporaryDirectory(prefix="clc-node-") as temporary:
        temp = Path(temporary)
        sums = temp / "SHASUMS256.txt"
        record.set_message("Identificando a versão LTS atual do Node.js…")
        code, _ = await run_streaming_command(record, ["curl", "-fsSL", "https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt", "-o", str(sums)], timeout=120)
        if code != 0:
            raise RuntimeError("Não foi possível consultar a versão LTS do Node.js")
        pattern = re.compile(rf"^([0-9a-f]{{64}})\s+(node-v[^\s]+-linux-{arch}\.tar\.xz)$")
        selected: tuple[str, str] | None = None
        for line in sums.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line.strip())
            if match:
                selected = (match.group(1), match.group(2))
                break
        if not selected:
            raise RuntimeError("O pacote LTS do Node.js para esta arquitetura não foi localizado")
        expected, filename = selected
        archive = temp / filename
        url = f"https://nodejs.org/dist/latest-v24.x/{filename}"
        record.set_message("Baixando o ambiente de automação do navegador…")
        code, _ = await run_streaming_command(record, ["curl", "-fL", url, "-o", str(archive)], timeout=600)
        if code != 0:
            raise RuntimeError("Falha ao baixar o Node.js")
        actual = __import__("hashlib").sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("A verificação de integridade do Node.js falhou")
        extract = temp / "extract"
        extract.mkdir()
        code, _ = await run_streaming_command(record, ["tar", "-xJf", str(archive), "-C", str(extract)], timeout=300)
        if code != 0:
            raise RuntimeError("Não foi possível extrair o Node.js")
        source = next(extract.iterdir())
        destination = tools / "node"
        backup = tools / "node.previous"
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            destination.replace(backup)
        shutil.copytree(source, destination, symlinks=True)
        if backup.exists():
            shutil.rmtree(backup)

    node = _managed_node_binary(settings)
    if not node.is_file():
        raise RuntimeError("O Node.js foi extraído, mas o executável não foi localizado")
    return {"node": str(node), "version": _quick_command([str(node), "--version"])[1]}


async def _install_playwright(record: SetupTask, settings: Settings) -> Dict[str, Any]:
    node = _managed_node_binary(settings)
    npm = _managed_npm_binary(settings)
    if not node.is_file() or not npm.is_file():
        await _install_managed_node(record, settings)
    target = settings.resolved_tools_dir / "playwright"
    target.mkdir(parents=True, exist_ok=True)
    package_json = target / "package.json"
    if not package_json.exists():
        package_json.write_text('{"name":"codex-linux-control-tools","private":true}\n', encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{node.parent}:{env.get('PATH', '')}"
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "codex-linux-control" / "ms-playwright")
    record.set_message("Instalando o Playwright e o servidor MCP…")
    code, _ = await run_streaming_command(
        record,
        [str(npm), "install", "--no-audit", "--no-fund", "--save-exact", "@playwright/mcp@latest", "playwright@latest"],
        timeout=1800,
        env=env,
        cwd=target,
    )
    if code != 0:
        raise RuntimeError("A instalação do Playwright MCP falhou")
    playwright_cli = target / "node_modules" / "playwright" / "cli.js"
    record.set_message("Baixando o Chromium controlado pelo aplicativo…")
    code, _ = await run_streaming_command(record, [str(node), str(playwright_cli), "install", "chromium"], timeout=2400, env=env, cwd=target)
    if code != 0:
        raise RuntimeError("O Chromium do Playwright não pôde ser instalado")
    settings.resolved_browser_profile_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_browser_output_dir.mkdir(parents=True, exist_ok=True)
    return {"playwright": str(target), "browsers": env["PLAYWRIGHT_BROWSERS_PATH"]}


async def install_full_experience(record: SetupTask, settings: Settings) -> Dict[str, Any]:
    record.set_message("Instalando os pacotes adicionais do Linux…")
    user = os.environ.get("USER") or Path.home().name
    code, _ = await run_streaming_command(record, _admin_command(settings, "install-full-packages", user), timeout=2400)
    if code != 0:
        raise RuntimeError("Os pacotes adicionais não puderam ser instalados")
    node_result = await _install_managed_node(record, settings)
    browser_result = await _install_playwright(record, settings)
    _write_bundled_skills(settings)

    # A user service gives Wayland a virtual input device without opening a terminal.
    if shutil.which("systemctl"):
        _quick_command(["systemctl", "--user", "daemon-reload"], timeout=20)
        # Ubuntu's generic ydotool user unit can start before the installer has
        # applied the uinput ACL and repeatedly fail.  The application-owned
        # unit uses a private per-user socket and is the only daemon we need.
        _quick_command(["systemctl", "--user", "mask", "--now", "ydotool.service"], timeout=20)
        _quick_command(["systemctl", "--user", "enable", "codex-linux-control-ydotoold.service"], timeout=20)
        _quick_command(["systemctl", "--user", "start", "codex-linux-control-ydotoold.service"], timeout=20)

    persist_settings(
        settings,
        full_experience_installed=True,
        browser_control_enabled=True,
        desktop_control_enabled=True,
        remote_desktop_enabled=True,
        device_auth_required=True,
    )
    record.set_message("Experiência completa instalada. As integrações serão registradas no Codex.")
    return {"node": node_result, "browser": browser_result, "state": full_experience_state(settings)}
