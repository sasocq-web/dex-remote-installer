from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class OperationsStore:
    """Small durable store for UI state that the Codex app-server does not own.

    Queue entries and navigation metadata survive browser refreshes and host
    reboots. Writes are atomic and guarded because HTTP requests and app-server
    event handlers can touch the store concurrently.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def queue(self, thread_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._read().get("queues", {}).get(thread_id, []))

    def enqueue(self, thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            queue = data.setdefault("queues", {}).setdefault(thread_id, [])
            item = {
                "id": str(uuid.uuid4()),
                "message": str(payload.get("message") or "").strip(),
                "project_id": str(payload.get("project_id") or ""),
                "model": payload.get("model"),
                "effort": payload.get("effort"),
                "service_tier": payload.get("service_tier"),
                "network_access": bool(payload.get("network_access")),
                "tools": payload.get("tools") or {},
                "references": payload.get("references") or [],
                "collaboration_mode": payload.get("collaboration_mode"),
                "goal_mode": bool(payload.get("goal_mode")),
                "status": "queued",
                "created_at": time.time(),
            }
            queue.append(item)
            self._write(data)
            return item

    def update_queue_item(self, thread_id: str, item_id: str, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"message", "status", "model", "effort", "service_tier", "network_access", "tools", "references", "collaboration_mode", "goal_mode"}
        with self._lock:
            data = self._read()
            queue = data.setdefault("queues", {}).setdefault(thread_id, [])
            item = next((entry for entry in queue if entry.get("id") == item_id), None)
            if item is None:
                raise KeyError(item_id)
            for key, value in values.items():
                if key in allowed:
                    item[key] = value
            item["updated_at"] = time.time()
            self._write(data)
            return dict(item)

    def claim_for_steer(self, thread_id: str, item_id: str) -> dict[str, Any] | None:
        """Reserve one queued item so the completion worker cannot start it."""
        with self._lock:
            data = self._read()
            queue = data.setdefault("queues", {}).setdefault(thread_id, [])
            item = next((entry for entry in queue if entry.get("id") == item_id), None)
            if item is None or item.get("status") != "queued":
                return None
            item["status"] = "steering"
            item["steering_at"] = time.time()
            self._write(data)
            return dict(item)

    def finish_steer(self, thread_id: str, item_id: str, accepted: bool) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            queue = data.setdefault("queues", {}).setdefault(thread_id, [])
            item = next((entry for entry in queue if entry.get("id") == item_id), None)
            if item is None:
                raise KeyError(item_id)
            if item.get("status") != "steering":
                return dict(item)
            now = time.time()
            item["status"] = "steered" if accepted else "queued"
            item["updated_at"] = now
            if accepted:
                item["finished_at"] = now
            else:
                item.pop("steering_at", None)
            self._write(data)
            return dict(item)

    def remove_queue_item(self, thread_id: str, item_id: str) -> bool:
        with self._lock:
            data = self._read()
            queue = data.setdefault("queues", {}).setdefault(thread_id, [])
            remaining = [item for item in queue if item.get("id") != item_id]
            if len(remaining) == len(queue):
                return False
            data["queues"][thread_id] = remaining
            self._write(data)
            return True

    def reorder(self, thread_id: str, item_ids: list[str]) -> list[dict[str, Any]]:
        with self._lock:
            data = self._read()
            queue = data.setdefault("queues", {}).setdefault(thread_id, [])
            by_id = {str(item.get("id")): item for item in queue}
            ordered = [by_id.pop(item_id) for item_id in item_ids if item_id in by_id]
            ordered.extend(item for item in queue if str(item.get("id")) in by_id)
            data["queues"][thread_id] = ordered
            self._write(data)
            return list(ordered)

    def claim_next(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            queue = data.setdefault("queues", {}).setdefault(thread_id, [])
            item = next((entry for entry in queue if entry.get("status") == "queued"), None)
            if item is None:
                return None
            item["status"] = "running"
            item["started_at"] = time.time()
            self._write(data)
            return dict(item)

    def finish_running(self, thread_id: str, status: str = "completed") -> None:
        with self._lock:
            data = self._read()
            queue = data.setdefault("queues", {}).setdefault(thread_id, [])
            for item in queue:
                if item.get("status") == "running":
                    item["status"] = status
                    item["finished_at"] = time.time()
            self._write(data)

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            return {
                "threads": data.get("threads", {}),
                "projects": data.get("projects", {}),
                "chrome": data.get("chrome", {"connected": False}),
            }

    def set_metadata(self, kind: str, item_id: str, values: dict[str, Any]) -> dict[str, Any]:
        if kind not in {"threads", "projects"}:
            raise ValueError("tipo de metadado inválido")
        with self._lock:
            data = self._read()
            item = data.setdefault(kind, {}).setdefault(item_id, {})
            item.update(values)
            item["updated_at"] = time.time()
            self._write(data)
            return dict(item)
