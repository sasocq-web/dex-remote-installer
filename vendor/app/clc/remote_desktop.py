from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Settings


MIN_WIDTH = 640
MIN_HEIGHT = 480
MAX_WIDTH = 2560
MAX_HEIGHT = 1600
DEFAULT_DISPLAY_RANGE = range(79, 100)


@dataclass(frozen=True)
class AdaptiveGeometry:
    width: int
    height: int
    profile: str
    orientation: str
    device_scale_factor: float
    touch: bool

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _even(value: float) -> int:
    integer = int(round(value))
    return integer if integer % 2 == 0 else integer + 1


def adaptive_geometry(
    viewport_width: int,
    viewport_height: int,
    *,
    device_type: str = "auto",
    orientation: str = "auto",
    touch: bool = False,
    device_pixel_ratio: float = 1.0,
) -> AdaptiveGeometry:
    """Choose a readable virtual desktop for phones, tablets and computers.

    The virtual framebuffer is intentionally larger than a phone's CSS viewport.
    noVNC scales it to the available area, while Chromium uses a device scale
    factor so responsive websites receive a true mobile-sized CSS viewport.
    """

    width = max(240, min(int(viewport_width or 0), 7680))
    height = max(320, min(int(viewport_height or 0), 7680))
    kind = str(device_type or "auto").strip().casefold()
    requested_orientation = str(orientation or "auto").strip().casefold()
    if requested_orientation not in {"portrait", "landscape"}:
        requested_orientation = "portrait" if height >= width else "landscape"

    shortest = min(width, height)
    longest = max(width, height)
    coarse_phone = bool(touch and shortest <= 600 and longest <= 1200)
    coarse_tablet = bool(touch and shortest <= 1024 and longest <= 1600)
    if kind in {"phone", "mobile", "celular"} or (kind == "auto" and coarse_phone):
        profile = "phone"
        logical_short, logical_long = 720, 1280
        scale = 2.0
    elif kind in {"tablet", "tab"} or (kind == "auto" and coarse_tablet):
        profile = "tablet"
        logical_short, logical_long = 1024, 1366
        scale = 1.5
    else:
        profile = "desktop"
        # A desktop uses the actual viewport, bounded to avoid excessive video memory.
        logical_short = max(MIN_HEIGHT, min(shortest, MAX_HEIGHT))
        logical_long = max(1024, min(longest, MAX_WIDTH))
        scale = max(1.0, min(float(device_pixel_ratio or 1.0), 1.5))

    if requested_orientation == "portrait":
        target_width, target_height = logical_short, logical_long
    else:
        target_width, target_height = logical_long, logical_short

    target_width = _even(max(MIN_WIDTH, min(target_width, MAX_WIDTH)))
    target_height = _even(max(MIN_HEIGHT, min(target_height, MAX_HEIGHT)))
    return AdaptiveGeometry(
        width=target_width,
        height=target_height,
        profile=profile,
        orientation=requested_orientation,
        device_scale_factor=scale,
        touch=bool(touch or profile in {"phone", "tablet"}),
    )


def find_novnc_web_root() -> Optional[Path]:
    candidates = [
        Path("/usr/share/novnc"),
        Path("/usr/share/noVNC"),
        Path("/usr/lib/novnc"),
        Path("/usr/local/share/novnc"),
    ]
    configured = os.environ.get("CLC_NOVNC_DIR", "").strip()
    if configured:
        candidates.insert(0, Path(os.path.expanduser(configured)))
    for candidate in candidates:
        if (candidate / "vnc.html").is_file() and (candidate / "core" / "rfb.js").is_file():
            return candidate.resolve()
    return None


