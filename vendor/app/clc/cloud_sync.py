from __future__ import annotations

import argparse
import asyncio
try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - Windows development/test host
    class _FcntlCompat:
        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 8

        @staticmethod
        def flock(_fd: int, _operation: int) -> None:
            # Production runs on Linux and uses the real advisory lock. The
            # Windows compatibility path exists so validation and packaging can
            # import the module; Windows is not a supported sync runtime.
            return None

    fcntl = _FcntlCompat()
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .config import Settings, get_settings, persist_settings
from .projects import ProjectStore
from .setup_ops import SetupTask, choose_directory, run_streaming_command


_REMOTE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")
_SAFE_SEGMENT_RE = re.compile(r"[^\w .()\-]+", re.UNICODE)
_PROVIDER_TYPES = {"google_drive": "drive", "onedrive": "onedrive"}
_PROVIDER_LABELS = {"google_drive": "Google Drive", "onedrive": "Microsoft OneDrive"}
_MIN_RCLONE_VERSION = (1, 71, 0)
_MIN_RCLONE_VERSION_TEXT = ".".join(str(item) for item in _MIN_RCLONE_VERSION)
_AUTO_ANSWERS = {
    "config_is_local": "true",
    "config_drive_ok": "true",
}


@dataclass
class CloudConfigSession:
    id: str
    provider: str
    remote_name: str
    base_options: Dict[str, str]
    state: str = ""
    question: Optional[Dict[str, Any]] = None
    complete: bool = False
    error: str = ""
    restart_required: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def new(cls, provider: str, remote_name: str, base_options: Dict[str, str]) -> "CloudConfigSession":
        now = time.time()
        return cls(
            id=uuid.uuid4().hex,
            provider=provider,
            remote_name=remote_name,
            base_options=dict(base_options),
            created_at=now,
            updated_at=now,
        )

    def public_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # OAuth client secrets and similar values must never be returned to the browser.
        safe_options = {}
        for key, value in self.base_options.items():
            safe_options[key] = "••••••••" if "secret" in key.casefold() and value else value
        data["base_options"] = safe_options
        return data


