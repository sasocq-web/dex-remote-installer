from __future__ import annotations

import json
import os
import shlex
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List


PERSISTENT_FIELDS = {
    "install_mode",
    "setup_completed",
    "start_at_login",
    "codex_command",
    "project_codex_command",
    "project_worker_user",
    "project_worker_home",
    "system_workspace_path",
    "allowed_tailscale_login",
    "allow_localhost",
    "require_paired_local",
    "allowed_project_roots",
    "projects_file",
    "session_ttl_seconds",
    "experimental_api",
    "log_level",
    "enable_raw_rpc",
    "external_url",
    "remote_enabled",
    "full_experience_installed",
    "desktop_control_enabled",
    "browser_control_enabled",
    "remote_desktop_enabled",
    "device_auth_required",
    "cloud_sync_enabled",
    "cloud_sync_initialized",
    "cloud_provider",
    "cloud_remote_name",
    "cloud_remote_path",
    "cloud_local_path",
    "cloud_sync_interval_minutes",
    "cloud_filter_profile",
    "control_plane_enabled",
    "entra_enabled",
    "entra_tenant",
    "entra_client_id",
    "entra_allowed_identities",
    "entra_require_mfa",
    "entra_require_phishing_resistant",
    "entra_step_up_seconds",
    "entra_redirect_uri",
    "entra_required_acr",
    "cloudflare_access_enabled",
    "cloudflare_access_team_domain",
    "cloudflare_access_audience",
    "cloudflare_access_allowed_identities",
}

_CONFIG_LOCK = RLock()
_TRUE_VALUES = {"1", "true", "yes", "on", "sim", "s"}
_FALSE_VALUES = {"0", "false", "no", "off", "nao", "não", "n"}