def novnc_inline_script_csp_hashes(filename: str = "vnc.html") -> tuple[str, ...]:
    """Return CSP SHA-256 digests for an approved noVNC inline loader."""

    root = find_novnc_web_root()
    if not root or filename not in {"vnc.html", "vnc_lite.html"}:
        return ()
    try:
        html = (root / filename).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    hashes: list[str] = []
    for match in re.finditer(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>", html, flags=re.IGNORECASE | re.DOTALL):
        if re.search(r"\bsrc\s*=", match.group("attrs"), flags=re.IGNORECASE):
            continue
        digest = hashlib.sha256(match.group("body").encode("utf-8")).digest()
        hashes.append(base64.b64encode(digest).decode("ascii"))
    return tuple(hashes)


def _find_xvnc() -> Optional[str]:
    return shutil.which("Xtigervnc") or shutil.which("Xvnc")


def _find_chromium(settings: Settings) -> Optional[Path]:
    # The supervised browser is user-facing through the live viewer, so prefer
    # the installed Google Chrome Stable in both the regular and recovery
    # sessions.  A dedicated profile keeps Chrome's remote-debugging policy
    # independent from the user's normal browser profile.  The Playwright build
    # remains an offline-compatible last resort, followed by Ubuntu Chromium.
    for command in ("google-chrome-stable", "google-chrome"):
        found = shutil.which(command)
        if found:
            return Path(found)
    browsers = settings.resolved_playwright_browsers_dir
    patterns = (
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    )
    for pattern in patterns:
        for candidate in sorted(browsers.glob(pattern), reverse=True):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    for command in ("chromium", "chromium-browser"):
        found = shutil.which(command)
        if found:
            return Path(found)
    return None


class RemoteDesktopManager:
    """Owns an isolated TigerVNC X11 session behind a private Unix socket."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._browser_lock = asyncio.Lock()
        self._x_process: Optional[asyncio.subprocess.Process] = None
        self._desktop_process: Optional[asyncio.subprocess.Process] = None
        self._browser_process: Optional[asyncio.subprocess.Process] = None
        self._display_number: Optional[int] = None
        self._geometry = adaptive_geometry(1440, 900, device_type="desktop")
        self._started_at = 0.0
        self._last_error = ""
        self._clients = 0
        self._last_client_at = 0.0
        self._browser_mode = ""
        self._browser_external = False
        self._owner_lock_handle: Optional[Any] = None
        self._owns_runtime = False
        self._external_owner_pid: Optional[int] = None

    @property
    def runtime_dir(self) -> Path:
        base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        if not base.exists() or not os.access(base, os.W_OK):
            base = Path.home() / ".cache"
        return (base / "codex-linux-control" / "remote-desktop").resolve()

    @property
    def state_dir(self) -> Path:
        return self.settings.resolved_remote_desktop_dir

    @property
    def socket_path(self) -> Path:
        return self.runtime_dir / "vnc.sock"

    @property
    def xauthority_path(self) -> Path:
        return self.runtime_dir / "Xauthority"

    @property
    def owner_lock_path(self) -> Path:
        return self.runtime_dir / "owner.lock"

    @property
    def owner_state_path(self) -> Path:
        return self.runtime_dir / "owner.json"

    @property
    def display(self) -> str:
        return f":{self._display_number}" if self._display_number is not None else ""

    @property
    def running(self) -> bool:
        if self._x_process and self._x_process.returncode is None:
            return self.socket_path.is_socket()
        return bool(
            self._external_owner_pid
            and self._pid_alive(self._external_owner_pid)
            and self.socket_path.is_socket()
        )

    @property
    def browser_running(self) -> bool:
        return bool(self._browser_external or (self._browser_process and self._browser_process.returncode is None))

    async def browser_cdp_ready(self, timeout: float = 1.0) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 9223), timeout=max(0.1, timeout)
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def _wait_browser_cdp(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.browser_running:
                code = self._browser_process.returncode if self._browser_process else "ausente"
                raise RuntimeError(f"O Chromium supervisionado encerrou durante a inicialização (código {code})")
            if await self.browser_cdp_ready(timeout=0.5):
                return
            await asyncio.sleep(0.15)
        raise RuntimeError("O Chromium supervisionado não abriu a porta CDP 9223")

    def status(self) -> Dict[str, Any]:
        xvnc = _find_xvnc()
        novnc = find_novnc_web_root()
        chromium = _find_chromium(self.settings)
        return {
            "available": bool(xvnc and novnc),
            "running": self.running,
            "display": self.display,
            "socket_private": self.socket_path.exists() and (self.socket_path.stat().st_mode & 0o777) == 0o600 if self.socket_path.exists() else False,
            "geometry": self._geometry.as_dict(),
            "clients": self._clients,
            "started_at": self._started_at,
            "last_client_at": self._last_client_at,
            "last_error": self._last_error,
            "xvnc": xvnc or "",
            "novnc_root": str(novnc or ""),
            "chromium": str(chromium or ""),
            "browser_mode": self._browser_mode if self.browser_running else "",
            "browser_running": self.browser_running,
            "browser_pid": int(self._browser_process.pid) if self._browser_process and self._browser_process.returncode is None else 0,
            "runtime_owner": "self" if self._owns_runtime else "external" if self._external_owner_pid else "none",
            "runtime_owner_pid": int(self._x_process.pid) if self._owns_runtime and self._x_process else int(self._external_owner_pid or 0),
            "transport": "authenticated-wss-to-unix-socket",
            "tcp_vnc_exposed": False,
        }

    async def _virtual_keyboard_call(self, method: str, *arguments: str) -> tuple[int, str]:
        if not self.running:
            raise RuntimeError("A sessão gráfica remota não está ativa")
        gdbus = shutil.which("gdbus")
        if not gdbus:
            raise RuntimeError("O cliente D-Bus do teclado virtual não está instalado")
        environment = self._environment()
        user_bus = self.runtime_dir.parent.parent / "bus"
        if user_bus.exists():
            environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={user_bus}"
        process = await asyncio.create_subprocess_exec(
            gdbus,
            "call",
            "--session",
            "--dest", "org.onboard.Onboard",
            "--object-path", "/org/onboard/Onboard/Keyboard",
            "--method", method,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
        )
        output, _ = await process.communicate()
        return process.returncode or 0, output.decode("utf-8", errors="replace").strip()

    async def virtual_keyboard_status(self) -> Dict[str, Any]:
        """Read Onboard's authoritative visibility property over D-Bus."""
        return_code, detail = await self._virtual_keyboard_call(
            "org.freedesktop.DBus.Properties.Get",
            "org.onboard.Onboard.Keyboard",
            "Visible",
        )
        if return_code != 0:
            return {"ok": True, "visible": False, "keyboard": "onboard", "display": self.display, "running": False}
        visible = bool(re.search(r"\btrue\b", detail, flags=re.IGNORECASE))
        return {"ok": True, "visible": visible, "keyboard": "onboard", "display": self.display, "running": True}

    async def set_virtual_keyboard_visible(self, visible: bool = True) -> Dict[str, Any]:
        """Show or hide Onboard inside the isolated VNC session over D-Bus."""
        method = "org.onboard.Onboard.Keyboard.Show" if visible else "org.onboard.Onboard.Keyboard.Hide"
        return_code, detail = await self._virtual_keyboard_call(method)
        if return_code != 0 and visible:
            onboard = shutil.which("onboard")
            if not onboard:
                raise RuntimeError("O teclado virtual Onboard não está instalado")
            layout = "Phone" if self._geometry.profile == "phone" else "Compact"
            await self._launch([onboard, f"--layout={layout}", "--quirks=metacity"])
            for _attempt in range(20):
                await asyncio.sleep(0.15)
                return_code, detail = await self._virtual_keyboard_call(method)
                if return_code == 0:
                    break
        if return_code != 0 and not visible:
            return {"ok": True, "visible": False, "keyboard": "onboard", "display": self.display, "running": False}
        if return_code != 0:
            raise RuntimeError(detail or "O teclado virtual do Ubuntu não respondeu")
        status = await self.virtual_keyboard_status()
        for _attempt in range(20):
            if bool(status.get("visible")) == bool(visible):
                break
            await asyncio.sleep(0.05)
            status = await self.virtual_keyboard_status()
        return {**status, "visible": bool(status.get("visible", visible))}

    async def toggle_virtual_keyboard(self) -> Dict[str, Any]:
        """Toggle from Onboard's real state, including auto-show changes."""
        status = await self.virtual_keyboard_status()
        return await self.set_virtual_keyboard_visible(not bool(status.get("visible")))

    async def _point_inside_virtual_keyboard(self, x: float, y: float) -> bool:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return False
        process = await asyncio.create_subprocess_exec(
            xdotool,
            "search", "--onlyvisible", "--class", "Onboard",
            "getwindowgeometry", "--shell", "%@",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._environment(),
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            return False
        geometry: dict[str, int] = {}
        rectangles: list[dict[str, int]] = []
        for line in output.decode("utf-8", errors="replace").splitlines() + ["WINDOW="]:
            key, separator, value = line.partition("=")
            if not separator:
                continue
            if key == "WINDOW" and geometry:
                rectangles.append(geometry)
                geometry = {}
            if key in {"X", "Y", "WIDTH", "HEIGHT"}:
                try:
                    geometry[key] = int(value)
                except ValueError:
                    pass
        return any(
            rect.get("X", 0) <= x < rect.get("X", 0) + rect.get("WIDTH", 0)
            and rect.get("Y", 0) <= y < rect.get("Y", 0) + rect.get("HEIGHT", 0)
            for rect in rectangles
        )

    async def _active_remote_window_title(self) -> str:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return ""
        process = await asyncio.create_subprocess_exec(
            xdotool, "getactivewindow", "getwindowname",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._environment(),
        )
        output, _ = await process.communicate()
        return output.decode("utf-8", errors="replace").strip() if process.returncode == 0 else ""

    async def auto_hide_virtual_keyboard(self, x: float, y: float) -> Dict[str, Any]:
        """Hide Onboard after a remote tap that is outside text entry and the keyboard."""
        status = await self.virtual_keyboard_status()
        if not status.get("visible"):
            return {**status, "changed": False, "reason": "already-hidden"}
        if await self._point_inside_virtual_keyboard(x, y):
            return {**status, "changed": False, "reason": "keyboard"}
        active_window_title = await self._active_remote_window_title()
        async with self._browser_lock:
            node = shutil.which("node")
            helper = Path(__file__).resolve().parent.parent / "system" / "browser-editable-at-point.cjs"
            if not node or not helper.is_file() or not self.browser_running or not await self.browser_cdp_ready():
                return {**status, "changed": False, "reason": "inspection-unavailable"}
            process = await asyncio.create_subprocess_exec(
                node, str(helper), str(float(x)), str(float(y)), active_window_title,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._environment(),
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return {**status, "changed": False, "reason": "inspection-timeout"}
        text = output.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            return {**status, "changed": False, "reason": "inspection-failed"}
        try:
            inspected = json.loads(text.splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return {**status, "changed": False, "reason": "inspection-invalid"}
        if inspected.get("editable"):
            return {**status, "changed": False, "reason": "editable", "element": inspected.get("element", "")}
        hidden = await self.set_virtual_keyboard_visible(False)
        return {**hidden, "changed": True, "reason": "outside-editable", "element": inspected.get("element", "")}

    def _choose_display(self) -> int:
        if self._display_number is not None:
            return self._display_number
        for number in DEFAULT_DISPLAY_RANGE:
            if not Path(f"/tmp/.X{number}-lock").exists() and not Path(f"/tmp/.X11-unix/X{number}").exists():
                return number
        raise RuntimeError("Não há um display X virtual livre para a sessão remota")

    def _prepare_dirs(self) -> None:
        for path in (self.runtime_dir, self.state_dir, self.settings.resolved_remote_browser_profile_dir):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        self.socket_path.unlink(missing_ok=True)
        self.xauthority_path.unlink(missing_ok=True)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            return False

    def _acquire_owner_lock(self) -> bool:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.runtime_dir, 0o700)
        handle = open(self.owner_lock_path, "a+", encoding="utf-8")
        os.chmod(self.owner_lock_path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self._owner_lock_handle = handle
        self._owns_runtime = True
        return True

    def _release_owner_lock(self) -> None:
        handle = self._owner_lock_handle
        self._owner_lock_handle = None
        self._owns_runtime = False
        if not handle:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _write_owner_state(self) -> None:
        if not self._owns_runtime or not self._x_process or self._display_number is None:
            return
        temporary = self.owner_state_path.with_suffix(f".json.{os.getpid()}.tmp")
        payload = {
            "schema": 1,
            "pid": int(self._x_process.pid),
            "display": int(self._display_number),
            "socket": str(self.socket_path),
            "xauthority": str(self.xauthority_path),
            "geometry": self._geometry.as_dict(),
            "created_at": time.time(),
        }
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.owner_state_path)

    def _adopt_existing_owner(self) -> bool:
        try:
            payload = json.loads(self.owner_state_path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            display_number = int(payload["display"])
            geometry = payload.get("geometry") or {}
            if payload.get("socket") != str(self.socket_path):
                return False
            if payload.get("xauthority") != str(self.xauthority_path):
                return False
            if display_number not in DEFAULT_DISPLAY_RANGE:
                return False
            if not self._pid_alive(pid) or not self.socket_path.is_socket() or not self.xauthority_path.is_file():
                return False
            if geometry:
                self._geometry = AdaptiveGeometry(
                    width=int(geometry["width"]),
                    height=int(geometry["height"]),
                    profile=str(geometry["profile"]),
                    orientation=str(geometry["orientation"]),
                    device_scale_factor=float(geometry["device_scale_factor"]),
                    touch=bool(geometry["touch"]),
                )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        self._display_number = display_number
        self._external_owner_pid = pid
        self._started_at = float(payload.get("created_at") or time.time())
        self._last_error = ""
        return True

    def _prepare_xauthority(self, display_number: int) -> None:
        xauth = shutil.which("xauth")
        if not xauth:
            raise RuntimeError("O pacote xauth não está instalado")
        cookie = secrets.token_hex(16)
        result = subprocess.run(
            [xauth, "-f", str(self.xauthority_path), "add", f":{display_number}", "MIT-MAGIC-COOKIE-1", cookie],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Não foi possível proteger o display virtual: {result.stdout.strip()}")
        os.chmod(self.xauthority_path, 0o600)

    def _write_openbox_config(self) -> Path:
        path = self.state_dir / "openbox-rc.xml"
        font_size = 14 if self._geometry.profile == "phone" else 12 if self._geometry.profile == "tablet" else 10
        path.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <resistance><strength>12</strength><screen_edge_strength>24</screen_edge_strength></resistance>
  <focus><focusNew>yes</focusNew><followMouse>no</followMouse><raiseOnFocus>yes</raiseOnFocus></focus>
  <placement><policy>Smart</policy><center>yes</center><monitor>Primary</monitor></placement>
  <theme>
    <name>Clearlooks</name><titleLayout>NLIMC</titleLayout><keepBorder>yes</keepBorder>
    <font place="ActiveWindow"><name>Sans</name><size>{font_size}</size><weight>Bold</weight></font>
    <font place="InactiveWindow"><name>Sans</name><size>{font_size}</size><weight>Normal</weight></font>
    <font place="MenuHeader"><name>Sans</name><size>{font_size}</size><weight>Bold</weight></font>
    <font place="MenuItem"><name>Sans</name><size>{font_size}</size><weight>Normal</weight></font>
  </theme>
  <desktops><number>1</number><firstdesk>1</firstdesk></desktops>
  <margins><top>0</top><bottom>0</bottom><left>0</left><right>0</right></margins>
  <applications>
    <application class="Chromium*"><maximized>yes</maximized><focus>yes</focus></application>
    <application class="Google-chrome*"><maximized>yes</maximized><focus>yes</focus></application>
    <application class="Gnome-shell*"><maximized>yes</maximized><focus>yes</focus></application>
    <application name="gnome-shell"><maximized>yes</maximized><focus>yes</focus></application>
  </applications>
</openbox_config>
""",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        return path

    def _write_session_script(self) -> Path:
        path = self.state_dir / "start-session.sh"
        openbox_config = self._write_openbox_config()
        show_panel = self._geometry.profile == "desktop"
        panel_command = "tint2 >/dev/null 2>&1 &" if show_panel else ":"
        onboard_layout = "Phone" if self._geometry.profile == "phone" else "Compact"
        path.write_text(
            f"""#!/bin/sh
set -u
umask 077
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=ubuntu:GNOME
export XDG_SESSION_DESKTOP=ubuntu
export DESKTOP_SESSION=ubuntu
export GNOME_SHELL_SESSION_MODE=ubuntu
export GDK_BACKEND=x11
export QT_QPA_PLATFORM=xcb
export NO_AT_BRIDGE=0
xsetroot -solid '#172033' >/dev/null 2>&1 || true
if command -v dbus-update-activation-environment >/dev/null 2>&1; then
  dbus-update-activation-environment --systemd DISPLAY XAUTHORITY XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP DESKTOP_SESSION >/dev/null 2>&1 || true
fi
openbox --config-file "{openbox_config}" &
wm_pid=$!
sleep 1
# Ubuntu's current GNOME session is Wayland-only. Run GNOME Shell as a nested
# Wayland compositor inside the private X/VNC display. This keeps the normal
# Ubuntu shell while preserving a separate, non-HDMI Codex desktop.
if command -v gnome-shell >/dev/null 2>&1; then
  session_uid="$(id -u)"
  cleanup_nested_gnome() {{
    pkill -u "$session_uid" -x gnome-shell >/dev/null 2>&1 || true
    pkill -u "$session_uid" -x Xwayland >/dev/null 2>&1 || true
    sleep 1
    pkill -KILL -u "$session_uid" -x gnome-shell >/dev/null 2>&1 || true
    pkill -KILL -u "$session_uid" -x Xwayland >/dev/null 2>&1 || true
  }}
  trap cleanup_nested_gnome EXIT HUP INT TERM
  cleanup_nested_gnome
  sleep 1
  mkdir -p "$HOME/.config"
  : > "$HOME/.config/gnome-initial-setup-done"
  if command -v gsettings >/dev/null 2>&1; then
    gsettings set org.gnome.desktop.session idle-delay 0 >/dev/null 2>&1 || true
    gsettings set org.gnome.desktop.screensaver lock-enabled false >/dev/null 2>&1 || true
  fi
  export MUTTER_DEBUG_DUMMY_MODE_SPECS="{self._geometry.width}x{self._geometry.height}"
  gnome-shell --devkit --mode=ubuntu --force-animations >/dev/null 2>&1 &
  sleep 5
  if pgrep -u "$session_uid" -x gnome-shell >/dev/null 2>&1; then
    while pgrep -u "$session_uid" -x gnome-shell >/dev/null 2>&1 && kill -0 "$wm_pid" >/dev/null 2>&1; do
      sleep 2
    done
    exit 0
  fi
  trap - EXIT HUP INT TERM
fi
# Minimal recovery desktop if GNOME Shell cannot run on the installed driver.
if command -v pcmanfm >/dev/null 2>&1; then
  pcmanfm --desktop --profile codex-linux-control >/dev/null 2>&1 &
fi
if command -v tint2 >/dev/null 2>&1; then
  {panel_command}
fi
if command -v onboard >/dev/null 2>&1; then
  if command -v gsettings >/dev/null 2>&1; then
    gsettings set org.gnome.desktop.interface toolkit-accessibility true >/dev/null 2>&1 || true
    gsettings set org.onboard start-minimized false >/dev/null 2>&1 || true
    gsettings set org.onboard show-status-icon false >/dev/null 2>&1 || true
    gsettings set org.onboard.auto-show enabled true >/dev/null 2>&1 || true
    gsettings set org.onboard.auto-show hide-on-key-press false >/dev/null 2>&1 || true
    gsettings set org.onboard.auto-show tablet-mode-detection-enabled false >/dev/null 2>&1 || true
    gsettings set org.onboard.auto-show keyboard-device-detection-enabled false >/dev/null 2>&1 || true
    gsettings set org.onboard.window docking-enabled true >/dev/null 2>&1 || true
    gsettings set org.onboard.window docking-edge bottom >/dev/null 2>&1 || true
    gsettings set org.onboard.window force-to-top true >/dev/null 2>&1 || true
    gsettings set org.onboard.keyboard key-synth XTest >/dev/null 2>&1 || true
    gsettings set org.onboard.keyboard touch-input single >/dev/null 2>&1 || true
    gsettings set org.onboard.keyboard input-event-source GTK >/dev/null 2>&1 || true
  fi
  onboard --layout="{onboard_layout}" --quirks=metacity >/dev/null 2>&1 &
fi
wait "$wm_pid"
""",
            encoding="utf-8",
        )
        os.chmod(path, 0o700)
        return path

    def _environment(self) -> Dict[str, str]:
        env = os.environ.copy()
        profile = self._geometry.profile
        gdk_scale = "2" if profile == "phone" else "1"
        dpi_scale = "1" if profile == "phone" else "1.25" if profile == "tablet" else "1"
        qt_scale = "2" if profile == "phone" else "1.25" if profile == "tablet" else "1"
        cursor_size = "40" if profile == "phone" else "32" if profile == "tablet" else "24"
        env.update(
            {
                "DISPLAY": self.display,
                "XAUTHORITY": str(self.xauthority_path),
                "XDG_SESSION_TYPE": "x11",
                "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
                "XDG_SESSION_DESKTOP": "ubuntu",
                "DESKTOP_SESSION": "ubuntu",
                "GNOME_SHELL_SESSION_MODE": "ubuntu",
                "GDK_BACKEND": "x11",
                "GDK_SCALE": gdk_scale,
                "GDK_DPI_SCALE": dpi_scale,
                "QT_QPA_PLATFORM": "xcb",
                "QT_SCALE_FACTOR": qt_scale,
                "XCURSOR_SIZE": cursor_size,
                "NO_AT_BRIDGE": "0",
            }
        )
        return env

    async def _terminate_process(self, process: Optional[asyncio.subprocess.Process], timeout: float = 5.0) -> None:
        if not process or process.returncode is not None:
            return
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    async def start(self, geometry: Optional[AdaptiveGeometry] = None) -> Dict[str, Any]:
        async with self._lock:
            if geometry is not None:
                self._geometry = geometry
            if self.running:
                return self.status()
            await self._stop_unlocked()
            xvnc = _find_xvnc()
            novnc = find_novnc_web_root()
            if not xvnc:
                raise RuntimeError("TigerVNC não está instalado; use Instalar experiência completa")
            if not novnc:
                raise RuntimeError("noVNC não está instalado; use Instalar experiência completa")
            if not self._acquire_owner_lock():
                # The backend and the recovery service share one private VNC
                # endpoint. Only one process may create/unlink that endpoint;
                # later processes attach to the published owner instead.
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if self._adopt_existing_owner():
                        return self.status()
                    await asyncio.sleep(0.15)
                raise RuntimeError("Outra instância ainda está preparando a sessão gráfica privada")
            self._prepare_dirs()
            self._display_number = self._choose_display()
            self._prepare_xauthority(self._display_number)
            session_script = self._write_session_script()
            log_path = self.state_dir / "remote-desktop.log"
            log = open(log_path, "ab", buffering=0)
            os.chmod(log_path, 0o600)
            command = [
                xvnc,
                self.display,
                "-geometry",
                f"{self._geometry.width}x{self._geometry.height}",
                "-depth",
                "24",
                "-rfbport",
                "-1",
                "-rfbunixpath",
                str(self.socket_path),
                "-rfbunixmode",
                "0600",
                "-SecurityTypes",
                "None",
                "-AlwaysShared",
                "-DisconnectClients=0",
                "-auth",
                str(self.xauthority_path),
                "-nolisten",
                "tcp",
                "-extension",
                "GLX",
                "-desktop",
                "Codex Linux Control",
            ]
            try:
                self._x_process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self._environment(),
                )
            finally:
                log.close()

            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if self._x_process.returncode is not None:
                    break
                if self.socket_path.exists():
                    os.chmod(self.socket_path, 0o600)
                    break
                await asyncio.sleep(0.15)
            if not self.socket_path.exists() or self._x_process.returncode is not None:
                return_code = self._x_process.returncode
                self._last_error = f"TigerVNC não iniciou (código {return_code}); consulte {log_path}"
                await self._stop_unlocked()
                raise RuntimeError(self._last_error)

            self._write_owner_state()

            session_log_path = self.state_dir / "remote-session.log"
            session_log = open(session_log_path, "ab", buffering=0)
            os.chmod(session_log_path, 0o600)
            session_env = self._environment()
            user_bus = self.runtime_dir.parent.parent / "bus"
            if user_bus.exists():
                # GNOME 46+ starts its session through systemd --user.  A fresh
                # dbus-run-session bus cannot expose org.freedesktop.systemd1,
                # so use the real user bus while keeping X/VNC private.
                session_env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={user_bus}"
                session_command = [str(session_script)]
            else:
                dbus = shutil.which("dbus-run-session")
                session_command = [dbus, "--", str(session_script)] if dbus else [str(session_script)]
            try:
                self._desktop_process = await asyncio.create_subprocess_exec(
                    *session_command,
                    stdout=session_log,
                    stderr=asyncio.subprocess.STDOUT,
                    env=session_env,
                )
            finally:
                session_log.close()
            self._started_at = time.time()
            self._last_error = ""
            return self.status()

    async def _stop_unlocked(self) -> None:
        owns_runtime = self._owns_runtime
        await self._terminate_process(self._browser_process, timeout=3)
        await self._terminate_process(self._desktop_process, timeout=4)
        await self._terminate_process(self._x_process, timeout=5)
        self._browser_process = None
        self._desktop_process = None
        self._x_process = None
        self._clients = 0
        self._browser_mode = ""
        self._browser_external = False
        if owns_runtime:
            self.socket_path.unlink(missing_ok=True)
            self.xauthority_path.unlink(missing_ok=True)
            self.owner_state_path.unlink(missing_ok=True)
            self._release_owner_lock()
        self._external_owner_pid = None
        self._display_number = None

    async def stop(self) -> Dict[str, Any]:
        async with self._lock:
            await self._stop_unlocked()
            return self.status()

    async def resize(self, geometry: AdaptiveGeometry) -> Dict[str, Any]:
        if not self.running:
            return await self.start(geometry)
        async with self._lock:
            self._geometry = geometry
            xrandr = shutil.which("xrandr")
            if not xrandr:
                return {**self.status(), "resized": False, "resize_error": "xrandr não está instalado"}
            process = await asyncio.create_subprocess_exec(
                xrandr,
                "--display",
                self.display,
                "--fb",
                f"{geometry.width}x{geometry.height}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._environment(),
            )
            output, _ = await process.communicate()
            resized = process.returncode == 0
            browser_windows = 0
            browser_windows_fitted = False
            browser_resize_error = ""
            wmctrl = shutil.which("wmctrl")
            if resized and wmctrl:
                # Playwright resizes the current page after the viewer connects.
                # Mutating every Chromium top-level window here also maximizes
                # JavaScript pop-ups and hides authentication dialogs behind the
                # main window.  Keep this pass read-only and let the owning
                # BrowserContext adjust only its current page.
                await asyncio.sleep(0.1)
                listed = await asyncio.create_subprocess_exec(
                    wmctrl,
                    "-lx",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self._environment(),
                )
                listed_output, _ = await listed.communicate()
                window_ids = []
                if listed.returncode == 0:
                    for line in listed_output.decode("utf-8", errors="replace").splitlines():
                        parts = line.split(None, 4)
                        window_class = parts[2].casefold() if len(parts) >= 3 else ""
                        if "chromium" in window_class or "google-chrome" in window_class:
                            window_ids.append(parts[0])
                browser_windows = len(window_ids)
            return {
                **self.status(),
                "resized": resized,
                "resize_error": "" if resized else output.decode("utf-8", errors="replace").strip(),
                "browser_windows": browser_windows,
                "browser_windows_fitted": browser_windows_fitted,
                "browser_resize_error": browser_resize_error,
            }

    async def stabilize_browser_windows(self) -> Dict[str, Any]:
        """Remove the bootstrap blank window and fit Playwright to the VNC frame.

        Chromium is launched once with an initial ``about:blank`` window before
        Playwright creates its isolated context.  Keeping both top-level windows
        makes Openbox expose the blank bootstrap window over the user's page.
        Playwright can also resize its outer window after a viewport change, so
        re-apply the current framebuffer bounds only when they diverge.
        """
        if not self.running:
            return {"ok": True, "running": False, "closed_placeholders": 0, "fitted": 0}
        wmctrl = shutil.which("wmctrl")
        if not wmctrl:
            return {"ok": True, "running": True, "available": False, "closed_placeholders": 0, "fitted": 0}
        listed = await asyncio.create_subprocess_exec(
            wmctrl,
            "-lGx",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._environment(),
        )
        output, _ = await listed.communicate()
        if listed.returncode != 0:
            return {
                "ok": False,
                "running": True,
                "closed_placeholders": 0,
                "fitted": 0,
                "error": output.decode("utf-8", errors="replace").strip(),
            }
        windows: list[dict[str, Any]] = []
        for line in output.decode("utf-8", errors="replace").splitlines():
            parts = line.split(None, 9)
            if len(parts) < 10 or "chromium" not in f"{parts[6]} {parts[7]}".casefold():
                continue
            try:
                windows.append({
                    "id": parts[0],
                    "x": int(parts[2]),
                    "y": int(parts[3]),
                    "width": int(parts[4]),
                    "height": int(parts[5]),
                    "title": parts[9],
                })
            except ValueError:
                continue
        placeholders = [item for item in windows if item["title"].casefold().startswith("about:blank:")]
        closed = 0
        if len(windows) > 1:
            for item in placeholders:
                process = await asyncio.create_subprocess_exec(
                    wmctrl,
                    "-ic",
                    item["id"],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self._environment(),
                )
                await process.communicate()
                if process.returncode == 0:
                    closed += 1
        target_width = self._geometry.width
        target_height = self._geometry.height
        fitted = 0
        failures: list[str] = []
        for item in windows:
            if item in placeholders and closed:
                continue
            if (
                item["x"] == 0
                and item["y"] == 0
                and item["width"] == target_width
                and item["height"] == target_height
            ):
                continue
            operations = (
                ("-b", "remove,maximized_vert,maximized_horz"),
                ("-e", f"0,0,0,{target_width},{target_height}"),
                ("-b", "add,maximized_vert,maximized_horz"),
            )
            succeeded = True
            for option, value in operations:
                process = await asyncio.create_subprocess_exec(
                    wmctrl,
                    "-ir",
                    item["id"],
                    option,
                    value,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self._environment(),
                )
                adjusted, _ = await process.communicate()
                if process.returncode != 0:
                    succeeded = False
                    failures.append(adjusted.decode("utf-8", errors="replace").strip())
                    break
            if succeeded:
                fitted += 1
        return {
            "ok": not failures,
            "running": True,
            "available": True,
            "closed_placeholders": closed,
            "fitted": fitted,
            "error": " • ".join(item for item in failures if item),
        }

    def client_connected(self) -> None:
        self._clients += 1
        self._last_client_at = time.time()

    def client_disconnected(self) -> None:
        self._clients = max(0, self._clients - 1)
        self._last_client_at = time.time()

    async def _launch(self, argv: list[str], *, replace_browser: bool = False) -> int:
        if not self.running:
            await self.start(self._geometry)
        if replace_browser:
            await self._terminate_process(self._browser_process, timeout=3)
            self._browser_process = None
        log_path = self.state_dir / "remote-apps.log"
        log = open(log_path, "ab", buffering=0)
        os.chmod(log_path, 0o600)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                env=self._environment(),
                start_new_session=True,
            )
        finally:
            log.close()
        if replace_browser:
            self._browser_process = process
            self._browser_external = False
        return int(process.pid)

    async def launch_browser(self, *, mode: str = "auto", url: str = "about:blank") -> Dict[str, Any]:
        async with self._browser_lock:
            return await self._launch_browser_unlocked(mode=mode, url=url)

    async def ensure_browser(self, *, mode: str = "desktop", url: str = "about:blank") -> Dict[str, Any]:
        async with self._browser_lock:
            if await self.browser_cdp_ready():
                self._browser_external = not bool(self._browser_process and self._browser_process.returncode is None)
                self._browser_mode = self._browser_mode or mode
                return {
                    "ok": True,
                    "application": "browser",
                    "mode": self._browser_mode or mode,
                    "pid": int(self._browser_process.pid) if self._browser_process and self._browser_process.returncode is None else 0,
                    "geometry": self._geometry.as_dict(),
                    "reused": True,
                    "external": self._browser_external,
                }
            return await self._launch_browser_unlocked(mode=mode, url=url)

    async def browser_layout(self, mode: str = "status") -> Dict[str, Any]:
        """Inspect or change responsive emulation on the focused web page."""
        requested = str(mode or "status").strip().casefold()
        if requested not in {"status", "mobile", "desktop"}:
            raise ValueError("Modo de layout do navegador inválido")
        async with self._browser_lock:
            if not self.browser_running or not await self.browser_cdp_ready():
                if requested == "status":
                    return {"ok": True, "available": False, "mobile": False, "reason": "O navegador Playwright ainda não está aberto."}
                await self._launch_browser_unlocked(mode="desktop", url="about:blank")
            node = shutil.which("node")
            helper = Path(__file__).resolve().parent.parent / "system" / "set-browser-layout.cjs"
            if not node or not helper.is_file():
                raise RuntimeError("O alternador de layout do navegador não está instalado")
            process = await asyncio.create_subprocess_exec(
                node,
                str(helper),
                requested,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._environment(),
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=12)
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.wait()
                raise RuntimeError("O navegador demorou para alterar o layout da página") from exc
            text = output.decode("utf-8", errors="replace").strip()
            if process.returncode != 0:
                raise RuntimeError((text or "Falha ao alterar o layout da página")[-800:])
            try:
                result = json.loads(text.splitlines()[-1])
            except (IndexError, json.JSONDecodeError) as exc:
                raise RuntimeError("O navegador retornou um estado de layout inválido") from exc
            return {**result, "available": True, "mode": requested}

    async def _launch_browser_unlocked(self, *, mode: str = "auto", url: str = "about:blank") -> Dict[str, Any]:
        browser = _find_chromium(self.settings)
        if not browser:
            raise RuntimeError("O Google Chrome e o navegador gerenciado do Playwright não estão instalados")
        # Keep the supervised Playwright browser independent from the former
        # interactive browser profile.  Reusing that profile can restore old
        # pages during startup; a page stalled behind authentication then
        # blocks Playwright's CDP initialization for every conversation.
        # The former profile is intentionally preserved for rollback.
        profile_dir = (
            self.settings.home
            / ".local"
            / "share"
            / "codex-linux-control"
            / "playwright-live-profile"
        ).resolve()
        # Chromium on Ubuntu is normally a strictly confined Snap.  Its
        # sandbox cannot create the ProcessSingleton files below ~/.local,
        # even when Unix ownership and modes are correct.  Keep the managed
        # profile inside the Snap writable area in that case.
        if str(browser).startswith("/snap/") or (
            browser.name in {"chromium", "chromium-browser"} and Path("/snap/chromium/current").exists()
        ):
            profile_dir = (self.settings.home / "snap" / "chromium" / "common" / "codex-linux-control-profile").resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(profile_dir, 0o700)
        profile = str(mode or "auto").casefold()
        if profile == "auto":
            profile = self._geometry.profile
        if profile not in {"phone", "tablet", "desktop"}:
            profile = "desktop"
        scale = 2.0 if profile == "phone" else 1.5 if profile == "tablet" else 1.0
        mobile = profile in {"phone", "tablet"}
        user_agent = (
            "Mozilla/5.0 (Linux; Android 16; Mobile) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
            if profile == "phone"
            else "Mozilla/5.0 (Linux; Android 16; Tablet) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            if profile == "tablet"
            else ""
        )
        target = str(url or "about:blank").strip()
        if not (target.startswith("https://") or target.startswith("http://") or target in {"about:blank", "chrome://newtab/"}):
            target = "about:blank"
        args = [
            str(browser),
            f"--user-data-dir={profile_dir}",
            # Ubuntu 26.04 blocks Chromium's unprivileged user namespace for
            # unpackaged Playwright builds.  This browser already runs as the
            # non-root Codex user on an isolated X/VNC socket with no TCP VNC
            # exposure, so disable Chromium's nested sandbox for this session.
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            # This supervised profile is non-interactive and already isolated
            # from the user's regular browser.  Using the desktop secret
            # service would raise an "Unlock Login Keyring" modal over the
            # VNC viewer and intercept live mouse/keyboard input.
            "--password-store=basic",
            "--disable-features=PasswordManagerOnboarding",
            "--disable-session-crashed-bubble",
            "--disable-dev-shm-usage",
            # The private TigerVNC display has no reliable GLX/EGL timing.
            # Chromium's GPU process otherwise logs continuous EGL failures
            # and eventually crashes with SIGSEGV, taking CDP port 9223 down.
            "--disable-gpu",
            "--ozone-platform=x11",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9223",
            "--touch-events=enabled" if mobile else "--touch-events=auto",
            f"--force-device-scale-factor={scale}",
            "--window-position=0,0",
            f"--window-size={self._geometry.width},{self._geometry.height}",
            "--start-maximized",
        ]
        if user_agent:
            args.append(f"--user-agent={user_agent}")
        args.append(target)
        pid = await self._launch(args, replace_browser=True)
        self._browser_mode = profile
        await self._wait_browser_cdp()
        return {"ok": True, "application": "browser", "mode": profile, "pid": pid, "geometry": self._geometry.as_dict()}

    async def launch_application(self, application: str) -> Dict[str, Any]:
        name = str(application or "").casefold()
        if name == "browser":
            return await self.launch_browser(mode="auto")
        candidates: Dict[str, tuple[str, ...]] = {
            "files": ("pcmanfm", "thunar", "nautilus", "dolphin"),
            "settings": ("gnome-control-center", "systemsettings", "xfce4-settings-manager"),
            "monitor": ("gnome-system-monitor", "xfce4-taskmanager", "mate-system-monitor"),
        }
        if name not in candidates:
            raise ValueError("Aplicativo remoto não permitido")
        command = next((shutil.which(item) for item in candidates[name] if shutil.which(item)), None)
        if not command:
            raise RuntimeError("O aplicativo solicitado não está instalado")
        pid = await self._launch([command])
        return {"ok": True, "application": name, "pid": pid}


__all__ = [
    "AdaptiveGeometry",
    "RemoteDesktopManager",
    "adaptive_geometry",
    "find_novnc_web_root",
    "novnc_inline_script_csp_hashes",
]
