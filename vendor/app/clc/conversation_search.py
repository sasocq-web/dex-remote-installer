from __future__ import annotations

import re
import unicodedata
from typing import Any


GENERATED_REQUEST_MARKERS = ("Solicitação atual:", "Solicitação do usuário:")
GENERATED_PREFACES = (
    "Use preferencialmente os servidores MCP associados a esta conversa:",
    "A administração SASOCQ está associada a esta conversa.",
    "Referências selecionadas pelo operador:",
)
BROKEN_TITLE_PREFIXES = (
    "$clc-",
    "Você é o Codex do Sistema SASOCQ",
    "Você é o Codex de Projetos SASOCQ",
    "A administração SASOCQ está associada a esta conversa.",
    "Use preferencialmente os servidores MCP associados a esta conversa:",
)
CONVERSATION_TITLE_MAX_LENGTH = 52
CONVERSATION_TITLE_MAX_WORDS = 6
CONVERSATION_REQUEST_PREVIEW_MAX_LENGTH = 280


def normalize_search_text(value: Any) -> str:
    """Return a forgiving, accent-insensitive representation for UI search."""

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", text).strip().casefold()


def is_generated_conversation_envelope(value: Any) -> bool:
    text = str(value or "").strip()
    has_marker = any(marker in text for marker in GENERATED_REQUEST_MARKERS)
    has_preface = bool(re.match(r"^\$clc-[^\s]+", text, flags=re.IGNORECASE)) or any(
        preface in text for preface in GENERATED_PREFACES
    )
    return has_marker and has_preface


def visible_user_text(value: Any) -> str:
    """Remove the Control Plane envelope while preserving the operator request."""

    text = str(value or "").strip()
    if is_generated_conversation_envelope(text):
        positions = [
            (text.rfind(marker), marker)
            for marker in GENERATED_REQUEST_MARKERS
            if text.rfind(marker) >= 0
        ]
        if positions:
            index, marker = max(positions)
            text = text[index + len(marker) :].strip()
    references_marker = "Referências selecionadas pelo operador:"
    references_index = text.find(references_marker)
    if references_index >= 0:
        text = text[:references_index].strip()
    text = re.sub(r"^Direção antecipada da fila:\s*", "", text, flags=re.IGNORECASE).strip()
    return re.sub(r"^\$clc-[^\s]+\s*", "", text, flags=re.IGNORECASE).strip()


