from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from .tool_profiles import ToolProfile

RPC = Callable[[str, Optional[Dict[str, Any]], float], Awaitable[Any]]
_SAFE_CONFIG_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidExtensionIdentifier(ValueError):
    pass


def config_identifier(value: str, kind: str = "extensão") -> str:
    cleaned = value.strip()
    if not _SAFE_CONFIG_ID.fullmatch(cleaned):
        raise InvalidExtensionIdentifier(f"Identificador de {kind} inválido")
    return cleaned


def skill_path(item: Dict[str, Any]) -> str:
    for key in ("path", "skillPath", "filePath", "skillFile"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def app_slug(item: Dict[str, Any]) -> str:
    raw = str(item.get("slug") or item.get("name") or item.get("id") or "").strip()
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-").casefold()


def profile_input(message: str, profile: ToolProfile, home: Path) -> list[dict[str, str]]:
    text_markers: list[str] = []
    items: list[dict[str, str]] = []
    seen_skills: set[str] = set()

    def add_skill(name: str, path: str) -> None:
        if not name or not path or path in seen_skills:
            return
        seen_skills.add(path)
        text_markers.append(f"${name}")
        items.append({"type": "skill", "name": name, "path": path})

    for skill in profile.skills:
        add_skill(str(skill.get("name") or ""), str(skill.get("path") or ""))

    if profile.browser:
        add_skill("clc-browser", str(home / ".codex" / "skills" / "clc-browser" / "SKILL.md"))
    if profile.desktop:
        add_skill("clc-desktop", str(home / ".codex" / "skills" / "clc-desktop" / "SKILL.md"))
    if profile.system_admin:
        add_skill("clc-system-admin", str(home / ".codex" / "skills" / "clc-system-admin" / "SKILL.md"))

    for app in profile.apps:
        app_id = str(app.get("id") or "").strip()
        name = str(app.get("name") or app_id).strip()
        slug = str(app.get("slug") or app_id).strip()
        if not app_id:
            continue
        text_markers.append(f"${slug}")
        items.append({"type": "mention", "name": name, "path": f"app://{app_id}"})

    preface: list[str] = []
    if text_markers:
        preface.append(" ".join(text_markers))
    if profile.mcp_servers:
        preface.append(
            "Use preferencialmente os servidores MCP associados a esta conversa: "
            + ", ".join(profile.mcp_servers)
            + ". O pedido atual já autoriza as ações reversíveis diretamente necessárias; solicite nova aprovação apenas para ações externas sensíveis, destrutivas, irreversíveis ou fora do escopo."
        )
    if profile.browser:
        preface.append(
            "O navegador Playwright está associado a esta conversa. Use-o para navegação e testes web; "
            "não finalize compras, pagamentos, envios ou alterações irreversíveis sem aprovação explícita."
        )
    if profile.desktop:
        preface.append(
            "O controle supervisionado do desktop Linux está associado a esta conversa. "
            "Se o pedido exigir clicar, digitar, abrir aplicativos ou enviar atalhos, descreva a ação e prossiga; não peça novamente a autorização já dada pelo próprio pedido."
        )
    if profile.system_admin:
        preface.append(
            "A administração do computador está associada a esta conversa. Esta identidade possui sudo sem senha; "
            "use-o somente quando necessário, preserve dados e confirme ações destrutivas com o operador."
        )
    final_text = message
    if preface:
        final_text = "\n".join(preface) + "\n\nSolicitação do usuário:\n" + message
    return [{"type": "text", "text": final_text}, *items]
