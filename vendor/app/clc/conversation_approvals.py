from __future__ import annotations

import re
import os
import stat
import sqlite3
import time
import tomllib
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .site_access import domains_from_text


SUPPORTED_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
}
PENDING_TTL_SECONDS = 20 * 60
PENDING_LIMIT = 2_000


def _credential_elicitation(params: dict[str, Any]) -> bool:
    message = str(params.get("message") or "")
    return message.startswith("SASOCQ_CREDENTIALS\n") or message.startswith("SASOCQ_PAYMENT_CARD\n")


def _interactive_elicitation(params: dict[str, Any]) -> bool:
    """Actual user forms/URLs cannot inherit permission to execute a tool."""
    schema = params.get("requestedSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if _tool_approval_identity(params) is not None:
        return False
    return _credential_elicitation(params) or params.get("mode") in {"form", "url"} or bool(properties)


def _tool_approval_identity(params: dict[str, Any]) -> tuple[str, str] | None:
    """Recognize Codex's permission envelope, never a server's login form."""
    meta = params.get("_meta")
    schema = params.get("requestedSchema")
    if (not isinstance(meta, dict) or meta.get("codex_approval_kind") != "mcp_tool_call"
            or params.get("mode") != "form" or params.get("url")
            or not isinstance(schema, dict) or schema.get("type") != "object"
            or schema.get("properties") != {} or schema.get("required")):
        return None
    parsed = re.fullmatch(r'Allow the ([A-Za-z0-9_.-]+) MCP server to run tool "([A-Za-z0-9_.-]+)"\?',
                          str(params.get("message") or ""))
    if not parsed or params.get("serverName") != parsed.group(1):
        return None
    return parsed.group(1), parsed.group(2)


def project_tool_approval(path: Path, server: str, tool: str) -> bool:
    """Read an explicit, root-owned project policy, including for loaded turns.

    Project source cannot grant itself permission by writing a configuration.
    Read the file descriptor we checked, fail closed and never cache approvals.
    """
    try:
        with (Path(path) / ".codex/config.toml").open("rb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
                return False
            raw = stream.read(131073)
        if len(raw) > 131072:
            return False
        config = tomllib.loads(raw.decode("utf-8"))
        policy = config.get("mcp_servers", {}).get(server, {})
        if policy.get("enabled") is False:
            return False
        override = policy.get("tools", {}).get(tool, {})
        if override.get("enabled") is False:
            return False
        return override.get("approval_mode", policy.get("default_tools_approval_mode")) == "approve"
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _command_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _normalized(value: Any, fallback: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip()).casefold()
    return text or fallback


def _walk_strings(value: Any, depth: int = 0):
    if depth > 6 or value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item, depth + 1)


def approval_type_key(method: Any, params: Any) -> str:
    """Build the stable type key used by both stored and incoming approvals."""
    method = str(method or "")
    if method not in SUPPORTED_METHODS:
        return ""
    params = params if isinstance(params, dict) else {}
    if method == "mcpServer/elicitation/request":
        if _interactive_elicitation(params):
            return ""
        message = str(params.get("message") or "")
        parsed = re.search(
            r"Allow the\s+(.+?)\s+MCP server\s+to run tool\s+[\"']([^\"']+)[\"']",
            message,
            re.IGNORECASE,
        )
        server = params.get("serverName") or params.get("server") or (parsed.group(1) if parsed else "mcp")
        tool = params.get("toolName") or params.get("tool") or (parsed.group(2) if parsed else "request")
        return f"{method}:{_normalized(server, 'mcp')}:{_normalized(tool, 'request')}"
    if method == "item/commandExecution/requestApproval":
        command = _command_text(params.get("command")).strip().split()
        return f"{method}:{_normalized(command[0] if command else '', 'command')}"
    if method == "item/fileChange/requestApproval":
        return f"{method}:{_normalized(params.get('grantRoot'), 'files')}"
    permissions = params.get("permissions")
    names = sorted(str(name) for name in permissions) if isinstance(permissions, dict) else []
    return f"{method}:{','.join(names)}"


def approval_result(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if method == "mcpServer/elicitation/request" and not _interactive_elicitation(params):
        return {"action": "accept", "content": {}}
    if method == "item/permissions/requestApproval":
        permissions = params.get("permissions")
        return {"scope": "turn", "permissions": permissions if isinstance(permissions, dict) else {}}
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        return {"decision": "acceptForSession"}
    return None


class ConversationApprovalRules:
    """Durable per-conversation rules plus a short-lived cache of real requests."""

    def __init__(self, path: Path, site_policy: Callable[[str], str] | None = None,
                 project_path: Callable[[str], Path | None] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.site_policy = site_policy
        self.project_path = project_path
        self._pending: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
        self._forms = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_approval_rules (
                    workspace TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    approval_type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (workspace, thread_id, approval_type)
                );
                CREATE INDEX IF NOT EXISTS conversation_approval_rules_thread_idx
                    ON conversation_approval_rules(thread_id);
                """
            )
        self.path.chmod(0o600)

    @staticmethod
    def _identity(workspace: str, message: dict[str, Any]) -> tuple[str, str, str] | None:
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        thread_id = str(params.get("threadId") or "")
        rule_type = approval_type_key(method, params)
        if not workspace or not thread_id or not rule_type:
            return None
        return workspace, thread_id, rule_type

    def _purge_pending(self) -> None:
        cutoff = time.monotonic() - PENDING_TTL_SECONDS
        self._pending = {key: value for key, value in self._pending.items() if value[1] >= cutoff}
        if len(self._pending) > PENDING_LIMIT:
            excess = len(self._pending) - PENDING_LIMIT
            for key, _value in sorted(self._pending.items(), key=lambda item: item[1][1])[:excess]:
                self._pending.pop(key, None)

    def _matches(self, identity: tuple[str, str, str]) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM conversation_approval_rules WHERE workspace=? AND thread_id=? AND approval_type=?",
                identity,
            ).fetchone()
        return row is not None

    def _site_decision(self, method: str, params: dict[str, Any]) -> str:
        if self.site_policy is None:
            return ""
        strings = list(_walk_strings(params))
        domains = {
            domain
            for value in strings
            for domain in domains_from_text(value, allow_plain=True)
        }
        if not domains:
            return ""
        policies = [self.site_policy(domain) for domain in sorted(domains)]
        if "block" in policies:
            return "deny"
        if not all(policy == "auto" for policy in policies):
            return "prompt"
        text = " ".join(strings).casefold()
        if method == "item/permissions/requestApproval" and re.search(
            r"network|domain|host|url|internet|web", text
        ):
            return "allow"
        if method != "mcpServer/elicitation/request":
            return ""
        read_only = re.search(
            r"browser_(?:navigate|open|snapshot|screenshot|find|search|tabs)|\b(?:open|navigate|read|get|fetch|search|snapshot|screenshot)\b",
            text,
        )
        sensitive = re.search(
            r"\b(?:click|type|fill|submit|upload|download|delete|remove|purchase|buy|checkout|pay|send|message|post|put|patch|login|sign[ -]?in|auth|oauth)\b",
            text,
        )
        return "allow" if read_only and not sensitive else "prompt"

    @staticmethod
    def _denial_result(method: str) -> dict[str, Any] | None:
        if method == "mcpServer/elicitation/request":
            return {"action": "cancel", "content": None}
        if method == "item/permissions/requestApproval":
            return {"scope": "turn", "permissions": {}}
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            return {"decision": "decline"}
        return None

    def discard_finished_forms(self, event):
        workspace = event.get("workspace", "system")
        notification = event.get("notification") or {}
        method = notification.get("method")
        params = notification.get("params") or {}
        thread_id = params.get("threadId")
        bridge_ended = event.get("kind") == "bridge_status" and event.get("status") in {"stopped", "error"}
        if not bridge_ended and method != "turn/completed":
            return []
        resolved = []
        for key, (request, _) in list(self._forms.items()):
            owner = request.get("params", {}).get("threadId")
            if key[0] == workspace and (bridge_ended or thread_id and owner == thread_id):
                self._forms.pop(key, None)
                resolved.append({"kind": "mcp_form_resolved", "workspace": workspace, "thread_id": owner, "request_id": key[1]})
        return resolved

    def pending_form_events(self) -> list[dict[str, Any]]:
        cutoff = time.monotonic() - 300
        self._forms = {key: value for key, value in self._forms.items() if value[1] >= cutoff}
        return [{"kind": "server_request", "workspace": key[0], "request": value[0]}
                for key, value in self._forms.items()]

    def validate_form_response(self, workspace, request_id, result):
        record = self._forms.get((workspace, str(request_id)))
        if record is None:
            return False
        self.pending_form_events()
        if (workspace, str(request_id)) not in self._forms:
            raise ValueError("O formulário expirou; solicite uma nova tentativa.")
        if result.get("action") in {"cancel", "decline"}:
            return True
        if result.get("action") != "accept":
            raise ValueError("Resposta de formulário inválida.")
        params = record[0].get("params", {})
        schema = params.get("requestedSchema", {})
        content = result.get("content")
        if not isinstance(content, dict):
            raise ValueError("Preencha o formulário antes de responder.")
        properties = schema.get("properties", {})
        if set(content) - set(properties) or set(schema.get("required", [])) - set(content):
            raise ValueError("Campos do formulário inválidos ou incompletos.")
        if "signed_in" in properties and content.get("signed_in") is not True:
            raise ValueError("Confirme o login pelo formulário; aprovação genérica não conclui autenticação.")
        for name, value in content.items():
            definition = properties[name]
            kind = definition.get("type")
            valid = (kind == "boolean" and type(value) is bool or
                     kind == "string" and isinstance(value, str) or
                     kind == "integer" and type(value) is int or
                     kind == "number" and type(value) in {int, float})
            if not valid or ("enum" in definition and value not in definition["enum"]):
                raise ValueError("Valor inválido no formulário: " + name)
        return True

    async def handle_server_request(self, workspace: str, message: dict[str, Any]) -> dict[str, Any] | None:
        params = message.get("params") or {}
        if message.get("method") == "mcpServer/elicitation/request" and _interactive_elicitation(params) and not _credential_elicitation(params):
            self.pending_form_events()
            if len(self._forms) >= PENDING_LIMIT:
                return {"action": "cancel", "content": None}
            self._forms[(workspace, str(message.get("id")))] = (message, time.monotonic())
            return None
        identity = self._identity(workspace, message)
        if identity is None:
            return None
        self._purge_pending()
        pending_key = (workspace, str(message.get("id")))
        self._pending[pending_key] = (message, time.monotonic())
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        site_decision = self._site_decision(method, params)
        if site_decision == "prompt":
            return None
        if site_decision in {"allow", "deny"}:
            self._pending.pop(pending_key, None)
            return approval_result(method, params) if site_decision == "allow" else self._denial_result(method)
        tool_identity = _tool_approval_identity(params) if method == "mcpServer/elicitation/request" else None
        if tool_identity and workspace.startswith("project:") and self.project_path is not None:
            path = self.project_path(workspace)
            if path is not None and project_tool_approval(path, *tool_identity):
                self._pending.pop(pending_key, None)
                return approval_result(method, params)
        if not self._matches(identity):
            return None
        self._pending.pop(pending_key, None)
        return approval_result(method, params)

    def remember_pending(self, workspace: str, request_id: Any) -> tuple[tuple[str, str, str], bool]:
        self._purge_pending()
        pending = self._pending.get((workspace, str(request_id)))
        if pending is None:
            raise ValueError("A solicitação de aprovação não está mais pendente")
        identity = self._identity(workspace, pending[0])
        if identity is None:
            raise ValueError("Este tipo de solicitação não pode ser aprovado automaticamente")
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO conversation_approval_rules(workspace,thread_id,approval_type,created_at) VALUES (?,?,?,?)",
                (*identity, time.time()),
            )
        return identity, cursor.rowcount > 0

    def finish_pending(self, workspace: str, request_id: Any) -> dict[str, Any] | None:
        self._pending.pop((workspace, str(request_id)), None)
        form = self._forms.pop((workspace, str(request_id)), None)
        return form[0] if form else None

    def remove(self, identity: tuple[str, str, str]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM conversation_approval_rules WHERE workspace=? AND thread_id=? AND approval_type=?",
                identity,
            )

    def forget_thread(self, thread_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM conversation_approval_rules WHERE thread_id=?", (str(thread_id),))
        for key, (message, _created_at) in list(self._pending.items()):
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if str(params.get("threadId") or "") == str(thread_id):
                self._pending.pop(key, None)
