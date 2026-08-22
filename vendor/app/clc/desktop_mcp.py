from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable

SERVER_NAME = "codex-linux-control-desktop"
SERVER_VERSION = "0.9.0"


def _run(argv: Iterable[str], timeout: float = 20, *, input_text: str | None = None) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(argv),
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, completed.stdout.strip()


def _session_type() -> str:
    value = os.environ.get("XDG_SESSION_TYPE", "").casefold()
    if value:
        return value
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def _ydotool_env() -> dict[str, str]:
    env = os.environ.copy()
    if not env.get("YDOTOOL_SOCKET"):
        runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        candidate = Path(runtime) / ".ydotool_socket"
        if candidate.exists():
            env["YDOTOOL_SOCKET"] = str(candidate)
    return env


def _run_env(argv: Iterable[str], timeout: float = 20, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            env=env or os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, completed.stdout.strip()


def desktop_status(_: Dict[str, Any]) -> Dict[str, Any]:
    session = _session_type()
    ysocket = _ydotool_env().get("YDOTOOL_SOCKET", "")
    return {
        "session_type": session,
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "display": os.environ.get("DISPLAY", ""),
        "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
        "at_spi": _atspi_available(),
        "screenshot_backend": _screenshot_backend(),
        "input_backend": "xdotool" if session == "x11" and shutil.which("xdotool") else ("ydotool" if shutil.which("ydotool") and ysocket else "unavailable"),
        "ydotool_socket": ysocket,
        "warning": "Controle de entrada é supervisionado e depende das permissões da sessão gráfica.",
    }


def _screenshot_backend() -> str:
    for command in ("grim", "gnome-screenshot", "scrot", "import"):
        if shutil.which(command):
            return command
    return "unavailable"


def capture_screenshot(path: Path | None = None) -> Path:
    target = path or Path(tempfile.mkstemp(prefix="clc-desktop-", suffix=".png")[1])
    backend = _screenshot_backend()
    commands: dict[str, list[str]] = {
        "grim": ["grim", str(target)],
        "gnome-screenshot": ["gnome-screenshot", "-f", str(target)],
        "scrot": ["scrot", str(target)],
        "import": ["import", "-window", "root", str(target)],
    }
    if backend == "unavailable":
        raise RuntimeError("Nenhum capturador de tela compatível foi encontrado")
    code, output = _run(commands[backend], timeout=30)
    if code != 0 or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(output or "Não foi possível capturar a tela")
    return target


def desktop_screenshot(args: Dict[str, Any]) -> Dict[str, Any]:
    path = capture_screenshot()
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    finally:
        path.unlink(missing_ok=True)
    return {
        "content": [
            {"type": "text", "text": "Captura atual da área de trabalho Linux."},
            {"type": "image", "data": payload, "mimeType": "image/png"},
        ]
    }


def _atspi_available() -> bool:
    try:
        import pyatspi  # type: ignore  # noqa: F401
    except Exception:
        return False
    return True


def desktop_accessibility_tree(args: Dict[str, Any]) -> str:
    try:
        import pyatspi  # type: ignore
    except Exception as exc:
        raise RuntimeError("AT-SPI não está disponível nesta sessão") from exc

    max_nodes = min(max(int(args.get("max_nodes", 220)), 20), 1000)
    max_depth = min(max(int(args.get("max_depth", 8)), 1), 20)
    desktop = pyatspi.Registry.getDesktop(0)
    lines: list[str] = []
    count = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal count
        if count >= max_nodes or depth > max_depth:
            return
        try:
            role = node.getRoleName() or "unknown"
        except Exception:
            role = "unknown"
        try:
            name = (node.name or "").strip().replace("\n", " ")
        except Exception:
            name = ""
        try:
            states = node.getState()
            state_names = [str(value) for value in states.getStates()]
        except Exception:
            state_names = []
        lines.append(f"{'  ' * depth}- {role}: {name or '(sem nome)'}" + (f" [{','.join(state_names[:5])}]" if state_names else ""))
        count += 1
        try:
            children = list(node)
        except Exception:
            children = []
        for child in children:
            if count >= max_nodes:
                break
            visit(child, depth + 1)

    for app in list(desktop):
        if count >= max_nodes:
            break
        visit(app, 0)
    if count >= max_nodes:
        lines.append(f"… limite de {max_nodes} nós atingido")
    return "\n".join(lines) or "Nenhum elemento acessível foi encontrado."


def desktop_list_windows(_: Dict[str, Any]) -> str:
    if shutil.which("wmctrl"):
        code, output = _run(["wmctrl", "-lx"])
        if code == 0 and output:
            return output
    if _atspi_available():
        return desktop_accessibility_tree({"max_nodes": 80, "max_depth": 2})
    raise RuntimeError("Não foi possível listar janelas: instale wmctrl ou habilite AT-SPI")


def desktop_focus_window(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query é obrigatório")
    if not shutil.which("wmctrl"):
        raise RuntimeError("wmctrl não está instalado")
    code, output = _run(["wmctrl", "-a", query])
    if code != 0:
        raise RuntimeError(output or f"Janela não encontrada: {query}")
    return f"Janela focalizada: {query}"


def desktop_open_application(args: Dict[str, Any]) -> str:
    target = str(args.get("target") or "").strip()
    if not target or len(target) > 2048:
        raise ValueError("target é obrigatório")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target) or target.startswith(("/", "~/")):
        code, output = _run(["xdg-open", os.path.expanduser(target)])
    elif re.fullmatch(r"[A-Za-z0-9_.+-]+", target):
        if shutil.which("gtk-launch"):
            code, output = _run(["gtk-launch", target])
            if code != 0 and shutil.which(target):
                code, output = _run([target])
        elif shutil.which(target):
            code, output = _run([target])
        else:
            raise RuntimeError("Aplicativo não localizado")
    else:
        raise ValueError("Use um endereço, caminho ou identificador simples de aplicativo")
    if code != 0:
        raise RuntimeError(output or "Não foi possível abrir o aplicativo")
    return f"Aplicativo/endereço aberto: {target}"


def _input_backend() -> str:
    if _session_type() == "x11" and shutil.which("xdotool"):
        return "xdotool"
    env = _ydotool_env()
    if shutil.which("ydotool") and env.get("YDOTOOL_SOCKET") and Path(env["YDOTOOL_SOCKET"]).exists():
        return "ydotool"
    raise RuntimeError("Nenhum backend de entrada está disponível nesta sessão")


def desktop_click(args: Dict[str, Any]) -> str:
    x = int(args.get("x"))
    y = int(args.get("y"))
    button = min(max(int(args.get("button", 1)), 1), 5)
    backend = _input_backend()
    if backend == "xdotool":
        code, output = _run(["xdotool", "mousemove", "--sync", str(x), str(y), "click", str(button)])
    else:
        env = _ydotool_env()
        code, output = _run_env(["ydotool", "mousemove", "--absolute", str(x), str(y)], env=env)
        if code == 0:
            # Linux BTN_LEFT/RIGHT/MIDDLE event combinations used by ydotool.
            masks = {1: "0xC0", 2: "0xC2", 3: "0xC1", 4: "0xC3", 5: "0xC4"}
            code, output = _run_env(["ydotool", "click", masks[button]], env=env)
    if code != 0:
        raise RuntimeError(output or "Falha ao clicar")
    return f"Clique executado em ({x}, {y}), botão {button}."


def desktop_type_text(args: Dict[str, Any]) -> str:
    text = str(args.get("text") or "")
    if not text or len(text) > 10_000:
        raise ValueError("text deve conter entre 1 e 10000 caracteres")
    backend = _input_backend()
    if backend == "xdotool":
        code, output = _run(["xdotool", "type", "--clearmodifiers", "--delay", "12", "--", text], timeout=60)
    else:
        code, output = _run_env(["ydotool", "type", "--key-delay", "12", "--", text], timeout=60, env=_ydotool_env())
    if code != 0:
        raise RuntimeError(output or "Falha ao digitar")
    return f"Texto digitado ({len(text)} caracteres)."


_LINUX_KEYCODES: dict[str, int] = {
    "ESC": 1, "ESCAPE": 1, "BACKSPACE": 14, "TAB": 15, "ENTER": 28, "RETURN": 28,
    "CTRL": 29, "CONTROL": 29, "LEFTCTRL": 29, "SHIFT": 42, "LEFTSHIFT": 42,
    "ALT": 56, "LEFTALT": 56, "SPACE": 57, "HOME": 102, "UP": 103, "PAGEUP": 104,
    "LEFT": 105, "RIGHT": 106, "END": 107, "DOWN": 108, "PAGEDOWN": 109,
    "INSERT": 110, "DELETE": 111, "SUPER": 125, "META": 125, "WIN": 125,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64,
    "F7": 65, "F8": 66, "F9": 67, "F10": 68, "F11": 87, "F12": 88,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21, "U": 22, "I": 23, "O": 24, "P": 25,
    "A": 30, "S": 31, "D": 32, "F": 33, "G": 34, "H": 35, "J": 36, "K": 37, "L": 38,
    "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48, "N": 49, "M": 50,
}


def _ydotool_key_sequence(keys: str) -> list[str]:
    tokens = [token.strip().upper() for token in re.split(r"[+\s]+", keys) if token.strip()]
    if not tokens:
        raise ValueError("Atalho vazio")
    try:
        codes = [_LINUX_KEYCODES[token] for token in tokens]
    except KeyError as exc:
        raise ValueError(f"Tecla ainda não suportada no Wayland: {exc.args[0]}") from exc
    modifiers = set(codes[:-1])
    sequence = [f"{code}:1" for code in codes]
    sequence.append(f"{codes[-1]}:0")
    sequence.extend(f"{code}:0" for code in reversed(codes[:-1]) if code in modifiers)
    return sequence


def desktop_hotkey(args: Dict[str, Any]) -> str:
    keys = str(args.get("keys") or "").strip()
    if not keys or len(keys) > 120:
        raise ValueError("keys é obrigatório")
    backend = _input_backend()
    if backend == "xdotool":
        code, output = _run(["xdotool", "key", "--clearmodifiers", keys])
    else:
        code, output = _run_env(["ydotool", "key", *_ydotool_key_sequence(keys)], env=_ydotool_env())
    if code != 0:
        raise RuntimeError(output or "Falha ao enviar atalho")
    return f"Atalho enviado: {keys}"


def desktop_scroll(args: Dict[str, Any]) -> str:
    direction = str(args.get("direction") or "down").casefold()
    amount = min(max(int(args.get("amount", 3)), 1), 30)
    if direction not in {"up", "down"}:
        raise ValueError("direction deve ser up ou down")
    button = 4 if direction == "up" else 5
    backend = _input_backend()
    if backend == "xdotool":
        command = ["xdotool", "click", "--repeat", str(amount), "--delay", "35", str(button)]
        code, output = _run(command)
    else:
        masks = {4: "0xC3", 5: "0xC4"}
        code, output = _run_env(["ydotool", "click", "--repeat", str(amount), masks[button]], env=_ydotool_env())
    if code != 0:
        raise RuntimeError(output or "Falha ao rolar")
    return f"Rolagem {direction} executada {amount} vez(es)."


def desktop_clipboard_read(_: Dict[str, Any]) -> str:
    if _session_type() == "wayland" and shutil.which("wl-paste"):
        code, output = _run(["wl-paste", "--no-newline"])
    elif shutil.which("xclip"):
        code, output = _run(["xclip", "-selection", "clipboard", "-o"])
    else:
        raise RuntimeError("Nenhuma ferramenta de área de transferência está disponível")
    if code != 0:
        raise RuntimeError(output or "Falha ao ler a área de transferência")
    return output


def desktop_clipboard_write(args: Dict[str, Any]) -> str:
    text = str(args.get("text") or "")
    if len(text) > 200_000:
        raise ValueError("Texto grande demais")
    try:
        if _session_type() == "wayland" and shutil.which("wl-copy"):
            completed = subprocess.run(["wl-copy"], input=text, text=True, timeout=20, check=False)
        elif shutil.which("xclip"):
            completed = subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, timeout=20, check=False)
        else:
            raise RuntimeError("Nenhuma ferramenta de área de transferência está disponível")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tempo excedido ao gravar a área de transferência") from exc
    if completed.returncode != 0:
        raise RuntimeError("Falha ao gravar a área de transferência")
    return f"Área de transferência atualizada ({len(text)} caracteres)."


