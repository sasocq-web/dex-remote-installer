from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


URL_RE = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"(?<![\w@])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?![\w-])",
    re.IGNORECASE,
)
POLICY_MODES = {"auto", "ask", "block"}
FREQUENT_ACCESS_THRESHOLD = 3
BROWSER_SERVERS = {"browser", "chrome", "playwright", "web"}


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if not text:
        return ""
    candidate = text if "://" in text else f"https://{text}"
    try:
        hostname = (urlsplit(candidate).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return ""
    labels = hostname.split(".")
    if len(labels) < 2 or any(not label or len(label) > 63 for label in labels):
        return ""
    if not all(re.fullmatch(r"[a-z0-9-]+", label) for label in labels):
        return ""
    return hostname


def domains_from_text(value: Any, *, allow_plain: bool = False) -> set[str]:
    text = str(value or "")
    candidates = [match.group(0).rstrip(".,);]}>") for match in URL_RE.finditer(text)]
    if allow_plain:
        candidates.extend(match.group(0) for match in DOMAIN_RE.finditer(text))
    return {domain for candidate in candidates if (domain := normalize_domain(candidate))}


def _walk_strings(value: Any, *, max_depth: int = 6) -> Iterable[str]:
    if max_depth < 0:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item, max_depth=max_depth - 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item, max_depth=max_depth - 1)


def _tool_identity(item: dict[str, Any]) -> tuple[str, str]:
    return (
        str(item.get("server") or item.get("serverName") or "").casefold(),
        str(item.get("tool") or item.get("toolName") or item.get("name") or "").casefold(),
    )


def domains_from_page_result(value: Any) -> set[str]:
    domains: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in {"url", "pageurl", "page_url", "currenturl", "current_url"}:
                for text in _walk_strings(item):
                    domains.update(domains_from_text(text))
            elif isinstance(item, (dict, list, tuple)):
                domains.update(domains_from_page_result(item))
        return domains
    for text in _walk_strings(value):
        for match in re.finditer(r"(?:Page|Current)\s+URL:\s*(https?://[^\s<>'\"`]+)", text, re.IGNORECASE):
            domain = normalize_domain(match.group(1).rstrip(".,);]}>"))
            if domain:
                domains.add(domain)
    return domains


def domains_from_item(item: Any) -> set[str]:
    """Extract only sites intentionally addressed by a completed tool call.

    Search-result bodies are deliberately ignored: appearing in results is not
    the same as a site being accessed. URLs are reduced to hostnames immediately.
    """

    if not isinstance(item, dict):
        return set()
    item_type = str(item.get("type") or "").casefold()
    server, tool = _tool_identity(item)
    values: list[Any] = []
    page_results: list[Any] = []
    allow_plain = False

    if item_type == "mcptoolcall":
        browser_tool = server in BROWSER_SERVERS or tool.startswith("browser_")
        web_open = server == "web" and any(part in tool for part in ("open", "click", "run"))
        if not (browser_tool or web_open):
            return set()
        values.append(item.get("arguments") or item.get("input") or item.get("params") or {})
        # Browser output often contains the final Page URL after redirects.
        if browser_tool and not any(part in tool for part in ("search", "query")):
            page_results.append(item.get("result") or item.get("output") or "")
        allow_plain = True
    elif item_type == "commandexecution":
        values.append(item.get("command") or "")
    elif item_type in {"websearch", "websearchcall"}:
        # Explicit URL opens may be represented as a webSearch item. Queries and
        # result snippets are not counted.
        values.append(item.get("url") or item.get("open") or "")
    else:
        return set()

    domains: set[str] = set()
    for value in values:
        for text in _walk_strings(value):
            domains.update(domains_from_text(text, allow_plain=allow_plain))
    for value in page_results:
        domains.update(domains_from_page_result(value))
    return domains


def action_summary(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "").casefold()
    server, tool = _tool_identity(item)
    normalized = tool.replace("browser_", "").replace("_", " ").strip()
    labels = {
        "navigate": "Abriu uma página",
        "open": "Abriu uma página",
        "click": "Seguiu um link",
        "snapshot": "Leu a página",
        "screenshot": "Capturou a página",
        "tabs": "Consultou as abas",
        "find": "Procurou conteúdo na página",
    }
    if item_type == "commandexecution":
        return "Acessou pela linha de comando"
    if item_type in {"websearch", "websearchcall"}:
        return "Abriu um resultado da web"
    return labels.get(normalized, f"Usou {tool or server or 'uma ferramenta web'}")[:160]


def first_user_request(thread: dict[str, Any]) -> str:
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").casefold() not in {"usermessage", "message"}:
                continue
            role = str(item.get("role") or "user").casefold()
            if role != "user":
                continue
            content = item.get("text") or item.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text") or part.get("input_text") or "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            text = re.sub(r"\s+", " ", str(content)).strip()
            if text:
                return text
    return str(thread.get("preview") or "")