class CloudSyncManager:
    """Graphical rclone configuration and authoritative local-to-cloud redundancy."""

    def __init__(self, settings: Settings, projects: Optional[ProjectStore] = None) -> None:
        self.settings = settings
        self.projects = projects
        self._session = self._load_session()

    # ------------------------------------------------------------------
    # Paths, command environment and state
    # ------------------------------------------------------------------

    @property
    def rclone_binary(self) -> str:
        return shutil.which("rclone") or ""

    def _common_args(self) -> list[str]:
        binary = self.rclone_binary
        if not binary:
            raise RuntimeError("O componente de sincronização rclone ainda não está instalado")
        compatible, version = self._rclone_compatible()
        if not compatible:
            installed = version or "versão desconhecida"
            raise RuntimeError(
                f"O rclone instalado ({installed}) é antigo para a sincronização protegida. "
                f"Atualize graficamente para a versão {_MIN_RCLONE_VERSION_TEXT} ou superior."
            )
        self._ensure_secret_material()
        return [
            binary,
            "--config",
            str(self.settings.resolved_rclone_config_file),
            "--password-command",
            str(self.settings.resolved_rclone_password_command),
            "--ask-password=false",
        ]

    def _ensure_secret_material(self) -> None:
        config_dir = self.settings.resolved_config_dir
        config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(config_dir, 0o700)
        password_file = self.settings.resolved_rclone_password_file
        if not password_file.exists():
            password_file.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
        os.chmod(password_file, 0o600)
        command_file = self.settings.resolved_rclone_password_command
        script = f"#!/bin/sh\nexec /bin/cat {shlex_quote(str(password_file))}\n"
        if not command_file.exists() or command_file.read_text(encoding="utf-8", errors="replace") != script:
            command_file.write_text(script, encoding="utf-8")
        os.chmod(command_file, 0o700)
        config_file = self.settings.resolved_rclone_config_file
        if not config_file.exists():
            config_file.touch(mode=0o600)
        os.chmod(config_file, 0o600)

    def _session_path(self) -> Path:
        return self.settings.resolved_cloud_config_session_file

    def _load_session(self) -> Optional[CloudConfigSession]:
        try:
            raw = json.loads(self._session_path().read_text(encoding="utf-8"))
            return CloudConfigSession(**raw) if isinstance(raw, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            return None

    def _save_session(self, session: Optional[CloudConfigSession]) -> None:
        path = self._session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if session is None:
            path.unlink(missing_ok=True)
            self._session = None
            return
        # The active in-memory session may contain an OAuth client secret and an
        # opaque continuation state. Neither is persisted. If the service is
        # restarted mid-authorization, the user restarts the graphical flow.
        persisted = asdict(session)
        persisted["base_options"] = {
            key: ("" if "secret" in key.casefold() else value)
            for key, value in session.base_options.items()
        }
        if not session.complete:
            persisted["state"] = ""
            persisted["question"] = None
            persisted["restart_required"] = True
            persisted["error"] = (
                "A autorização foi interrompida. Selecione Conectar novamente; "
                "nenhum segredo de autorização foi salvo fora da configuração criptografada."
            )
        else:
            persisted["state"] = ""
            persisted["question"] = None
            persisted["restart_required"] = False
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(persisted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
        self._session = session

    def _read_status(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.settings.resolved_cloud_status_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write_status(self, **values: Any) -> Dict[str, Any]:
        status = self._read_status()
        status.update(values)
        path = self.settings.resolved_cloud_status_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
        return status

    def _quick(self, args: Iterable[str], timeout: float = 30) -> tuple[int, str]:
        try:
            result = subprocess.run(
                [str(item) for item in args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        return int(result.returncode), result.stdout.strip()

    def _remote_exists(self, remote_name: str) -> bool:
        if not self.rclone_binary or not self._rclone_compatible()[0] or not _REMOTE_RE.fullmatch(remote_name):
            return False
        try:
            code, output = self._quick([*self._common_args(), "listremotes"], timeout=15)
        # State collection feeds the whole Dex bootstrap. A stale or
        # administratively restored credential can be unreadable until its
        # ownership is repaired; report the remote as unavailable instead of
        # turning an optional cloud-sync check into a 500 for /api/status.
        except (OSError, RuntimeError):
            return False
        return code == 0 and f"{remote_name}:" in {line.strip() for line in output.splitlines()}

    def _timer_state(self) -> Dict[str, Any]:
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return {"available": False, "enabled": False, "active": False}
        enabled, enabled_text = self._quick([systemctl, "--user", "is-enabled", self.settings.cloud_sync_timer_name], 10)
        active, active_text = self._quick([systemctl, "--user", "is-active", self.settings.cloud_sync_timer_name], 10)
        return {
            "available": True,
            "enabled": enabled == 0 and enabled_text == "enabled",
            "active": active == 0 and active_text == "active",
            "enabled_text": enabled_text,
            "active_text": active_text,
            "interval_minutes": self.settings.cloud_sync_interval_minutes,
        }

    def state(self) -> Dict[str, Any]:
        binary = self.rclone_binary
        version = ""
        compatible = False
        parsed_version: tuple[int, ...] = ()
        if binary:
            code, output = self._quick([binary, "version"], 10)
            if code == 0 and output:
                version = output.splitlines()[0]
                parsed_version = _parse_rclone_version(version)
                compatible = parsed_version >= _MIN_RCLONE_VERSION
        local = self.settings.resolved_cloud_local_path
        remote_name = self.settings.cloud_remote_name
        configured = bool(compatible and remote_name and self._remote_exists(remote_name))
        return {
            "installed": bool(binary),
            "compatible": compatible,
            "binary": binary,
            "version": version,
            "parsed_version": list(parsed_version),
            "minimum_version": _MIN_RCLONE_VERSION_TEXT,
            "provider": self.settings.cloud_provider,
            "provider_label": _PROVIDER_LABELS.get(self.settings.cloud_provider, ""),
            "remote_name": remote_name,
            "remote_path": self.settings.cloud_remote_path,
            "local_path": str(local),
            "local_exists": local.is_dir(),
            "configured": configured,
            "enabled": self.settings.cloud_sync_enabled,
            "initialized": self.settings.cloud_sync_initialized,
            "interval_minutes": self.settings.cloud_sync_interval_minutes,
            "filter_profile": self.settings.cloud_filter_profile,
            "session": self._session.public_dict() if self._session else None,
            "status": self._read_status(),
            "timer": self._timer_state(),
            "folder_message": f"Todos os projetos sincronizados ficam em {local}",
        }

    # ------------------------------------------------------------------
    # Installation and OAuth configuration
    # ------------------------------------------------------------------

    async def install(self, record: SetupTask) -> Dict[str, Any]:
        compatible, version = self._rclone_compatible()
        if compatible:
            record.set_message(f"Rclone {version or ''} já está pronto.")
            return self.state()
        helper = Path(self.settings.privileged_helper)
        pkexec = shutil.which("pkexec")
        if not pkexec or not helper.is_file():
            raise RuntimeError("O instalador administrativo do pacote não foi encontrado")
        record.set_message("Autorize a instalação ou atualização do sincronizador na janela do Linux…")
        code, output = await run_streaming_command(
            record,
            [pkexec, str(helper), "install-cloud-packages"],
            timeout=1200,
        )
        compatible, version = self._rclone_compatible()
        if code != 0 or not compatible:
            raise RuntimeError(output[-1000:] or "A instalação do rclone não foi concluída")
        record.set_message(f"Sincronizador rclone {version or ''} instalado e verificado.")
        return self.state()

    def _rclone_compatible(self) -> tuple[bool, str]:
        binary = self.rclone_binary
        if not binary:
            return False, ""
        code, output = self._quick([binary, "version"], 10)
        first = output.splitlines()[0].strip() if code == 0 and output else ""
        return _parse_rclone_version(first) >= _MIN_RCLONE_VERSION, first

    async def _ensure_encrypted_config(self, record: SetupTask) -> None:
        self._ensure_secret_material()
        common = self._common_args()
        config_file = self.settings.resolved_rclone_config_file
        try:
            if config_file.read_text(encoding="utf-8", errors="replace").lstrip().startswith("RCLONE_ENCRYPT_V0:"):
                return
        except OSError:
            pass
        record.set_message("Protegendo as credenciais locais da nuvem…")
        code, output = await run_streaming_command(
            record,
            [*common, "config", "encryption", "set"],
            timeout=120,
        )
        if code != 0:
            raise RuntimeError(output[-1000:] or "Não foi possível criptografar a configuração da nuvem")

    async def start_configuration(
        self,
        record: SetupTask,
        *,
        provider: str,
        client_id: str = "",
        client_secret: str = "",
    ) -> Dict[str, Any]:
        provider = provider.strip().casefold()
        if provider not in _PROVIDER_TYPES:
            raise RuntimeError("Escolha Google Drive ou Microsoft OneDrive")
        if provider == "google_drive" and not client_id.strip():
            raise RuntimeError("Informe o Client ID do Google para continuar")
        await self._ensure_encrypted_config(record)
        remote_name = "clc_google_drive" if provider == "google_drive" else "clc_onedrive"
        options: Dict[str, str] = {"config_is_local": "true"}
        if provider == "google_drive":
            options.update({"client_id": client_id.strip(), "client_secret": client_secret.strip(), "scope": "drive"})
        else:
            if client_id.strip():
                options["client_id"] = client_id.strip()
            if client_secret.strip():
                options["client_secret"] = client_secret.strip()

        # Replace only the application-owned remote; unrelated rclone remotes are untouched.
        if self._remote_exists(remote_name):
            await run_streaming_command(record, [*self._common_args(), "config", "delete", remote_name], timeout=60)
        session = CloudConfigSession.new(provider, remote_name, options)
        self._save_session(session)
        return await self._advance_configuration(record, session, answer=None, initial=True)

    async def answer_configuration(self, record: SetupTask, session_id: str, answer: str) -> Dict[str, Any]:
        session = self._session
        if not session or session.id != session_id:
            raise RuntimeError("A sessão de autorização expirou; inicie novamente")
        if session.complete:
            return {"session": session.public_dict(), "cloud": self.state()}
        if session.restart_required:
            raise RuntimeError("A autorização foi interrompida; selecione Conectar novamente")
        if not session.state:
            raise RuntimeError("A autorização não está aguardando uma resposta")
        return await self._advance_configuration(record, session, answer=answer, initial=False)

    async def _advance_configuration(
        self,
        record: SetupTask,
        session: CloudConfigSession,
        *,
        answer: Optional[str],
        initial: bool,
    ) -> Dict[str, Any]:
        common = self._common_args()
        sensitive = [value for key, value in session.base_options.items() if "secret" in key.casefold() and value]
        # Continuation state is opaque, and an answer can itself be a password.
        # Keep both out of the graphical task log even though they only exist
        # transiently in memory during the authorization flow.
        if session.state:
            sensitive.append(session.state)
        if answer is not None and session.question and bool(session.question.get("IsPassword")):
            sensitive.append(str(answer))
        attempts = 0
        next_answer = answer
        while attempts < 8:
            attempts += 1
            option_args: list[str] = []
            for key, value in session.base_options.items():
                if value != "":
                    option_args.extend([key, value])
            if initial:
                command = [
                    *common,
                    "config",
                    "create",
                    session.remote_name,
                    _PROVIDER_TYPES[session.provider],
                    *option_args,
                    "--non-interactive",
                ]
                initial = False
            else:
                if next_answer is None:
                    raise RuntimeError("A resposta da configuração não foi informada")
                command = [
                    *common,
                    "config",
                    "update",
                    session.remote_name,
                    *option_args,
                    "--continue",
                    "--state",
                    session.state,
                    "--result",
                    str(next_answer),
                    "--non-interactive",
                ]
            record.set_message("Conclua a autorização na página do provedor quando ela abrir…")
            code, output = await run_streaming_command(
                record,
                command,
                timeout=1200,
                redact=sensitive,
            )
            parsed = _extract_json_object(output)
            if parsed:
                error = str(parsed.get("Error") or "").strip()
                state = str(parsed.get("State") or "")
                option = parsed.get("Option") if isinstance(parsed.get("Option"), dict) else None
                session.state = state
                session.question = option
                session.error = error
                session.updated_at = time.time()
                if not state:
                    session.complete = True
                    session.question = None
                    self._save_session(session)
                    persist_settings(
                        self.settings,
                        cloud_provider=session.provider,
                        cloud_remote_name=session.remote_name,
                        cloud_local_path=str(self.settings.resolved_cloud_local_path),
                    )
                    record.set_message(f"{_PROVIDER_LABELS[session.provider]} conectado.")
                    return {"session": session.public_dict(), "cloud": self.state()}
                self._save_session(session)
                name = str(option.get("Name") or "") if option else ""
                if name in _AUTO_ANSWERS:
                    next_answer = _AUTO_ANSWERS[name]
                    continue
                record.set_message("A conta foi autorizada. Responda à próxima opção na tela do aplicativo.")
                return {"session": session.public_dict(), "cloud": self.state()}
            if code == 0 and self._remote_exists(session.remote_name):
                session.complete = True
                session.question = None
                session.state = ""
                session.error = ""
                session.updated_at = time.time()
                self._save_session(session)
                persist_settings(
                    self.settings,
                    cloud_provider=session.provider,
                    cloud_remote_name=session.remote_name,
                    cloud_local_path=str(self.settings.resolved_cloud_local_path),
                )
                record.set_message(f"{_PROVIDER_LABELS[session.provider]} conectado.")
                return {"session": session.public_dict(), "cloud": self.state()}
            raise RuntimeError(output[-1500:] or "O provedor não concluiu a autorização")
        raise RuntimeError("A configuração do provedor excedeu o número de etapas esperado")

    # ------------------------------------------------------------------
    # Folder selection and project consolidation
    # ------------------------------------------------------------------

    async def choose_local_folder(self) -> Path:
        selected = await choose_directory("Escolha a pasta central dos projetos sincronizados")
        self.configure_paths(str(selected), self.settings.cloud_remote_path)
        return selected

    def configure_paths(self, local_path: str, remote_path: str) -> Dict[str, Any]:
        local = _validate_local_root(local_path, self.settings)
        remote = _normalize_remote_path(remote_path)
        local.mkdir(parents=True, exist_ok=True)
        os.chmod(local, 0o700)
        persist_settings(
            self.settings,
            cloud_local_path=str(local),
            cloud_remote_path=remote,
        )
        if self.projects:
            self.projects.allow_root(local)
        return self.state()

    def list_remote_folders(self, path: str = "") -> Dict[str, Any]:
        """List direct child folders without exposing tokens or rclone config."""
        state = self.state()
        remote_name = state.get("remote_name", "")
        if not state.get("configured") or not remote_name:
            raise RuntimeError("Conclua primeiro o login da conta de nuvem")
        current = str(path or "").strip().strip("/")
        if current:
            current = _normalize_remote_path(current)
        target = f"{remote_name}:{current}" if current else f"{remote_name}:"
        code, output = self._quick(
            [*self._common_args(), "lsf", target, "--dirs-only", "--format", "p", "--max-depth", "1"],
            timeout=90,
        )
        if code != 0:
            raise RuntimeError(output[-1200:] or "Não foi possível listar as pastas")
        folders = []
        for raw in output.splitlines():
            name = raw.strip().strip("/")
            if not name or "/" in name or name in {".", ".."}:
                continue
            child = f"{current}/{name}" if current else name
            folders.append({"name": name, "path": child})
        folders.sort(key=lambda item: item["name"].casefold())
        parent = current.rsplit("/", 1)[0] if "/" in current else ""
        return {"current": current, "parent": parent, "folders": folders, "remote_name": remote_name}

    def create_remote_folder(self, parent: str, name: str) -> Dict[str, Any]:
        state = self.state()
        remote_name = state.get("remote_name", "")
        if not state.get("configured") or not remote_name:
            raise RuntimeError("Conclua primeiro o login da conta de nuvem")
        clean_parent = str(parent or "").strip().strip("/")
        if clean_parent:
            clean_parent = _normalize_remote_path(clean_parent)
        if not str(name or "").strip():
            raise ValueError("Informe o nome da pasta")
        clean_name = _safe_folder_name(name)
        if not clean_name or clean_name in {".", ".."}:
            raise ValueError("Nome de pasta inválido")
        selected = f"{clean_parent}/{clean_name}" if clean_parent else clean_name
        selected = _normalize_remote_path(selected)
        code, output = self._quick([*self._common_args(), "mkdir", f"{remote_name}:{selected}"], timeout=90)
        if code != 0:
            raise RuntimeError(output[-1200:] or "Não foi possível criar a pasta")
        return {"created": selected, **self.list_remote_folders(clean_parent)}

    async def consolidate_projects(self, record: SetupTask) -> Dict[str, Any]:
        if not self.projects:
            raise RuntimeError("O cadastro de projetos não está disponível")
        root = self.settings.resolved_cloud_local_path
        root.mkdir(parents=True, exist_ok=True)
        self.projects.allow_root(root)
        moved: list[Dict[str, str]] = []
        skipped: list[Dict[str, str]] = []
        for project in list(self.projects.list()):
            source = Path(project.path).expanduser().resolve()
            if source == root or _is_relative_to(source, root):
                skipped.append({"name": project.name, "path": str(source), "reason": "já está na pasta sincronizada"})
                continue
            base = _safe_folder_name(project.name or source.name or "Projeto")
            destination = _unique_destination(root, base)
            staging = root / f".clc-import-{uuid.uuid4().hex}"
            record.set_message(f"Copiando {project.name} para a pasta sincronizada…")
            record.append(f"Copiando projeto: {source} → {destination}")
            try:
                await asyncio.to_thread(
                    shutil.copytree,
                    source,
                    staging,
                    symlinks=True,
                    copy_function=shutil.copy2,
                )
                staging.replace(destination)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            relocated = self.projects.relocate(project.id, str(destination))
            moved.append({"id": relocated.id, "name": relocated.name, "source": str(source), "path": relocated.path})
        self.projects.allow_root(root)
        record.set_message("Projetos consolidados. Os arquivos originais não foram apagados.")
        return {"moved": moved, "skipped": skipped, "local_path": str(root)}

    # ------------------------------------------------------------------
    # Initial and scheduled synchronization
    # ------------------------------------------------------------------

    def _write_filters(self) -> Path:
        path = self.settings.resolved_cloud_filters_file
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.settings.cloud_filter_profile == "complete":
            content = "+ **\n"
        else:
            content = """# Perfil seguro para projetos de código\n- **/.git/**\n- **/node_modules/**\n- **/.next/**\n- **/.turbo/**\n- **/.venv/**\n- **/venv/**\n- **/__pycache__/**\n- **/.pytest_cache/**\n- **/.mypy_cache/**\n- **/.ruff_cache/**\n- **/.cache/**\n- **/dist/**\n- **/build/**\n- **/out/**\n- **/coverage/**\n- **/Backup Conversas Codex/**\n- **/*.pyc\n- **/*.log\n- **/.env\n- **/.env.*\n- **/*.pem\n- **/*.key\n- **/id_rsa\n- **/id_rsa.*\n- **/*.sqlite-journal\n- **/*.sqlite-wal\n+ **\n"""
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def _sync_paths(self) -> tuple[Path, str]:
        local = self.settings.resolved_cloud_local_path
        remote_name = self.settings.cloud_remote_name
        if not remote_name or not _REMOTE_RE.fullmatch(remote_name) or not self._remote_exists(remote_name):
            raise RuntimeError("Conecte uma conta Google Drive ou OneDrive antes de sincronizar")
        remote_path = _normalize_remote_path(self.settings.cloud_remote_path)
        return local, f"{remote_name}:{remote_path}"

    async def initial_sync(self, record: SetupTask, strategy: str = "path1") -> Dict[str, Any]:
        strategy = strategy.strip().casefold()
        if strategy not in {"path1", "path2", "newer"}:
            raise RuntimeError("Estratégia inicial inválida")
        local, remote = self._sync_paths()
        local.mkdir(parents=True, exist_ok=True)
        marker = local / ".clc-sync-access"
        marker.write_text("Codex Linux Control sync access marker\n", encoding="utf-8")
        os.chmod(marker, 0o600)
        record.set_message("Preparando a pasta remota e verificações de segurança…")
        code, output = await run_streaming_command(record, [*self._common_args(), "mkdir", remote], timeout=120)
        if code != 0:
            raise RuntimeError(output[-1000:] or "Não foi possível criar a pasta remota")
        code, output = await run_streaming_command(
            record,
            [*self._common_args(), "copyto", str(marker), f"{remote}/.clc-sync-access"],
            timeout=120,
        )
        if code != 0:
            raise RuntimeError(output[-1000:] or "Não foi possível validar a gravação na nuvem")
        result = await self._run_copy(record)
        persist_settings(self.settings, cloud_sync_enabled=True, cloud_sync_initialized=True)
        self.set_timer(True, self.settings.cloud_sync_interval_minutes)
        result.update({"local_path": str(local), "remote_path": remote, "initialized": True})
        record.set_message(f"Sincronização concluída. Todos os projetos ficam em {local}")
        return result

    async def sync_now(self, record: SetupTask) -> Dict[str, Any]:
        if not self.settings.cloud_sync_initialized:
            raise RuntimeError("Execute a primeira sincronização antes")
        return await self._run_copy(record)

    async def _run_copy(self, record: SetupTask) -> Dict[str, Any]:
        """Copy the authoritative local tree to the cloud without remote-to-local mutations."""
        local, remote = self._sync_paths()
        filters = self._write_filters()
        lock_path = self.settings.resolved_cloud_lock_file
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        args = [
            *self._common_args(),
            "copy",
            str(local),
            remote,
            "--filter-from",
            str(filters),
            "--create-empty-src-dirs",
            "--links",
            "--fast-list",
            "--transfers",
            "8",
            "--checkers",
            "16",
            "--retries",
            "5",
            "--low-level-retries",
            "10",
            "--contimeout",
            "30s",
            "--timeout",
            "5m",
            "--stats",
            "2s",
            "--stats-one-line",
            "-v",
        ]
        started = time.time()
        self._write_status(
            running=True,
            started_at=started,
            finished_at=0,
            ok=False,
            error="",
            local_path=str(local),
            remote_path=remote,
            sync_mode="authoritative-local-copy",
            creates_conflict_suffixes=False,
        )
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._write_status(running=False, error="Outra sincronização já está em andamento", finished_at=time.time())
                raise RuntimeError("Outra sincronização já está em andamento") from exc
            try:
                record.set_message("Atualizando a cópia redundante dos projetos no OneDrive…")
                code, output = await run_streaming_command(record, args, timeout=28_800)
                finished = time.time()
                if code != 0:
                    self._write_status(
                        running=False,
                        finished_at=finished,
                        ok=False,
                        error=output[-4000:] or f"rclone encerrou com código {code}",
                    )
                    raise RuntimeError(output[-1500:] or "A cópia redundante não foi concluída")
                status = self._write_status(
                    running=False,
                    finished_at=finished,
                    ok=True,
                    error="",
                    duration_seconds=round(finished - started, 2),
                    output_tail=output[-4000:],
                    sync_mode="authoritative-local-copy",
                    creates_conflict_suffixes=False,
                )
                record.set_message("Cópia redundante dos projetos atualizada.")
                return status
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def set_timer(self, enabled: bool, interval_minutes: int) -> Dict[str, Any]:
        interval = max(1, min(int(interval_minutes), 1440))
        persist_settings(self.settings, cloud_sync_interval_minutes=interval, cloud_sync_enabled=bool(enabled))
        dropin = self.settings.resolved_cloud_timer_dropin
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(
            "[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec={}min\n".format(interval),
            encoding="utf-8",
        )
        os.chmod(dropin, 0o600)
        systemctl = shutil.which("systemctl")
        if systemctl:
            self._quick([systemctl, "--user", "daemon-reload"], 20)
            action = "enable" if enabled else "disable"
            args = [systemctl, "--user", action, "--now", self.settings.cloud_sync_timer_name]
            code, output = self._quick(args, 30)
            if code != 0 and enabled:
                raise RuntimeError(output or "Não foi possível ativar a sincronização automática")
        return self.state()

    def disable(self) -> Dict[str, Any]:
        return self.set_timer(False, self.settings.cloud_sync_interval_minutes)

    def open_local_folder(self) -> Dict[str, Any]:
        local = self.settings.resolved_cloud_local_path
        local.mkdir(parents=True, exist_ok=True)
        opener = shutil.which("xdg-open")
        if not opener:
            raise RuntimeError("O abridor gráfico de pastas não está disponível")
        subprocess.Popen(
            [opener, str(local)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "local_path": str(local)}


def _extract_json_object(output: str) -> Optional[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and ("State" in value or "Option" in value or "Error" in value):
            return value
    return None


def _parse_rclone_version(value: str) -> tuple[int, ...]:
    match = re.search(r"\bv?(\d+)\.(\d+)(?:\.(\d+))?", str(value or ""), flags=re.IGNORECASE)
    if not match:
        return ()
    return tuple(int(item or 0) for item in match.groups())


def _validate_local_root(value: str, settings: Settings) -> Path:
    path = Path(os.path.expanduser(value or str(Path.home() / "CodexProjects"))).resolve()
    forbidden = {
        Path("/"),
        Path.home().resolve(),
        settings.resolved_config_dir,
        settings.resolved_tools_dir,
        settings.resolved_remote_desktop_dir,
    }
    if path in forbidden:
        raise ValueError("Escolha uma pasta exclusiva para os projetos, não a pasta pessoal inteira")
    try:
        path.relative_to(Path.home().resolve())
    except ValueError as exc:
        raise ValueError("A pasta de sincronização precisa estar dentro da sua pasta pessoal") from exc
    return path


def _normalize_remote_path(value: str) -> str:
    clean = str(value or "Codex Linux Control/Projetos").strip().strip("/")
    parts = [item.strip() for item in clean.split("/") if item.strip()]
    if not parts or any(item in {".", ".."} or ":" in item for item in parts):
        raise ValueError("Pasta remota inválida")
    return "/".join(parts)[:500]


def _safe_folder_name(value: str) -> str:
    clean = _SAFE_SEGMENT_RE.sub("-", value).strip(" .-") or "Projeto"
    return clean[:100]


def _unique_destination(root: Path, base: str) -> Path:
    candidate = root / base
    counter = 2
    while candidate.exists():
        candidate = root / f"{base} ({counter})"
        counter += 1
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


async def _run_from_timer() -> int:
    settings = get_settings()
    manager = CloudSyncManager(settings)
    record = SetupTask(id="timer", kind="cloud-sync", title="Sincronização automática")
    try:
        result = await manager.sync_now(record)
    except Exception as exc:  # noqa: BLE001 - journal must receive the reason
        for line in record.logs:
            print(line, flush=True)
        print(f"ERRO: {exc}", flush=True)
        return 1
    for line in record.logs:
        print(line, flush=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincronização segura de projetos do Codex Linux Control")
    parser.add_argument("--run", action="store_true", help="executa uma sincronização agendada")
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return 2
    return asyncio.run(_run_from_timer())


if __name__ == "__main__":
    raise SystemExit(main())
