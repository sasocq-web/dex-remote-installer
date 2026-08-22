from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Iterable, List, Optional

SYSTEM_PROJECT_ID = "system-control"
LEGACY_SYSTEM_PROJECT_IDS = {"system-sasocq", "system-control"}


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    path: str
    kind: str = "project"


class ProjectStore:
    def __init__(self, path: Path, allowed_roots: Iterable[Path], system_workspace: Path | None = None) -> None:
        self.path = path
        self.allowed_roots = [root.resolve() for root in allowed_roots]
        self.system_workspace = (system_workspace or (Path.home() / ".local/share/codex-linux-control/system-workspace")).resolve()
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def set_allowed_roots(self, roots: Iterable[Path]) -> None:
        with self._lock:
            self.allowed_roots = [root.resolve() for root in roots]

    def allow_root(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        with self._lock:
            if resolved not in self.allowed_roots:
                self.allowed_roots.append(resolved)

    def ensure_default(self) -> None:
        """Ensure at least one normal project without duplicating the permanent System workspace.

        The permanent System workspace is owned by ``main.py`` and never appears
        in this mutable project store. Legacy system entries are removed during
        migration.
        """
        with self._lock:
            default_root = (self.allowed_roots[0] if self.allowed_roots else (Path.home() / "CodexProjects")).resolve()
            default_root.mkdir(parents=True, exist_ok=True)
            self.allow_root(default_root)
            current = self.list() if self.path.exists() else []
            normal = [item for item in current if item.kind != "system" and item.id not in LEGACY_SYSTEM_PROJECT_IDS]
            if not normal:
                normal.append(Project(self._make_id(default_root), "Projetos Codex", str(default_root), "project"))
            self._write(self._sorted(normal))

    def list(self) -> List[Project]:
        with self._lock:
            if not self.path.exists():
                return []
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return []
            projects: List[Project] = []
            for item in data.get("projects", []):
                try:
                    kind = str(item.get("kind") or ("system" if item.get("id") in LEGACY_SYSTEM_PROJECT_IDS else "project"))
                    projects.append(Project(id=item["id"], name=item["name"], path=item["path"], kind=kind))
                except (KeyError, TypeError):
                    continue
            return projects

    def get(self, project_id: str) -> Optional[Project]:
        return next((project for project in self.list() if project.id == project_id), None)

    def add(self, name: str, path: str, *, trust_selected_path: bool = False) -> Project:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError("A pasta selecionada não existe")
        if resolved == self.system_workspace:
            return self.get(SYSTEM_PROJECT_ID) or Project(SYSTEM_PROJECT_ID, "Sistema — Mini PC", str(resolved), "system")
        if trust_selected_path:
            self.allow_root(resolved)
        elif not self._is_allowed(resolved):
            roots = ", ".join(str(root) for root in self.allowed_roots)
            raise ValueError(f"Pasta fora das raízes permitidas: {roots}")
        clean_name = self._clean_name(name, resolved.name or "Projeto")
        project = Project(self._make_id(resolved), clean_name, str(resolved), "project")
        with self._lock:
            current = [item for item in self.list() if item.id != project.id]
            current.append(project)
            self._write(self._sorted(current))
        return project

    def rename(self, project_id: str, name: str) -> Project:
        if project_id in LEGACY_SYSTEM_PROJECT_IDS:
            raise ValueError("O workspace Sistema é permanente e não pode ser renomeado")
        with self._lock:
            current = self.list()
            existing = next((item for item in current if item.id == project_id), None)
            if not existing:
                raise ValueError("Projeto não encontrado")
            renamed = Project(existing.id, self._clean_name(name, existing.name), existing.path, existing.kind)
            updated = [renamed if item.id == project_id else item for item in current]
            self._write(self._sorted(updated))
            return renamed

    def relocate(self, project_id: str, path: str) -> Project:
        if project_id in LEGACY_SYSTEM_PROJECT_IDS:
            raise ValueError("O workspace Sistema tem localização fixa")
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError("A nova pasta do projeto não existe")
        if not self._is_allowed(resolved):
            raise ValueError("A nova pasta está fora das raízes permitidas")
        with self._lock:
            current = self.list()
            existing = next((item for item in current if item.id == project_id), None)
            if not existing:
                raise ValueError("Projeto não encontrado")
            relocated = Project(existing.id, existing.name, str(resolved), existing.kind)
            updated = [relocated if item.id == project_id else item for item in current]
            self._write(self._sorted(updated))
            return relocated

    def remove(self, project_id: str) -> bool:
        if project_id in LEGACY_SYSTEM_PROJECT_IDS:
            raise ValueError("O workspace Sistema é permanente e não pode ser removido")
        with self._lock:
            current = self.list()
            remaining = [item for item in current if item.id != project_id]
            if len(remaining) == len(current):
                return False
            self._write(self._sorted(remaining))
            return True

    def _write(self, projects: List[Project]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"projects": [asdict(item) for item in projects]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    @staticmethod
    def _sorted(projects: List[Project]) -> List[Project]:
        return sorted(projects, key=lambda item: (0 if item.kind == "system" else 1, item.name.casefold()))

    def _is_allowed(self, path: Path) -> bool:
        for root in self.allowed_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _clean_name(value: str, fallback: str) -> str:
        clean = re.sub(r"\s+", " ", value).strip() or fallback
        return clean[:100]

    @staticmethod
    def _make_id(path: Path) -> str:
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
        return f"project-{digest}"