def broken_generated_title(value: Any) -> bool:
    """Return whether a persisted title came from the generated Dex envelope."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or text.casefold() in {"conversa sem título", "nova conversa"}:
        return True
    normalized = text.casefold()
    return any(normalized.startswith(prefix.casefold()) for prefix in BROKEN_TITLE_PREFIXES)


def compact_conversation_text(value: Any, max_length: int) -> str:
    text = re.sub(r"\s+", " ", str(value or ""))
    text = re.sub(r"^[\s:;,.–—-]+|[\s:;,.–—-]+$", "", text).strip()
    if len(text) <= max_length:
        return text
    shortened = text[: max_length + 1]
    shortened = re.sub(r"\s+\S*$", "", shortened).strip()
    return f"{shortened or text[:max_length].strip()}…"


def conversation_request_preview(value: Any) -> str:
    return compact_conversation_text(
        visible_user_text(value), CONVERSATION_REQUEST_PREVIEW_MAX_LENGTH
    )


def compact_conversation_title(value: Any) -> str:
    """Return a clean ChatGPT-style title with at most a few useful words."""

    text = re.sub(r"https?://\S+", "", str(value or ""), flags=re.IGNORECASE)
    text = re.sub(r"[`*_#>]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[\s:;,.!?–—-]+|[\s:;,.!?–—-]+$", "", text).strip()
    words = text.split()[:CONVERSATION_TITLE_MAX_WORDS]
    while len(words) > 2 and normalize_search_text(words[-1]) in {
        "a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "com", "para"
    }:
        words.pop()
    title = " ".join(words)
    if len(title) > CONVERSATION_TITLE_MAX_LENGTH:
        title = re.sub(
            r"\s+\S*$", "", title[: CONVERSATION_TITLE_MAX_LENGTH + 1]
        ).strip()
    return re.sub(r"[,:;.!?–—-]+$", "", title).strip()


def _action_conversation_title(noun: str, raw_target: Any) -> str:
    target = re.split(
        r"[.!?;]|\s+(?:para que|porque|pois|quando|assim como|de acordo com|sem que|em vez de|ao invés de)\s+",
        str(raw_target or ""),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    target = re.sub(
        r"\s+(?:com|usando)\s+(?:poucas palavras|um título curto|títulos curtos).*$",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(r"^(?:que\s+)?", "", target, flags=re.IGNORECASE).strip()
    if not target:
        return ""
    article = re.match(r"^(o|a|os|as|um|uma)\s+(.+)$", target, flags=re.IGNORECASE)
    connector = "de"
    if article:
        connector = {
            "o": "do", "a": "da", "os": "dos", "as": "das", "um": "de", "uma": "de"
        }.get(normalize_search_text(article.group(1)), "de")
        target = article.group(2)
    return compact_conversation_title(f"{noun} {connector} {target}")


def _polish_conversation_title(value: Any, fallback: str) -> str:
    title = str(value or "").strip()
    replacements = (
        (r"\bpc\b", "PC"),
        (r"\bchat\s*gpt\b", "ChatGPT"),
        (r"\bdex\b", "Dex"),
        (r"\bsasocq\b", "SASOCQ"),
        (r"\bkvm\b", "KVM"),
        (r"\bandroid\b", "Android"),
        (r"\bsteam\b", "Steam"),
        (r"\bpostgresql\b", "PostgreSQL"),
        (r"\bplaywright\b", "Playwright"),
        (r"\bwaydroid\b", "Waydroid"),
        (r"\bchrome\b", "Chrome"),
        (r"\bwhatsapp\b", "WhatsApp"),
    )
    for pattern, replacement in replacements:
        title = re.sub(pattern, replacement, title, flags=re.IGNORECASE)
    return title[:1].upper() + title[1:] if title else fallback


def conversation_title_from_request(value: Any, fallback: str = "Conversa") -> str:
    """Create a stable, short title from the request, never from the envelope."""

    request = re.sub(r"\s+", " ", visible_user_text(value)).strip()
    if not request or re.fullmatch(
        r"(?:sim|não|nao|ok|certo|pode|confirmo|continue|continuar|prossiga|isso)",
        request,
        flags=re.IGNORECASE,
    ):
        return fallback

    normalized = normalize_search_text(request)
    contextual_rules = (
        (("conversa", "mesmo nome"), (), "Títulos repetidos nas conversas"),
        (("titulo", "context"), ("mensagem", "conversa", "pedido"), "Títulos curtos e contextuais"),
        (("conversa",), ("pesquis", "busc"), "Busca de conversas"),
        (("erro", "conversa", "whatsapp"), (), "Erro na conversa do WhatsApp"),
        (("card", "navegacao ao vivo"), ("print", "ultima pagina"), "Prévia da navegação ao vivo"),
        (("chrom", "playwright"), (), "Chrome com Playwright"),
        (("playwright", "pedir", "toda hora"), (), "Permissão persistente do Playwright"),
        (("play store", "waydroid"), (), "Play Store no Waydroid"),
        (("conversa", "interromp"), (), "Interrupções nas conversas"),
        (("conversa", "janela"), (), "Janelas isoladas por conversa"),
        (("conexao remota", "resolucao"), (), "Resolução da conexão remota"),
        (("dois toques",), ("deslizar", "rolagem", "navegacao"), "Rolagem com dois toques"),
        (("orientar agora", "nao esta funcionando"), (), "Correção do Orientar agora"),
        (("atualiza",), ("indicador", "icone", "pendente"), "Indicador de atualizações"),
        (("concluid", "compact"), ("caixa", "cartao", "atividade"), "Atividades concluídas compactas"),
        (("anex",), ("andamento", "processamento", "execucao"), "Anexos durante a execução"),
        (("orienta", "fila"), (), "Controles de fila"),
    )
    for required, alternatives, title in contextual_rules:
        if all(fragment in normalized for fragment in required) and (
            not alternatives or any(fragment in normalized for fragment in alternatives)
        ):
            return title
    if "conversa" in normalized and any(
        fragment in normalized for fragment in ("pulando", "na frente", "antiga", "todas as conversas")
    ):
        return "Navegação entre conversas"

    cleaned_request = re.sub(
        r"^(?:por favor,?\s*)?(?:(?:eu\s+)?(?:quero|gostaria|preciso|necessito)\s+que|voc[eê]\s+pode|poderia|pode)\s+",
        "",
        request,
        flags=re.IGNORECASE,
    ).strip()
    actions = (
        (r"^(?:corrigir|corrija|corrigindo|consertar|conserte|reparar|repare)\s+(.+)", "Correção"),
        (r"^(?:melhor|melhorar|melhore|melhora|otimizar|otimize)\s+(.+)", "Melhoria"),
        (r"^(?:ajustar|ajuste|alterar|altere)\s+(.+)", "Ajuste"),
        (r"^(?:compactar|compacte|compactando)\s+(.+)", "Compactação"),
        (r"^(?:criar|crie|montar|monte|desenvolver|desenvolva)\s+(.+)", "Criação"),
        (r"^(?:adicionar|adicione|incluir|inclua)\s+(.+)", "Adição"),
        (r"^(?:instalar|instale)\s+(.+)", "Instalação"),
        (r"^(?:atualizar|atualize)\s+(.+)", "Atualização"),
        (r"^(?:configurar|configure)\s+(.+)", "Configuração"),
        (r"^(?:remover|remova|excluir|exclua)\s+(.+)", "Remoção"),
        (r"^(?:validar|valide|testar|teste)\s+(.+)", "Validação"),
        (r"^(?:analisar|analise|investigar|investigue)\s+(.+)", "Análise"),
    )
    for pattern, noun in actions:
        match = re.match(pattern, cleaned_request, flags=re.IGNORECASE)
        if match and match.group(1):
            title = _action_conversation_title(noun, match.group(1))
            if title:
                return _polish_conversation_title(title, fallback)

    broken = re.match(
        r"^((?:o|a|os|as)\s+.+?)\s+(?:não|nao)\s+(?:está|esta|estão|estao)?\s*(?:funcionando|abrindo|carregando|aparecendo|respondendo)",
        cleaned_request,
        flags=re.IGNORECASE,
    )
    if broken:
        return _polish_conversation_title(
            _action_conversation_title("Correção", broken.group(1)), fallback
        )

    topic = re.split(
        r"[.!?;]|\s+(?:porque|pois|assim como|de acordo com|para que)\s+",
        cleaned_request,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    topic = re.sub(
        r"^(?:como|qual|quais|onde|quando|por que|porque)\s+", "", topic, flags=re.IGNORECASE
    )
    topic = re.sub(
        r"\b(?:precisa|precisam|deve|devem|pode|podem)\s+(?:ser|ficar|ter)?\s*",
        "",
        topic,
        flags=re.IGNORECASE,
    ).strip()
    return _polish_conversation_title(compact_conversation_title(topic or request), fallback)


def first_meaningful_user_request(document: Any) -> str:
    if not isinstance(document, dict):
        return ""
    fallback = ""
    for value in document.get("users") or []:
        request = visible_user_text(value)
        if not request or broken_generated_title(request):
            continue
        if not fallback:
            fallback = request
        normalized = normalize_search_text(request)
        generic_reference = bool(re.fullmatch(
            r"(?:evitar|corrigir|resolver|fazer com que).*\b(?:isso|aquilo)\b.*", normalized
        ))
        if len(request.split()) >= 3 and not generic_reference and normalized not in {
            "resolver", "corrigir", "continue", "continuar", "prossiga", "tente novamente"
        }:
            return request
    return fallback


def is_injected_user_context(value: Any) -> bool:
    """Identify skill payloads injected beside, but distinct from, user requests."""

    text = str(value or "").strip()
    return bool(
        re.fullmatch(r"<skill>.*</skill>", text, flags=re.IGNORECASE | re.DOTALL)
        and "<name>" in text
        and "SKILL.md" in text
    )


def message_item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    if item.get("type") == "userMessage":
        pieces: list[str] = []
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if text is not None:
                pieces.append(str(text))
        return "\n".join(pieces).strip()
    if item.get("type") == "agentMessage":
        return str(item.get("text") or "").strip()
    return ""


def match_snippet(value: Any, query: Any, radius: int = 110) -> str:
    """Build a compact snippet around the first normalized match."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    normalized_text = normalize_search_text(text)
    normalized_query = normalize_search_text(query)
    index = normalized_text.find(normalized_query) if normalized_query else -1
    if index < 0:
        return text[: radius * 2].strip()

    # NFKD can shift offsets slightly. The nearby original slice is still the
    # most useful and deterministic preview, and avoids returning whole turns.
    start = max(0, index - radius)
    end = min(len(text), index + len(normalized_query) + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def thread_search_document(thread: Any) -> dict[str, Any]:
    """Extract only user-visible, bounded search fields from a full thread."""
    if not isinstance(thread, dict):
        return {"title": "", "users": [], "assistants": []}

    user_messages: list[str] = []
    assistant_messages: list[str] = []
    for turn in thread.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                raw_text = message_item_text(item)
                if is_injected_user_context(raw_text):
                    continue
                text = visible_user_text(raw_text)
                if text:
                    user_messages.append(text)
            elif item_type == "agentMessage" and item.get("phase") != "commentary":
                text = message_item_text(item)
                if text:
                    assistant_messages.append(text)

    preview = visible_user_text(thread.get("preview") or "")
    if preview and all(normalize_search_text(value) != normalize_search_text(preview) for value in user_messages):
        user_messages.insert(0, preview)
    return {
        "title": str(thread.get("name") or "").strip(),
        "users": user_messages,
        "assistants": assistant_messages,
    }


def classify_search_document(document: Any, query: Any) -> dict[str, str] | None:
    """Rank title, visible user requests, then final Codex responses."""

    if not isinstance(document, dict):
        return None
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return None

    title = str(document.get("title") or "").strip()
    if normalized_query in normalize_search_text(title):
        return {"kind": "title", "snippet": match_snippet(title, query)}

    for text in document.get("users") or []:
        if normalized_query in normalize_search_text(text):
            return {"kind": "user", "snippet": match_snippet(text, query)}
    for text in document.get("assistants") or []:
        if normalized_query in normalize_search_text(text):
            return {"kind": "assistant", "snippet": match_snippet(text, query)}
    return None


def classify_thread_match(thread: Any, query: Any) -> dict[str, str] | None:
    return classify_search_document(thread_search_document(thread), query)
