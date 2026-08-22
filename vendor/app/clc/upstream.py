from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .codex_bridge import CodexBridge
from .config import Settings


UPSTREAM_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
CHANGELOG_URL = "https://learn.chatgpt.com/docs/changelog"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _method_names(node: Any) -> set[str]:
    methods: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "method" and isinstance(value, dict):
                constant = value.get("const")
                if isinstance(constant, str) and "/" in constant:
                    methods.add(constant)
                for item in value.get("enum") or []:
                    if isinstance(item, str) and "/" in item:
                        methods.add(item)
            methods.update(_method_names(value))
    elif isinstance(node, list):
        for value in node:
            methods.update(_method_names(value))
    return methods


def _items(result: Any) -> list[Dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    value: Any = result
    for key in ("data", "items", "models", "features", "profiles", "modes", "hooks"):
        candidate = value.get(key) if isinstance(value, dict) else None
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _identity(items: list[Dict[str, Any]]) -> list[str]:
    values = {
        str(item.get("id") or item.get("name") or item.get("slug") or item.get("displayName"))
        for item in items
        if item.get("id") or item.get("name") or item.get("slug") or item.get("displayName")
    }
    return sorted(values)


class UpstreamRegistry:
    """Read-only registry of capabilities exposed by the installed Codex.

    It deliberately never enables experimental flags, installs packages, edits
    configuration, or promotes an update.  The registry supplies evidence for
    a later, explicitly approved canary/promotion workflow.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.resolved_config_dir / "upstream"
        self.state_file = self.root / "registry.json"
        self.schema_dir = self.root / "schemas"
        self._lock = asyncio.Lock()

    def read(self) -> Dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {
                "status": "never_checked",
                "last_checked_at": None,
                "current": {},
                "previous": {},
                "diff": {},
                "history": [],
                "policy": self.policy(),
            }

    @staticmethod
    def policy() -> Dict[str, Any]:
        return {
            "automatic_detection": True,
            "automatic_schema_capture": True,
            "automatic_promotion": False,
            "automatic_code_changes": False,
            "requires_approval": [
                "package_install",
                "production_promotion",
                "permissions",
                "sandbox",
                "authentication",
                "ssh",
                "desktop_control",
                "host_operations",
            ],
        }

    async def _run(self, *args: str, timeout: float = 120) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return process.returncode or 0, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()
        except Exception as exc:
            return 127, "", str(exc)

    async def _optional_rpc(self, bridge: CodexBridge, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return {"ok": True, "result": await bridge.request(method, params, timeout=60)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "result": {}}

    async def _capture_schema(self, codex_binary: str, version: str) -> Dict[str, Any]:
        self.schema_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="codex-upstream-schema-") as temporary:
            output = Path(temporary)
            code, stdout, stderr = await self._run(
                codex_binary,
                "app-server",
                "generate-json-schema",
                "--out",
                str(output),
                timeout=180,
            )
            if code:
                return {"ok": False, "error": stderr or stdout or f"exit {code}"}
            digest = hashlib.sha256()
            files = sorted(path for path in output.rglob("*.json") if path.is_file())
            for path in files:
                digest.update(path.relative_to(output).as_posix().encode())
                digest.update(path.read_bytes())
            schema_hash = digest.hexdigest()
            bundle = output / "codex_app_server_protocol.v2.schemas.json"
            methods: list[str] = []
            stored_path = ""
            if bundle.is_file():
                document = json.loads(bundle.read_text(encoding="utf-8"))
                methods = sorted(_method_names(document))
                safe_version = "".join(character if character.isalnum() or character in ".-_" else "-" for character in version)
                destination = self.schema_dir / f"{safe_version or 'unknown'}-{schema_hash[:12]}.json"
                if not destination.exists():
                    shutil.copy2(bundle, destination)
                    os.chmod(destination, 0o600)
                stored_path = str(destination)
            return {
                "ok": True,
                "hash": schema_hash,
                "file_count": len(files),
                "methods": methods,
                "stored_path": stored_path,
            }

    async def _desktop(self) -> Dict[str, Any]:
        code, stdout, _ = await self._run("dpkg-query", "-W", "-f=${Version}", "chatgpt", timeout=15)
        return {
            "installed": code == 0,
            "version": stdout if code == 0 else "",
            "command": shutil.which("chatgpt") or "",
            "linux_computer_use": False,
        }

    @staticmethod
    def _diff(previous: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
        previous_methods = set(previous.get("schema", {}).get("methods") or [])
        current_methods = set(current.get("schema", {}).get("methods") or [])
        previous_capabilities = previous.get("capability_ids") or {}
        current_capabilities = current.get("capability_ids") or {}
        capability_diff: Dict[str, Any] = {}
        for name in sorted(set(previous_capabilities) | set(current_capabilities)):
            old = set(previous_capabilities.get(name) or [])
            new = set(current_capabilities.get(name) or [])
            capability_diff[name] = {"added": sorted(new - old), "removed": sorted(old - new)}
        return {
            "version_changed": bool(previous) and previous.get("codex_version") != current.get("codex_version"),
            "schema_changed": bool(previous) and previous.get("schema", {}).get("hash") != current.get("schema", {}).get("hash"),
            "methods_added": sorted(current_methods - previous_methods),
            "methods_removed": sorted(previous_methods - current_methods),
            "capabilities": capability_diff,
        }

    async def check(self, bridge: CodexBridge) -> Dict[str, Any]:
        async with self._lock:
            prior_state = self.read()
            previous = prior_state.get("current") or {}
            codex_args = self.settings.codex_args
            codex_binary = codex_args[0]
            version_code, version_stdout, version_stderr = await self._run(codex_binary, "--version", timeout=20)
            version = version_stdout or "unknown"

            cwd = str(self.settings.resolved_system_workspace)
            calls = {
                "models": ("model/list", {"limit": 100, "includeHidden": True}),
                "experimental": ("experimentalFeature/list", {"limit": 200}),
                "permissions": ("permissionProfile/list", {"cwd": cwd, "limit": 100}),
                "collaboration": ("collaborationMode/list", {}),
                "skills": ("skills/list", {"cwds": [cwd], "forceReload": False}),
                "hooks": ("hooks/list", {"cwds": [cwd]}),
            }
            results_list = await asyncio.gather(
                *(self._optional_rpc(bridge, method, params) for method, params in calls.values())
            )
            capabilities = dict(zip(calls, results_list))
            schema = await self._capture_schema(codex_binary, version)
            desktop = await self._desktop()
            capability_ids = {name: _identity(_items(value.get("result"))) for name, value in capabilities.items()}
            current = {
                "checked_at": _utc_now(),
                "codex_version": version,
                "codex_version_ok": version_code == 0,
                "codex_version_error": version_stderr if version_code else "",
                "control_plane_version": self.settings.app_version,
                "schema": schema,
                "desktop": desktop,
                "capabilities": capabilities,
                "capability_ids": capability_ids,
                "changelog_url": CHANGELOG_URL,
            }
            diff = self._diff(previous, current)
            history = list(prior_state.get("history") or [])
            history.append({
                "checked_at": current["checked_at"],
                "codex_version": version,
                "schema_hash": schema.get("hash", ""),
                "changes": sum(len(diff.get(key) or []) for key in ("methods_added", "methods_removed")),
            })
            state = {
                "status": "ok" if version_code == 0 and schema.get("ok") else "degraded",
                "last_checked_at": current["checked_at"],
                "current": current,
                "previous": previous,
                "diff": diff,
                "history": history[-24:],
                "policy": self.policy(),
            }
            _atomic_json(self.state_file, state)
            return state

