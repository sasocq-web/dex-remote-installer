#!/usr/bin/env python3
"""Resilient stdio adapter for the supervised shared Playwright MCP.

The Playwright HTTP server exposes legacy SSE and Streamable HTTP transports.
The former maps cleanly to Codex's long-lived stdio MCP process: one persistent
GET owns the BrowserContext and short POSTs submit JSON-RPC messages. Keeping
that GET open is essential; POST-only Streamable HTTP use makes the server
dispose the context and the next call silently returns to ``about:blank``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import uuid
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = "http://localhost:8766"
SSE_URL = f"{BASE_URL}/sse"
CREDENTIAL_BRIDGE_SOCKET = os.environ.get("SASOCQ_BROWSER_CREDENTIAL_SOCKET", "").strip()
SAFE_RETRY_TOOLS = {
    "browser_close",
    "browser_console_messages",
    "browser_network_requests",
    "browser_snapshot",
    "browser_take_screenshot",
}

CREDENTIAL_TOOL_NAME = "browser_fill_credentials"
CREDENTIAL_TOOL = {
    "name": CREDENTIAL_TOOL_NAME,
    "description": (
        "Solicita login, senha e/ou código de uso único em um formulário protegido dentro da conversa do Dex "
        "e preenche os campos indicados na página atual. Use esta ferramenta sempre que uma navegação no Chrome "
        "precisar de credenciais; nunca peça a senha no chat nem digite segredos com browser_type/browser_fill_form. "
        "A ferramenta devolve somente o resultado do preenchimento, nunca os valores informados pelo usuário."
    ),
    "inputSchema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "site": {
                "type": "string",
                "description": "Domínio ou nome do serviço mostrado ao usuário.",
                "maxLength": 200,
            },
            "purpose": {
                "type": "string",
                "description": "Motivo curto para o login, sem incluir dados secretos.",
                "maxLength": 300,
            },
            "request_token": {
                "type": "string",
                "description": (
                    "Identificador técnico único para vincular o formulário à conversa correta. "
                    "Gere um UUID novo em cada chamada."
                ),
                "pattern": "^[0-9a-fA-F-]{32,64}$",
                "maxLength": 64,
            },
            "login_target": {
                "type": "string",
                "description": "Referência exata do campo de login no snapshot ou seletor único.",
                "maxLength": 1000,
            },
            "password_target": {
                "type": "string",
                "description": "Referência exata do campo de senha no snapshot ou seletor único.",
                "maxLength": 1000,
            },
            "one_time_code_target": {
                "type": "string",
                "description": "Referência exata do campo de código temporário no snapshot ou seletor único.",
                "maxLength": 1000,
            },
            "submit_target": {
                "type": "string",
                "description": "Referência do botão a clicar depois de preencher. Omita para apenas preencher.",
                "maxLength": 1000,
            },
        },
        "required": ["site", "request_token"],
        "additionalProperties": False,
    },
    "annotations": {
        "title": "Preencher credenciais com segurança",
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
    },
}

PAYMENT_CARD_TOOL_NAME = "browser_fill_payment_card"
PAYMENT_CARD_TOOL = {
    "name": PAYMENT_CARD_TOOL_NAME,
    "description": (
        "Solicita dados de cartão de pagamento em um formulário protegido dentro da conversa do Dex e preenche "
        "os campos indicados na página HTTPS atual. Use esta ferramenta sempre que uma página precisar de número "
        "do cartão, nome, validade, código de segurança ou CEP; nunca peça esses dados no chat nem os digite com "
        "browser_type/browser_fill_form. A ferramenta nunca confirma a compra ou o pagamento e devolve somente "
        "o resultado do preenchimento, nunca os valores informados pelo usuário."
    ),
    "inputSchema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "site": {
                "type": "string",
                "description": "Domínio ou nome do serviço mostrado ao usuário.",
                "maxLength": 200,
            },
            "purpose": {
                "type": "string",
                "description": "Motivo curto para preencher o cartão, sem incluir dados secretos.",
                "maxLength": 300,
            },
            "request_token": {
                "type": "string",
                "description": (
                    "Identificador técnico único para vincular o formulário à conversa correta. "
                    "Gere um UUID novo em cada chamada."
                ),
                "pattern": "^[0-9a-fA-F-]{32,64}$",
                "maxLength": 64,
            },
            "cardholder_name_target": {
                "type": "string",
                "description": "Referência exata do campo de nome no cartão ou seletor único.",
                "maxLength": 1000,
            },
            "card_number_target": {
                "type": "string",
                "description": "Referência exata do campo de número do cartão ou seletor único.",
                "maxLength": 1000,
            },
            "expiration_target": {
                "type": "string",
                "description": "Referência do campo único de validade (MM/AA ou MM/AAAA).",
                "maxLength": 1000,
            },
            "expiration_month_target": {
                "type": "string",
                "description": "Referência do campo separado de mês da validade.",
                "maxLength": 1000,
            },
            "expiration_year_target": {
                "type": "string",
                "description": "Referência do campo separado de ano da validade.",
                "maxLength": 1000,
            },
            "security_code_target": {
                "type": "string",
                "description": "Referência exata do campo de CVV/CVC ou seletor único.",
                "maxLength": 1000,
            },
            "postal_code_target": {
                "type": "string",
                "description": "Referência do campo de CEP/código postal, quando solicitado.",
                "maxLength": 1000,
            },
        },
        "required": ["site", "request_token"],
        "additionalProperties": False,
    },
    "annotations": {
        "title": "Preencher cartão com segurança",
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
    },
}

initialize_message: dict[str, Any] | None = None
initialized_message: dict[str, Any] | None = None
post_url = ""
stream_generation = 0
stream_thread: threading.Thread | None = None
stream_ready = threading.Event()
stream_lock = threading.Lock()
emit_lock = threading.Lock()
suppressed_lock = threading.Lock()
suppressed_responses: dict[object, threading.Event] = {}
internal_responses: dict[object, dict[str, Any]] = {}
tool_list_requests: set[object] = set()
# Codex starts this adapter in the selected conversation's workspace. The
# shared HTTP server starts in the service account's home instead. Advertise
# that workspace through standard MCP roots when the client cannot do so;
# never disable Playwright's file checks or add other projects as roots.
workspace_root = Path.cwd().resolve()
provide_workspace_root = False


class BackendUnavailable(RuntimeError):
    pass


class CredentialRoutePending(RuntimeError):
    pass


def emit(message: object) -> None:
    with emit_lock:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def emit_error(message: dict[str, Any], detail: str) -> None:
    if "id" in message:
        emit({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": detail}})


def _deliver(event: dict[str, Any]) -> None:
    if event.get("method") == "roots/list" and "id" in event and provide_workspace_root:
        _post({
            "jsonrpc": "2.0",
            "id": event["id"],
            "result": {"roots": [{"uri": workspace_root.as_uri(), "name": workspace_root.name}]},
        })
        return
    response_id = event.get("id")
    with suppressed_lock:
        waiter = suppressed_responses.pop(response_id, None)
        internal = internal_responses.get(response_id)
    if waiter is not None:
        if internal is not None:
            internal["response"] = event
        waiter.set()
        return
    if response_id in tool_list_requests:
        tool_list_requests.discard(response_id)
        result = event.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if isinstance(tools, list):
            names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
            if CREDENTIAL_TOOL_NAME not in names:
                tools.append(CREDENTIAL_TOOL)
            if PAYMENT_CARD_TOOL_NAME not in names:
                tools.append(PAYMENT_CARD_TOOL)
    emit(event)


def _internal_tool_call(name: str, arguments: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    request_id = f"sasocq-internal-{uuid.uuid4()}"
    waiter = threading.Event()
    holder: dict[str, Any] = {}
    with suppressed_lock:
        suppressed_responses[request_id] = waiter
        internal_responses[request_id] = holder
    try:
        _post({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if not waiter.wait(timeout):
            raise BackendUnavailable(f"Playwright não respondeu à operação interna {name}")
        response = holder.get("response")
        if not isinstance(response, dict):
            raise BackendUnavailable(f"Playwright devolveu resposta inválida para {name}")
        if response.get("error"):
            raise BackendUnavailable(str(response["error"].get("message") or response["error"]))
        result = response.get("result") or {}
        if result.get("isError"):
            detail = "\n".join(
                str(item.get("text") or "") for item in result.get("content") or [] if isinstance(item, dict)
            ).strip()
            raise BackendUnavailable(detail or f"Playwright recusou {name}")
        return result
    finally:
        with suppressed_lock:
            suppressed_responses.pop(request_id, None)
            internal_responses.pop(request_id, None)


def _credential_schema(arguments: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    properties: dict[str, Any] = {}
    requested: list[tuple[str, str, str]] = []
    definitions = (
        ("login", "login_target", "Login", "textbox"),
        ("password", "password_target", "Senha", "textbox"),
        ("one_time_code", "one_time_code_target", "Código temporário", "textbox"),
    )
    for field, target_key, title, field_type in definitions:
        target = str(arguments.get(target_key) or "").strip()
        if not target:
            continue
        description = "Dado protegido SASOCQ. É usado uma única vez e não é mostrado ao Codex."
        properties[field] = {"type": "string", "title": title, "description": description}
        requested.append((field, target, field_type))
    if not requested:
        raise ValueError("informe pelo menos um campo de login, senha ou código temporário")
    return {"type": "object", "properties": properties, "required": [item[0] for item in requested]}, requested


def _payment_card_schema(arguments: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    properties: dict[str, Any] = {}
    requested: list[tuple[str, str, str]] = []
    definitions = (
        ("cardholder_name", "cardholder_name_target", "Nome no cartão", "textbox", 200),
        ("card_number", "card_number_target", "Número do cartão", "textbox", 32),
        ("expiration", "expiration_target", "Validade", "textbox", 9),
        ("expiration_month", "expiration_month_target", "Mês", "textbox", 2),
        ("expiration_year", "expiration_year_target", "Ano", "textbox", 4),
        ("security_code", "security_code_target", "CVV/CVC", "textbox", 8),
        ("postal_code", "postal_code_target", "CEP", "textbox", 32),
    )
    for field, target_key, title, field_type, max_length in definitions:
        target = str(arguments.get(target_key) or "").strip()
        if not target:
            continue
        properties[field] = {
            "type": "string",
            "title": title,
            "maxLength": max_length,
            "description": "Dado de pagamento protegido SASOCQ. É usado uma única vez e não é mostrado ao Codex.",
        }
        requested.append((field, target, field_type))
    if not requested:
        raise ValueError("informe pelo menos um campo do cartão de pagamento")
    return {"type": "object", "properties": properties, "required": [item[0] for item in requested]}, requested


def _credential_result(request_id: object, text: str, *, error: bool = False) -> None:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        result["isError"] = True
    emit({"jsonrpc": "2.0", "id": request_id, "result": result})


def _current_browser_site(fallback: str) -> str:
    """Prefer the real top-level page host over the model-supplied label."""
    try:
        result = _internal_tool_call("browser_evaluate", {"function": "() => location.href"}, timeout=15)
        text = "\n".join(
            str(item.get("text") or "") for item in result.get("content") or [] if isinstance(item, dict)
        )
        match = re.search(r"https?://[^\s\"'<>\\]+", text)
        if match:
            parsed = urllib.parse.urlparse(match.group(0))
            if parsed.hostname:
                return parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    except Exception:
        pass
    return fallback


def _current_browser_url() -> str:
    try:
        result = _internal_tool_call("browser_evaluate", {"function": "() => location.href"}, timeout=15)
        text = "\n".join(
            str(item.get("text") or "") for item in result.get("content") or [] if isinstance(item, dict)
        )
        match = re.search(r"https?://[^\s\"'<>\\]+", text)
        return match.group(0) if match else ""
    except Exception:
        return ""


def _payment_origin_is_secure(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _credential_bridge_json(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not CREDENTIAL_BRIDGE_SOCKET:
        raise RuntimeError("a ponte protegida desta release não foi configurada")
    message = json.dumps({"op": operation, **payload}, separators=(",", ":")).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(CREDENTIAL_BRIDGE_SOCKET)
        client.sendall(message)
        chunks = bytearray()
        while b"\n" not in chunks:
            chunk = client.recv(65536 - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) >= 65536:
                raise RuntimeError("a ponte protegida devolveu uma resposta muito grande")
    raw = bytes(chunks).split(b"\n", 1)[0]
    value = json.loads(raw) if raw else {}
    if not isinstance(value, dict):
        raise RuntimeError("a ponte protegida devolveu uma resposta inválida")
    if value.get("error"):
        if value.get("status_code") == 409:
            raise CredentialRoutePending(str(value["error"]))
        raise RuntimeError(str(value["error"]))
    return value


def _request_protected_credentials(
    request_token: str,
    site: str,
    purpose: str,
    fields: list[str],
    *,
    kind: str = "credentials",
) -> dict[str, Any]:
    deadline = time.monotonic() + 300
    registered = False
    while time.monotonic() < deadline and not registered:
        try:
            _credential_bridge_json(
                "request",
                {"request_token": request_token, "site": site, "purpose": purpose, "fields": fields, "kind": kind},
            )
            registered = True
        except CredentialRoutePending:
            time.sleep(0.1)
    if not registered:
        raise TimeoutError("a conversa não registrou o formulário protegido")
    while time.monotonic() < deadline:
        answer = _credential_bridge_json("poll", {"request_token": request_token})
        if answer.get("status") == "waiting":
            time.sleep(0.2)
            continue
        return answer
    raise TimeoutError("A solicitação protegida de credenciais expirou após 5 minutos")


def _handle_credential_call(message: dict[str, Any]) -> None:
    request_id = message.get("id")
    params = message.get("params") or {}
    arguments = params.get("arguments") or {}
    secret_content: dict[str, Any] = {}
    try:
        if not isinstance(arguments, dict):
            raise ValueError("parâmetros de credenciais inválidos")
        schema, requested = _credential_schema(arguments)
        site = str(arguments.get("site") or "este site").strip()[:200]
        site = _current_browser_site(site)
        purpose = str(arguments.get("purpose") or "Concluir a autenticação solicitada").strip()[:300]
        request_token = str(arguments.get("request_token") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{32,64}", request_token):
            raise ValueError("request_token inválido; gere um UUID novo para esta chamada")
        answer = _request_protected_credentials(
            request_token,
            site,
            purpose,
            [field for field, _target, _field_type in requested],
        )
        if answer.get("action") != "accept":
            _credential_result(request_id, "Credenciais canceladas pelo usuário; nenhum campo foi preenchido.")
            return
        content = answer.get("content")
        if not isinstance(content, dict):
            raise ValueError("o formulário protegido não devolveu conteúdo válido")
        secret_content = content
        fields = []
        for field, target, field_type in requested:
            value = content.get(field)
            if not isinstance(value, str) or not value or len(value) > 8192:
                raise ValueError(f"o campo protegido {field} está vazio ou é inválido")
            fields.append({"target": target, "name": field, "type": field_type, "value": value})
        _internal_tool_call("browser_fill_form", {"fields": fields})
        submit_target = str(arguments.get("submit_target") or "").strip()
        if submit_target:
            _internal_tool_call("browser_click", {"target": submit_target, "element": "Entrar"})
        action = "preenchidas e enviadas" if submit_target else "preenchidas"
        _credential_result(request_id, f"Credenciais {action} com segurança em {site}. Os valores não foram expostos ao Codex.")
    except Exception as exc:
        safe_error = str(exc)
        for value in secret_content.values():
            if isinstance(value, str) and value:
                safe_error = safe_error.replace(value, "••••")
        _credential_result(request_id, f"Não foi possível preencher as credenciais: {safe_error}", error=True)
    finally:
        for key in list(secret_content):
            secret_content[key] = ""


def _handle_payment_card_call(message: dict[str, Any]) -> None:
    request_id = message.get("id")
    params = message.get("params") or {}
    arguments = params.get("arguments") or {}
    secret_content: dict[str, Any] = {}
    try:
        if not isinstance(arguments, dict):
            raise ValueError("parâmetros do cartão inválidos")
        schema, requested = _payment_card_schema(arguments)
        current_url = _current_browser_url()
        if not _payment_origin_is_secure(current_url):
            raise ValueError("dados de cartão só podem ser preenchidos em uma página HTTPS verificada")
        parsed = urllib.parse.urlparse(current_url)
        site = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
        purpose = str(arguments.get("purpose") or "Preencher os dados de pagamento solicitados").strip()[:300]
        request_token = str(arguments.get("request_token") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{32,64}", request_token):
            raise ValueError("request_token inválido; gere um UUID novo para esta chamada")
        answer = _request_protected_credentials(
            request_token,
            site,
            purpose,
            [field for field, _target, _field_type in requested],
            kind="payment_card",
        )
        if answer.get("action") != "accept":
            _credential_result(request_id, "Dados do cartão cancelados pelo usuário; nenhum campo foi preenchido.")
            return
        content = answer.get("content")
        if not isinstance(content, dict):
            raise ValueError("o formulário protegido não devolveu conteúdo válido")
        secret_content = content
        fields = []
        limits = {name: int(definition.get("maxLength") or 0) for name, definition in schema["properties"].items()}
        for field, target, field_type in requested:
            value = content.get(field)
            if not isinstance(value, str) or not value or len(value) > limits[field]:
                raise ValueError(f"o campo protegido {field} está vazio ou é inválido")
            fields.append({"target": target, "name": field, "type": field_type, "value": value})
        _internal_tool_call("browser_fill_form", {"fields": fields})
        _credential_result(
            request_id,
            f"Dados do cartão preenchidos com segurança em {site}. Nenhuma compra ou pagamento foi confirmado e os valores não foram expostos ao Codex.",
        )
    except Exception as exc:
        safe_error = str(exc)
        for value in secret_content.values():
            if isinstance(value, str) and value:
                safe_error = safe_error.replace(value, "••••")
        _credential_result(request_id, f"Não foi possível preencher os dados do cartão: {safe_error}", error=True)
    finally:
        for key in list(secret_content):
            secret_content[key] = ""


def _consume_sse(generation: int, ready: threading.Event) -> None:
    global post_url
    request = urllib.request.Request(SSE_URL, headers={"Accept": "text/event-stream"}, method="GET")
    event_name = ""
    data_lines: list[str] = []
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            for raw_line in response:
                if generation != stream_generation:
                    return
                line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif not line:
                    data = "\n".join(data_lines)
                    if event_name == "endpoint" and data:
                        post_url = urllib.parse.urljoin(BASE_URL, data)
                        ready.set()
                    elif data:
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            event = None
                        if isinstance(event, dict):
                            _deliver(event)
                    event_name = ""
                    data_lines = []
    except Exception:
        pass
    finally:
        if generation == stream_generation:
            post_url = ""
            ready.set()


def _invalidate_stream() -> None:
    global post_url, stream_generation, stream_thread, stream_ready
    with stream_lock:
        stream_generation += 1
        post_url = ""
        stream_thread = None
        stream_ready = threading.Event()


def _ensure_stream(timeout: float = 10.0) -> None:
    global stream_generation, stream_thread, stream_ready
    with stream_lock:
        if post_url and stream_thread and stream_thread.is_alive():
            return
        stream_generation += 1
        generation = stream_generation
        ready = threading.Event()
        stream_ready = ready
        stream_thread = threading.Thread(
            target=_consume_sse,
            args=(generation, ready),
            name="playwright-mcp-sse",
            daemon=True,
        )
        stream_thread.start()
    if not ready.wait(timeout) or not post_url:
        raise BackendUnavailable("o canal SSE do Playwright não ficou pronto")


def _post(message: dict[str, Any]) -> None:
    _ensure_stream()
    request = urllib.request.Request(
        post_url,
        data=json.dumps(message, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except Exception as exc:
        raise BackendUnavailable(str(exc)) from exc


def _safe_to_retry(message: dict[str, Any]) -> bool:
    if message.get("method") != "tools/call":
        return message.get("method") == "tools/list"
    params = message.get("params") or {}
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if name == "browser_tabs":
        return arguments.get("action") == "list"
    return name in SAFE_RETRY_TOOLS


def _recover_session() -> None:
    if initialize_message is None:
        raise BackendUnavailable("a inicialização MCP ainda não foi recebida")
    _invalidate_stream()
    _ensure_stream()
    response_id = initialize_message.get("id")
    waiter = threading.Event()
    with suppressed_lock:
        suppressed_responses[response_id] = waiter
    try:
        _post(initialize_message)
        if not waiter.wait(10):
            raise BackendUnavailable("a reinicialização MCP não respondeu")
    finally:
        with suppressed_lock:
            suppressed_responses.pop(response_id, None)
    if initialized_message is not None:
        _post(initialized_message)


def forward(message: dict[str, Any]) -> None:
    global initialize_message, initialized_message, provide_workspace_root
    method = message.get("method")
    if method == "initialize":
        params = dict(message.get("params") or {})
        capabilities = dict(params.get("capabilities") or {})
        # Respect clients with their own roots implementation, including an
        # explicitly empty root list. Only fill a missing capability.
        provide_workspace_root = "roots" not in capabilities and workspace_root != Path(workspace_root.anchor)
        if provide_workspace_root:
            capabilities["roots"] = {"listChanged": False}
            message = {**message, "params": {**params, "capabilities": capabilities}}
        initialize_message = message
    elif method == "notifications/initialized":
        initialized_message = message
    elif method == "tools/list" and "id" in message:
        tool_list_requests.add(message["id"])
    elif method == "tools/call":
        tool_name = str((message.get("params") or {}).get("name") or "")
        if tool_name == CREDENTIAL_TOOL_NAME:
            threading.Thread(target=_handle_credential_call, args=(message,), name="sasocq-credential-fill", daemon=True).start()
            return
        if tool_name == PAYMENT_CARD_TOOL_NAME:
            threading.Thread(target=_handle_payment_card_call, args=(message,), name="sasocq-payment-card-fill", daemon=True).start()
            return
    try:
        _post(message)
        return
    except BackendUnavailable as first_error:
        if method == "initialize":
            _invalidate_stream()
            try:
                _post(message)
            except BackendUnavailable as retry_error:
                emit_error(message, f"Playwright indisponível: {retry_error}")
            return
        try:
            _recover_session()
        except BackendUnavailable as recovery_error:
            emit_error(message, f"Playwright indisponível; autorrecuperação falhou: {recovery_error}")
            return
        if not _safe_to_retry(message):
            emit_error(
                message,
                "A sessão Playwright foi recuperada, mas a operação não foi repetida por segurança; solicite-a novamente.",
            )
            return
        try:
            _post(message)
        except BackendUnavailable as retry_error:
            emit_error(message, f"Playwright indisponível após recuperação: {retry_error}")
            return
        print(f"Playwright MCP recuperado após: {first_error}", file=sys.stderr, flush=True)


def main() -> int:
    for raw_line in sys.stdin:
        try:
            incoming = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(incoming, dict):
            forward(incoming)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