def numeric_timestamp(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


class SiteAccessStore:
    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _empty(self) -> dict[str, Any]:
        return {"version": self.VERSION, "sites": {}, "policies": {}, "seen": []}

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.setdefault("sites", {})
                value.setdefault("policies", {})
                value.setdefault("seen", [])
                return value
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return self._empty()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def policy(self, domain: Any) -> str:
        normalized = normalize_domain(domain)
        with self._lock:
            explicit = self._data["policies"].get(normalized)
            if explicit:
                return str(explicit)
            count = int((self._data["sites"].get(normalized) or {}).get("count") or 0)
            return "auto" if count >= FREQUENT_ACCESS_THRESHOLD else "ask"

    def set_policy(self, domain: Any, mode: str) -> dict[str, str]:
        normalized = normalize_domain(domain)
        if not normalized:
            raise ValueError("Domínio inválido")
        if mode not in POLICY_MODES:
            raise ValueError("Política inválida")
        with self._lock:
            self._data["policies"][normalized] = mode
            self._save()
        return {"domain": normalized, "policy": mode}

    def record_item(
        self,
        item: dict[str, Any],
        *,
        workspace: str,
        thread_id: str,
        project_id: str = "",
        project_name: str = "",
        thread_title: str = "",
        request_summary: str = "",
        timestamp: float | None = None,
    ) -> int:
        domains = domains_from_item(item)
        if not domains or not thread_id:
            return 0
        item_id = str(item.get("id") or "")
        if not item_id:
            item_id = hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:24]
        timestamp = float(timestamp or time.time())
        action = action_summary(item)
        recorded = 0
        with self._lock:
            seen = set(str(value) for value in self._data.get("seen") or [])
            for domain in sorted(domains):
                event_key = f"{workspace}:{thread_id}:{item_id}:{domain}"
                if event_key in seen:
                    continue
                seen.add(event_key)
                recorded += 1
                site = self._data["sites"].setdefault(
                    domain,
                    {"domain": domain, "count": 0, "first_access": timestamp, "last_access": timestamp, "conversations": {}},
                )
                site["count"] = int(site.get("count") or 0) + 1
                site["first_access"] = min(float(site.get("first_access") or timestamp), timestamp)
                site["last_access"] = max(float(site.get("last_access") or timestamp), timestamp)
                conversation_key = f"{workspace}:{thread_id}"
                conversation = site["conversations"].setdefault(
                    conversation_key,
                    {"workspace": workspace, "thread_id": thread_id, "count": 0, "first_access": timestamp, "last_access": timestamp, "actions": []},
                )
                conversation["count"] = int(conversation.get("count") or 0) + 1
                conversation["first_access"] = min(float(conversation.get("first_access") or timestamp), timestamp)
                conversation["last_access"] = max(float(conversation.get("last_access") or timestamp), timestamp)
                if project_id:
                    conversation["project_id"] = project_id
                if project_name:
                    conversation["project_name"] = project_name[:160]
                if thread_title:
                    conversation["thread_title"] = thread_title[:240]
                if request_summary:
                    conversation["summary"] = re.sub(r"\s+", " ", request_summary).strip()[:500]
                actions = conversation.setdefault("actions", [])
                if action not in actions:
                    actions.append(action)
                    del actions[8:]
            if recorded:
                self._data["seen"] = list(seen)[-20_000:]
                self._save()
        return recorded

    def import_thread(
        self,
        thread: dict[str, Any],
        *,
        workspace: str,
        project_id: str,
        project_name: str,
        request_summary: str = "",
    ) -> int:
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            return 0
        title = str(thread.get("name") or thread.get("title") or "")
        summary = request_summary or first_user_request(thread)
        timestamp = numeric_timestamp(thread.get("updatedAt") or thread.get("createdAt"), time.time())
        total = 0
        for turn in thread.get("turns") or []:
            turn_timestamp = numeric_timestamp(
                turn.get("completedAt") or turn.get("startedAt"), timestamp
            ) if isinstance(turn, dict) else timestamp
            for item in turn.get("items") or [] if isinstance(turn, dict) else []:
                if isinstance(item, dict):
                    total += self.record_item(
                        item,
                        workspace=workspace,
                        thread_id=thread_id,
                        project_id=project_id,
                        project_name=project_name,
                        thread_title=title,
                        request_summary=summary,
                        timestamp=turn_timestamp,
                    )
        return total

    def snapshot(self, refresh: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            sites: list[dict[str, Any]] = []
            effective_policies = dict(self._data["policies"])
            for domain, raw in self._data["sites"].items():
                conversations = list((raw.get("conversations") or {}).values())
                conversations.sort(key=lambda value: float(value.get("last_access") or 0), reverse=True)
                policy = str(self._data["policies"].get(domain) or (
                    "auto" if int(raw.get("count") or 0) >= FREQUENT_ACCESS_THRESHOLD else "ask"
                ))
                effective_policies[domain] = policy
                sites.append({
                    "domain": domain,
                    "count": int(raw.get("count") or 0),
                    "first_access": float(raw.get("first_access") or 0),
                    "last_access": float(raw.get("last_access") or 0),
                    "policy": policy,
                    "conversation_count": len(conversations),
                    "conversations": conversations,
                })
            sites.sort(key=lambda value: (-int(value["count"]), -float(value["last_access"]), value["domain"]))
            return {
                "sites": sites,
                "policies": effective_policies,
                "totals": {"sites": len(sites), "accesses": sum(site["count"] for site in sites), "conversations": len({(c.get("workspace"), c.get("thread_id")) for site in sites for c in site["conversations"]})},
                "refresh": dict(refresh or {}),
                "privacy": "Somente os domínios são armazenados; caminhos, parâmetros e tokens das URLs são descartados. Após 3 acessos, navegação e leitura passam a Automático, salvo uma escolha diferente nesta página.",
            }

    def policy_snapshot(self) -> dict[str, Any]:
        """Return only effective policies needed while the Dex boots."""
        with self._lock:
            effective_policies = dict(self._data["policies"])
            for domain, raw in self._data["sites"].items():
                effective_policies[domain] = str(self._data["policies"].get(domain) or (
                    "auto" if int(raw.get("count") or 0) >= FREQUENT_ACCESS_THRESHOLD else "ask"
                ))
            return {"policies": effective_policies}