@dataclass
class Settings:
    """Runtime settings loaded from a per-user JSON file and ``CLC_*`` variables.

    This intentionally uses only the Python standard library so the Debian package
    works with the FastAPI/Pydantic versions shipped by supported distributions.
    """

    app_name: str = "Codex Linux Control"
    app_version: str = "0.9.0"
    install_mode: str = "full"
    host: str = "127.0.0.1"
    port: int = 8787
    codex_command: str = "codex app-server"
    project_codex_command: str = "/usr/bin/sudo -n -u codex-worker -- /usr/lib/dex-remote/run-project-app-server"
    project_worker_user: str = "codex-worker"
    project_worker_home: str = "/home/codex-worker"
    system_workspace_path: str = "/home/codex/SystemWorkspace"
    allowed_tailscale_login: str = ""
    allow_localhost: bool = True
    require_paired_local: bool = True
    allowed_project_roots: str = ""
    config_dir: str = ""
    config_file: str = ""
    projects_file: str = ""
    session_ttl_seconds: int = 43_200
    csrf_header_name: str = "X-CLC-CSRF"
    experimental_api: bool = False
    log_level: str = "INFO"
    enable_raw_rpc: bool = False
    setup_completed: bool = False
    start_at_login: bool = True
    external_url: str = ""
    remote_enabled: bool = False
    full_experience_installed: bool = False
    desktop_control_enabled: bool = False
    browser_control_enabled: bool = False
    remote_desktop_enabled: bool = True
    device_auth_required: bool = True
    package_mode: bool = False
    privileged_helper: str = "/usr/lib/dex-remote/clc-admin"
    service_name: str = "codex-linux-control.service"
    cloud_sync_enabled: bool = False
    cloud_sync_initialized: bool = False
    cloud_provider: str = ""
    cloud_remote_name: str = ""
    cloud_remote_path: str = "Codex Linux Control/Projetos"
    cloud_local_path: str = ""
    cloud_sync_interval_minutes: int = 5
    cloud_filter_profile: str = "source"
    cloud_sync_service_name: str = "codex-linux-control-sync.service"
    cloud_sync_timer_name: str = "codex-linux-control-sync.timer"
    control_plane_enabled: bool = True
    control_broker_socket: str = "/run/sasocq-control/broker.sock"
    entra_enabled: bool = True
    entra_tenant: str = ""
    entra_client_id: str = ""
    entra_allowed_identities: str = ""
    entra_require_mfa: bool = True
    entra_require_phishing_resistant: bool = True
    entra_step_up_seconds: int = 900
    entra_redirect_uri: str = ""
    entra_required_acr: str = ""
    cloudflare_access_enabled: bool = False
    cloudflare_access_team_domain: str = ""
    cloudflare_access_audience: str = ""
    cloudflare_access_allowed_identities: str = ""

    @property
    def home(self) -> Path:
        return Path.home()

    @property
    def resolved_config_dir(self) -> Path:
        if self.config_dir:
            return Path(os.path.expanduser(self.config_dir)).resolve()
        return (self.home / ".config" / "codex-linux-control").resolve()

    @property
    def resolved_config_file(self) -> Path:
        if self.config_file:
            return Path(os.path.expanduser(self.config_file)).resolve()
        return self.resolved_config_dir / "config.json"

    @property
    def resolved_projects_file(self) -> Path:
        if self.projects_file:
            return Path(os.path.expanduser(self.projects_file)).resolve()
        return self.resolved_config_dir / "projects.json"

    @property
    def resolved_tool_profiles_file(self) -> Path:
        return self.resolved_config_dir / "tool-profiles.json"

    @property
    def resolved_tools_dir(self) -> Path:
        return (self.home / ".local" / "share" / "codex-linux-control" / "tools").resolve()

    @property
    def resolved_browser_profile_dir(self) -> Path:
        return (self.home / ".local" / "share" / "codex-linux-control" / "browser-profile").resolve()

    @property
    def resolved_browser_output_dir(self) -> Path:
        return (self.home / ".local" / "share" / "codex-linux-control" / "browser-output").resolve()

    @property
    def resolved_devices_file(self) -> Path:
        return self.resolved_config_dir / "devices.json"

    @property
    def resolved_remote_desktop_dir(self) -> Path:
        return (self.home / ".local" / "share" / "codex-linux-control" / "remote-desktop").resolve()

    @property
    def resolved_remote_browser_profile_dir(self) -> Path:
        return (self.home / ".local" / "share" / "codex-linux-control" / "remote-browser-profile").resolve()

    @property
    def resolved_cloud_local_path(self) -> Path:
        if self.cloud_local_path:
            return Path(os.path.expanduser(self.cloud_local_path)).resolve()
        shared = Path("/srv/sasocq/projects")
        if shared.exists():
            return shared.resolve()
        return (self.home / "CodexProjects").resolve()

    @property
    def resolved_rclone_config_file(self) -> Path:
        return self.resolved_config_dir / "rclone.conf"

    @property
    def resolved_rclone_password_file(self) -> Path:
        return self.resolved_config_dir / "rclone-config-password"

    @property
    def resolved_rclone_password_command(self) -> Path:
        return self.resolved_config_dir / "rclone-password-command"

    @property
    def resolved_cloud_config_session_file(self) -> Path:
        return self.resolved_config_dir / "cloud-config-session.json"

    @property
    def resolved_cloud_status_file(self) -> Path:
        return self.resolved_config_dir / "cloud-sync-status.json"

    @property
    def resolved_cloud_filters_file(self) -> Path:
        return self.resolved_config_dir / "cloud-sync-filters.txt"

    @property
    def resolved_cloud_work_dir(self) -> Path:
        return (self.home / ".local" / "share" / "codex-linux-control" / "cloud-sync-work").resolve()

    @property
    def resolved_cloud_backup_dir(self) -> Path:
        return (self.home / ".local" / "share" / "codex-linux-control" / "cloud-backups" / "local").resolve()

    @property
    def resolved_cloud_lock_file(self) -> Path:
        return (self.home / ".local" / "share" / "codex-linux-control" / "cloud-sync.lock").resolve()

    @property
    def resolved_cloud_timer_dropin(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / f"{self.cloud_sync_timer_name}.d" / "interval.conf"

    @property
    def resolved_playwright_browsers_dir(self) -> Path:
        configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        if configured:
            return Path(os.path.expanduser(configured)).resolve()
        xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", str(self.home / ".cache")))
        return (xdg_cache / "codex-linux-control" / "ms-playwright").resolve()

    @property
    def codex_args(self) -> List[str]:
        args = shlex.split(os.path.expandvars(os.path.expanduser(self.codex_command)))
        if not args:
            raise ValueError("O comando do Codex não pode estar vazio")
        return args

    @property
    def project_codex_args(self) -> List[str]:
        args = shlex.split(os.path.expandvars(os.path.expanduser(self.project_codex_command)))
        if not args:
            raise ValueError("O comando do Codex de projetos não pode estar vazio")
        return args

    @property
    def resolved_system_workspace(self) -> Path:
        return Path(os.path.expanduser(self.system_workspace_path)).resolve()

    @property
    def projects_only(self) -> bool:
        return self.install_mode.strip().casefold() == "projects"

    @property
    def resolved_project_worker_home(self) -> Path:
        return Path(os.path.expanduser(self.project_worker_home)).resolve()

    @property
    def project_roots(self) -> List[Path]:
        raw = self.allowed_project_roots.strip()
        if not raw:
            raw = str(self.home / "CodexProjects")
        roots: List[Path] = []
        for item in raw.split(os.pathsep):
            item = item.strip()
            if not item:
                continue
            roots.append(Path(os.path.expanduser(item)).resolve())
        return roots

    @property
    def tailscale_login_normalized(self) -> str:
        return self.allowed_tailscale_login.strip().casefold()

    @property
    def entra_authority(self) -> str:
        tenant = self.entra_tenant.strip() or "common"
        return f"https://login.microsoftonline.com/{tenant}/v2.0"

    @property
    def entra_allowed(self) -> set[str]:
        return {item.strip().casefold() for item in self.entra_allowed_identities.replace(",", " ").split() if item.strip()}

    @property
    def entra_configured(self) -> bool:
        tenant = self.entra_tenant.strip().casefold()
        return bool(
            self.entra_enabled
            and self.entra_client_id.strip()
            and tenant
            and tenant not in {"common", "consumers", "organizations"}
        )

    @property
    def entra_high_assurance_ready(self) -> bool:
        return bool(
            self.entra_configured
            and self.entra_require_phishing_resistant
            and self.entra_required_acr.strip()
            and self.entra_allowed
        )

    @property
    def cloudflare_access_allowed(self) -> set[str]:
        return {
            item.strip().casefold()
            for item in self.cloudflare_access_allowed_identities.replace(",", " ").split()
            if item.strip()
        }

    @property
    def cloudflare_access_configured(self) -> bool:
        return bool(
            self.cloudflare_access_enabled
            and self.cloudflare_access_team_domain.strip()
            and self.cloudflare_access_audience.strip()
            and self.cloudflare_access_allowed
        )

    @property
    def remote_operator_identity(self) -> str:
        """Identity used to bind pairing tickets and device records."""
        if self.cloudflare_access_configured:
            allowed = sorted(self.cloudflare_access_allowed)
            if len(allowed) == 1:
                return f"cloudflare:{allowed[0]}"
        return self.tailscale_login_normalized

    def persistent_dict(self) -> Dict[str, Any]:
        values = asdict(self)
        return {name: values[name] for name in sorted(PERSISTENT_FIELDS)}


def _default_config_path() -> Path:
    configured = os.environ.get("CLC_CONFIG_FILE", "").strip()
    if configured:
        return Path(os.path.expanduser(configured)).resolve()
    configured_dir = os.environ.get("CLC_CONFIG_DIR", "").strip()
    if configured_dir:
        return Path(os.path.expanduser(configured_dir)).resolve() / "config.json"
    return (Path.home() / ".config" / "codex-linux-control" / "config.json").resolve()


def _read_config_file(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_value(raw: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        value = str(raw).strip().casefold()
        if value in _TRUE_VALUES:
            return True
        if value in _FALSE_VALUES:
            return False
        raise ValueError(f"Valor booleano inválido: {raw!r}")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, str):
        return str(raw)
    return raw


def _settings_values(path: Path, *, include_environment: bool = True) -> Dict[str, Any]:
    defaults = Settings()
    valid_names = {item.name for item in fields(Settings)}
    values: Dict[str, Any] = {}

    for name, raw in _read_config_file(path).items():
        if name not in valid_names:
            continue
        values[name] = _coerce_value(raw, getattr(defaults, name))

    if include_environment:
        # Environment variables deliberately take precedence over the main JSON file.
        for name in valid_names:
            key = f"CLC_{name.upper()}"
            if key in os.environ:
                values[name] = _coerce_value(os.environ[key], getattr(defaults, name))

    values.setdefault("config_file", str(path))
    values.setdefault("config_dir", str(path.parent))
    return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    path = _default_config_path()
    return Settings(**_settings_values(path))


def load_settings(path: Path) -> Settings:
    """Load an independent settings file without affecting the cached main config."""

    resolved = Path(path).expanduser().resolve()
    return Settings(**_settings_values(resolved, include_environment=False))


def persist_settings(settings: Settings, **patch: Any) -> Dict[str, Any]:
    """Persist a validated subset of settings and update the live object."""

    unknown = sorted(set(patch) - PERSISTENT_FIELDS)
    if unknown:
        raise ValueError(f"Configurações não persistíveis: {', '.join(unknown)}")

    defaults = Settings()
    normalized: Dict[str, Any] = {}
    for key, value in patch.items():
        normalized[key] = _coerce_value(value, getattr(defaults, key))

    with _CONFIG_LOCK:
        path = settings.resolved_config_file
        path.parent.mkdir(parents=True, exist_ok=True)
        data = settings.persistent_dict()
        data.update(normalized)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
        for key, value in normalized.items():
            setattr(settings, key, value)
        return data
