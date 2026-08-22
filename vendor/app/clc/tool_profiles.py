from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, Optional


@dataclass
class ToolProfile:
    """Tools explicitly associated with one project or Codex thread."""

    skills: list[dict[str, str]] = field(default_factory=list)
    apps: list[dict[str, str]] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    browser: bool = False
    desktop: bool = False
    system_admin: bool = False
    automatic: bool = True

    @classmethod
    def from_value(cls, raw: Any) -> "ToolProfile":
        if not isinstance(raw, dict):
            return cls()

        skills: list[dict[str, str]] = []
        for item in raw.get("skills") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            path = str(item.get("path") or "").strip()
            if name and path:
                skills.append({"name": name, "path": path})

        apps: list[dict[str, str]] = []
        for item in raw.get("apps") or []:
            if not isinstance(item, dict):
                continue
            app_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or app_id).strip()
            slug = str(item.get("slug") or app_id).strip()
            if app_id:
                apps.append({"id": app_id, "name": name, "slug": slug})

        mcp_servers = sorted({str(value).strip() for value in raw.get("mcp_servers") or [] if str(value).strip()})
        return cls(
            skills=_dedupe_dicts(skills, "path"),
            apps=_dedupe_dicts(apps, "id"),
            mcp_servers=mcp_servers,
            browser=bool(raw.get("browser", False)),
            desktop=bool(raw.get("desktop", False)),
            system_admin=bool(raw.get("system_admin", False)),
            automatic=bool(raw.get("automatic", True)),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def merge(self, override: "ToolProfile") -> "ToolProfile":
        """Return project defaults plus thread choices.

        Thread profiles are stored as complete choices in the UI, but merging keeps
        project defaults useful for conversations created before a thread-specific
        profile exists.
        """

        return ToolProfile(
            skills=_dedupe_dicts([*self.skills, *override.skills], "path"),
            apps=_dedupe_dicts([*self.apps, *override.apps], "id"),
            mcp_servers=sorted(set(self.mcp_servers) | set(override.mcp_servers)),
            browser=self.browser or override.browser,
            desktop=self.desktop or override.desktop,
            system_admin=self.system_admin or override.system_admin,
            automatic=self.automatic or override.automatic,
        )

    def with_automatic_selection(self, message: str, project_kind: str) -> "ToolProfile":
        """Augment explicit choices with safe, locally available resources."""

        selected = ToolProfile.from_value(self.as_dict())
        if not selected.automatic:
            return selected

        text = str(message or "").casefold()
        web_terms = (
            "http://", "https://", ".com", ".com.br", "site", "página", "pagina",
            "web", "navegador", "browser", "url", "endpoint", "api pública",
        )
        desktop_terms = (
            "desktop", "tela", "janela", "interface gráfica", "interface grafica",
            "mouse", "teclado", "clicar", "clique", "aplicativo linux",
        )
        server_terms = (
            "servidor", "server", "ssh", "deploy", "produção", "producao",
            "serviço", "servico", "systemd", "docker", "banco", "postgres",
            "backup", "sasocq.com", "dex.sasocq.com",
        )

        if any(term in text for term in web_terms):
            selected.browser = True
            selected.mcp_servers = sorted(set(selected.mcp_servers) | {"playwright"})
        if project_kind == "system" and any(term in text for term in desktop_terms):
            selected.desktop = True
        if project_kind == "system" and any(term in text for term in server_terms):
            selected.system_admin = True
            selected.mcp_servers = sorted(set(selected.mcp_servers) | {"sasocq_server"})
        return selected


def _dedupe_dicts(items: Iterable[dict[str, str]], key: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        value = str(item.get(key) or "")
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(dict(item))
    return result


class ToolProfileStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def _read(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {"version": 1, "projects": {}, "threads": {}}
        if not isinstance(raw, dict):
            return {"version": 1, "projects": {}, "threads": {}}
        raw.setdefault("version", 1)
        raw.setdefault("projects", {})
        raw.setdefault("threads", {})
        return raw

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)

    def project(self, project_id: str) -> ToolProfile:
        with self._lock:
            data = self._read()
            return ToolProfile.from_value((data.get("projects") or {}).get(project_id))

    def thread(self, thread_id: str) -> Optional[ToolProfile]:
        with self._lock:
            data = self._read()
            raw = (data.get("threads") or {}).get(thread_id)
            return ToolProfile.from_value(raw) if raw is not None else None

    def effective(self, project_id: str, thread_id: Optional[str] = None) -> ToolProfile:
        project_profile = self.project(project_id)
        if not thread_id:
            return project_profile
        thread_profile = self.thread(thread_id)
        return thread_profile if thread_profile is not None else project_profile

    def save_project(self, project_id: str, profile: ToolProfile) -> ToolProfile:
        with self._lock:
            data = self._read()
            projects = data.setdefault("projects", {})
            projects[project_id] = profile.as_dict()
            self._write(data)
        return profile

    def save_thread(self, thread_id: str, project_id: str, profile: ToolProfile) -> ToolProfile:
        with self._lock:
            data = self._read()
            threads = data.setdefault("threads", {})
            value = profile.as_dict()
            value["project_id"] = project_id
            threads[thread_id] = value
            self._write(data)
        return profile

    def remember_thread_project(self, thread_id: str, project_id: str) -> None:
        """Persist routing for a thread without changing its tool choices."""

        with self._lock:
            data = self._read()
            threads = data.setdefault("threads", {})
            raw = threads.get(thread_id)
            value = dict(raw) if isinstance(raw, dict) else ToolProfile().as_dict()
            if value.get("project_id") == project_id:
                return
            value["project_id"] = project_id
            threads[thread_id] = value
            self._write(data)

    def thread_project_id(self, thread_id: str) -> Optional[str]:
        """Return the workspace/project that owns a persisted thread."""

        with self._lock:
            data = self._read()
            raw = (data.get("threads") or {}).get(thread_id)
            if not isinstance(raw, dict):
                return None
            value = str(raw.get("project_id") or "").strip()
            return value or None

    def remove_thread(self, thread_id: str) -> None:
        with self._lock:
            data = self._read()
            threads = data.setdefault("threads", {})
            if thread_id in threads:
                del threads[thread_id]
                self._write(data)