def desktop_wait(args: Dict[str, Any]) -> str:
    seconds = min(max(float(args.get("seconds", 1)), 0), 30)
    time.sleep(seconds)
    return f"Aguardado {seconds:.1f} segundo(s)."


TOOLS: list[dict[str, Any]] = [
    {"name": "desktop_status", "description": "Inspect the current Linux graphical session and available control backends.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "desktop_screenshot", "description": "Capture the current Linux desktop. Read-only.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "desktop_accessibility_tree", "description": "Read the AT-SPI accessibility tree of visible Linux applications. Read-only.", "inputSchema": {"type": "object", "properties": {"max_nodes": {"type": "integer", "minimum": 20, "maximum": 1000}, "max_depth": {"type": "integer", "minimum": 1, "maximum": 20}}, "additionalProperties": False}},
    {"name": "desktop_list_windows", "description": "List desktop windows and application identifiers. Read-only.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "desktop_focus_window", "description": "Focus a Linux window by title. Requires user approval.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
    {"name": "desktop_open_application", "description": "Open an installed desktop application, file or URL. Requires user approval.", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"], "additionalProperties": False}},
    {"name": "desktop_click", "description": "Move the pointer and click a desktop coordinate. Requires user approval.", "inputSchema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1}}, "required": ["x", "y"], "additionalProperties": False}},
    {"name": "desktop_type_text", "description": "Type text into the focused Linux application. Requires user approval.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}},
    {"name": "desktop_hotkey", "description": "Send an X11 symbolic keyboard shortcut such as ctrl+l. Requires user approval.", "inputSchema": {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"], "additionalProperties": False}},
    {"name": "desktop_scroll", "description": "Scroll the active desktop application. Requires user approval.", "inputSchema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "integer", "minimum": 1, "maximum": 30}}, "additionalProperties": False}},
    {"name": "desktop_clipboard_read", "description": "Read the Linux clipboard. Read-only but may expose sensitive content.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "desktop_clipboard_write", "description": "Replace the Linux clipboard text. Requires user approval.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}},
    {"name": "desktop_wait", "description": "Wait briefly for a graphical application to update.", "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number", "minimum": 0, "maximum": 30}}, "additionalProperties": False}},
]

HANDLERS: dict[str, Callable[[Dict[str, Any]], Any]] = {
    "desktop_status": desktop_status,
    "desktop_screenshot": desktop_screenshot,
    "desktop_accessibility_tree": desktop_accessibility_tree,
    "desktop_list_windows": desktop_list_windows,
    "desktop_focus_window": desktop_focus_window,
    "desktop_open_application": desktop_open_application,
    "desktop_click": desktop_click,
    "desktop_type_text": desktop_type_text,
    "desktop_hotkey": desktop_hotkey,
    "desktop_scroll": desktop_scroll,
    "desktop_clipboard_read": desktop_clipboard_read,
    "desktop_clipboard_write": desktop_clipboard_write,
    "desktop_wait": desktop_wait,
}


def _text_result(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict) and "content" in value:
        return value
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(message: Dict[str, Any]) -> Dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if request_id is None:
        return None
    if method == "initialize":
        requested = str(params.get("protocolVersion") or "2025-06-18")
        return {"id": request_id, "result": {"protocolVersion": requested, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method == "ping":
        return {"id": request_id, "result": {}}
    if method == "tools/list":
        return {"id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            return {"id": request_id, "error": {"code": -32602, "message": f"Ferramenta desconhecida: {name}"}}
        try:
            result = _text_result(handler(arguments))
        except Exception as exc:  # noqa: BLE001 - MCP tool errors are returned to Codex
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"id": request_id, "result": result}
    return {"id": request_id, "error": {"code": -32601, "message": f"Método não implementado: {method}"}}


def main() -> int:
    for raw in sys.stdin:
        try:
            message = json.loads(raw)
            response = handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            print(f"desktop-mcp: {exc}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
