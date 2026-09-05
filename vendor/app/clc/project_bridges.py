from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from .codex_bridge import CodexBridge
from .config import Settings
from .events import EventHub


def safe_project_unit(project_id: str) -> str:
    """Return the deterministic per-project systemd unit used by the worker wrapper."""
    readable = re.sub(r"[^A-Za-z0-9]+", "-", str(project_id)).strip("-").lower()[:36] or "project"
    digest = hashlib.sha256(str(project_id).encode("utf-8")).hexdigest()[:12]
    return f"clc-project-{readable}-{digest}.service"


class ProjectBridgePool:
    """One independent Codex app-server per project."""

    def __init__(
        self,
        settings: Settings,
        events: EventHub,
        server_request_handler: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.server_request_handler = server_request_handler
        self._bridges: dict[str, CodexBridge] = {}
        self._paths: dict[str, str] = {}
        self._names: dict[str, str] = {}

    def get(self, project_id: str, path: str = "", name: str = "") -> CodexBridge:
        project_id = str(project_id).strip()
        if not project_id:
            raise ValueError("project_id é obrigatório")
        bridge = self._bridges.get(project_id)
        if bridge is None:
            environment = {
                "CLC_WORKSPACE": "project",
                "CLC_PROJECT_ID": project_id,
                "CLC_PROJECT_PATH": str(Path(path).resolve()) if path else "",
                "CLC_PROJECT_UNIT": safe_project_unit(project_id),
            }
            bridge = CodexBridge(
                self.settings,
                self.events,
                label=f"project:{project_id}",
                command=self.settings.project_codex_args,
                environment=environment,
                server_request_handler=self.server_request_handler,
            )
            self._bridges[project_id] = bridge
        if path:
            self._paths[project_id] = str(Path(path).resolve())
            bridge.environment["CLC_PROJECT_PATH"] = self._paths[project_id]
        if name:
            self._names[project_id] = str(name)
        return bridge

    def existing(self, project_id: str) -> CodexBridge | None:
        return self._bridges.get(str(project_id))

    async def stop(self, project_id: str | None = None) -> None:
        if project_id is not None:
            bridge = self._bridges.pop(str(project_id), None)
            self._paths.pop(str(project_id), None)
            self._names.pop(str(project_id), None)
            if bridge:
                await bridge.stop()
            return
        bridges = list(self._bridges.values())
        self._bridges.clear()
        self._paths.clear()
        self._names.clear()
        # Each worker wrapper can take up to five seconds to stop its transient
        # systemd unit. Stopping sequentially exceeded the service's 20-second
        # timeout as soon as several projects had been opened, which caused a
        # forced kill and a false watchdog quarantine. They are independent and
        # must be stopped concurrently.
        if bridges:
            await asyncio.gather(*(bridge.stop() for bridge in bridges))

    def state(self, project_id: str | None = None) -> dict[str, Any]:
        if project_id:
            bridge = self._bridges.get(str(project_id))
            if not bridge:
                return {"running": False, "initialized": False, "last_error": None, "lazy": True}
            return {
                "running": bridge.running,
                "initialized": bridge.initialized,
                "last_error": bridge.last_error,
                "lazy": not bridge.running,
                "workspace": bridge.label,
            }
        projects: dict[str, Any] = {}
        for pid, bridge in self._bridges.items():
            projects[pid] = {
                "running": bridge.running,
                "initialized": bridge.initialized,
                "last_error": bridge.last_error,
                "workspace": bridge.label,
                "path": self._paths.get(pid, ""),
                "name": self._names.get(pid, ""),
            }
        return {
            "running": any(item["running"] for item in projects.values()),
            "initialized": any(item["initialized"] for item in projects.values()),
            "last_error": next((item["last_error"] for item in projects.values() if item["last_error"]), None),
            "lazy": True,
            "count": len(projects),
            "projects": projects,
            "independent_processes": True,
        }

    def bridges(self) -> list[CodexBridge]:
        return list(self._bridges.values())
