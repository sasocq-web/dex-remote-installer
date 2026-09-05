from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Mapping, Optional
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import HTTPException, Request, WebSocket, status
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .config import Settings


_CF_JWKS_LOCK = threading.RLock()
_CF_JWKS_CACHE: dict[str, tuple[float, dict]] = {}
_CF_JWKS_TTL_SECONDS = 3600
PLAYWRIGHT_AUTOMATION_IDENTITY = "internal:playwright-read-only"
PLAYWRIGHT_AUTOMATION_DEVICE_ID = "internal-playwright-read-only"
PLAYWRIGHT_ACCESS_COOKIE = "clc_playwright_access"
_PLAYWRIGHT_ACCESS_TOKEN = ""


def _valid_playwright_token(value: str) -> bool:
    return 48 <= len(value) <= 128 and all(character.isalnum() or character in {"-", "_"} for character in value)


def _read_protected_token(path, *, expected_uid: int) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid or info.st_mode & 0o077:
        raise RuntimeError("arquivo da identidade Playwright não está protegido por modo 0600")
    value = path.read_text(encoding="utf-8").strip()
    if not _valid_playwright_token(value):
        raise RuntimeError("token da identidade Playwright é inválido")
    return value


def provision_playwright_internal_access(settings: Settings) -> bool:
    """Provision a loopback-only, read-only browser identity without exposing its token."""

    global _PLAYWRIGHT_ACCESS_TOKEN
    if not settings.playwright_internal_access_enabled:
        _PLAYWRIGHT_ACCESS_TOKEN = ""
        return False
    uid = os.getuid()
    token_path = settings.resolved_playwright_access_token_file
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = _read_protected_token(token_path, expected_uid=uid)
    except FileNotFoundError:
        token = secrets.token_urlsafe(48)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(token_path, flags, 0o600)
        try:
            os.write(descriptor, (token + "\n").encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        token = _read_protected_token(token_path, expected_uid=uid)

    state_path = settings.resolved_browser_storage_state_file
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {"cookies": [], "origins": []}
    try:
        info = state_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
            raise RuntimeError("estado protegido do navegador possui proprietário inválido")
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("cookies"), list) and isinstance(loaded.get("origins"), list):
            state = loaded
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as exc:
        raise RuntimeError("estado protegido do navegador é inválido") from exc

    cookie = {
        "name": PLAYWRIGHT_ACCESS_COOKIE,
        "value": token,
        "domain": "127.0.0.1",
        "path": "/",
        "expires": int(time.time()) + 10 * 365 * 24 * 60 * 60,
        "httpOnly": True,
        "secure": False,
        "sameSite": "Strict",
    }
    cookies = [
        item for item in state["cookies"]
        if not (
            isinstance(item, dict)
            and item.get("name") == PLAYWRIGHT_ACCESS_COOKIE
            and item.get("domain") == "127.0.0.1"
            and item.get("path") == "/"
        )
    ]
    cookies.append(cookie)
    payload = json.dumps({"cookies": cookies, "origins": state["origins"]}, ensure_ascii=False, indent=2) + "\n"
    temporary = state_path.with_name(f".{state_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, state_path)
    os.chmod(state_path, 0o600)
    _PLAYWRIGHT_ACCESS_TOKEN = token
    return True


def is_playwright_automation_request(headers: Mapping[str, str], client_host: str | None, settings: Settings) -> bool:
    """Accept the protected cookie only on a direct loopback request, never through a proxy."""

    if not settings.playwright_internal_access_enabled or not _is_loopback(client_host):
        return False
    if any(
        headers.get(name)
        for name in (
            "cf-access-jwt-assertion", "cf-access-authenticated-user-email", "cf-connecting-ip",
            "tailscale-user-login", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
        )
    ):
        return False
    try:
        host = (urlparse("//" + headers.get("host", "")).hostname or "").casefold()
    except ValueError:
        return False
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    raw = headers.get("cookie", "")
    if not raw or len(raw) > 32_768:
        return False
    try:
        cookies = SimpleCookie()
        cookies.load(raw)
    except CookieError:
        return False
    item = cookies.get(PLAYWRIGHT_ACCESS_COOKIE)
    supplied = item.value.strip() if item is not None else ""
    expected = _PLAYWRIGHT_ACCESS_TOKEN
    if not expected:
        try:
            expected = _read_protected_token(settings.resolved_playwright_access_token_file, expected_uid=os.getuid())
        except (FileNotFoundError, OSError, RuntimeError):
            return False
    return bool(supplied and hmac.compare_digest(supplied, expected))


@dataclass
class Session:
    session_id: str
    csrf_token: str
    identity: str
    expires_at: float
    device_id: str = ""
    entra_subject: str = ""
    entra_email: str = ""
    entra_auth_time: float = 0.0
    strong_auth_time: float = 0.0
    auth_method: str = ""

    @property
    def entra_verified(self) -> bool:
        return bool(self.entra_subject and self.strong_auth_time)


class SessionStore:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}

    def create(self, identity: str, device_id: str = "") -> Session:
        now = time.time()
        self.cleanup(now)
        session = Session(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            identity=identity,
            expires_at=now + self._ttl,
            device_id=device_id,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: Optional[str]) -> Optional[Session]:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if not session:
            return None
        if session.expires_at <= time.time():
            self._sessions.pop(session_id, None)
            return None
        return session

    def touch(self, session: Session) -> None:
        session.expires_at = time.time() + self._ttl

    def cleanup(self, now: Optional[float] = None) -> None:
        current = now if now is not None else time.time()
        expired = [key for key, value in self._sessions.items() if value.expires_at <= current]
        for key in expired:
            self._sessions.pop(key, None)

    def revoke_device(self, device_id: str) -> int:
        keys = [key for key, value in self._sessions.items() if value.device_id == device_id]
        for key in keys:
            self._sessions.pop(key, None)
        return len(keys)

    def mark_entra(
        self,
        session: Session,
        *,
        subject: str,
        email: str,
        auth_time: float,
        strong_auth_time: float,
        auth_method: str,
    ) -> None:
        session.entra_subject = subject
        session.entra_email = email
        session.entra_auth_time = auth_time
        session.strong_auth_time = strong_auth_time
        session.auth_method = auth_method
        self.touch(session)

    def strong_recent(self, session: Session, max_age_seconds: int) -> bool:
        return bool(session.entra_verified and time.time() - session.strong_auth_time <= max(1, max_age_seconds))


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _cloudflare_jwks(team_domain: str, *, force: bool = False) -> dict:
    domain = team_domain.strip().casefold().removeprefix("https://").strip("/")
    if not domain or not domain.endswith(".cloudflareaccess.com"):
        raise ValueError("domínio de equipe Cloudflare Access inválido")
    now = time.time()
    with _CF_JWKS_LOCK:
        cached = _CF_JWKS_CACHE.get(domain)
        if cached and not force and cached[0] > now:
            return cached[1]
    request = UrlRequest(
        f"https://{domain}/cdn-cgi/access/certs",
        headers={"Accept": "application/json", "User-Agent": "Codex-Linux-Control/0.9.0"},
    )
    with urlopen(request, timeout=5) as response:
        value = json.loads(response.read(1_000_000).decode("utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("keys"), list):
        raise ValueError("JWKS Cloudflare Access inválido")
    with _CF_JWKS_LOCK:
        _CF_JWKS_CACHE[domain] = (now + _CF_JWKS_TTL_SECONDS, value)
    return value


def _verify_cloudflare_access(token: str, settings: Settings) -> str:
    if not settings.cloudflare_access_configured:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cloudflare Access não configurado")
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        header = json.loads(_b64url_decode(encoded_header))
        claims = json.loads(_b64url_decode(encoded_payload))
        if header.get("alg") != "RS256" or not str(header.get("kid", "")):
            raise ValueError("algoritmo ou chave JWT inválidos")
        key = next(
            (item for item in _cloudflare_jwks(settings.cloudflare_access_team_domain).get("keys", []) if item.get("kid") == header["kid"]),
            None,
        )
        if key is None:
            key = next(
                (item for item in _cloudflare_jwks(settings.cloudflare_access_team_domain, force=True).get("keys", []) if item.get("kid") == header["kid"]),
                None,
            )
        if not key or key.get("kty") != "RSA":
            raise ValueError("chave JWT desconhecida")
        public_key = rsa.RSAPublicNumbers(
            int.from_bytes(_b64url_decode(str(key["e"])), "big"),
            int.from_bytes(_b64url_decode(str(key["n"])), "big"),
        ).public_key()
        public_key.verify(
            _b64url_decode(encoded_signature),
            f"{encoded_header}.{encoded_payload}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        now = time.time()
        if float(claims.get("exp", 0)) < now - 30 or float(claims.get("nbf", 0)) > now + 30:
            raise ValueError("token expirado ou ainda não válido")
        domain = settings.cloudflare_access_team_domain.strip().casefold().removeprefix("https://").strip("/")
        if str(claims.get("iss", "")).rstrip("/").casefold() != f"https://{domain}":
            raise ValueError("emissor inválido")
        audience = claims.get("aud", [])
        if isinstance(audience, str):
            audience = [audience]
        if settings.cloudflare_access_audience.strip() not in audience:
            raise ValueError("audiência inválida")
        email = str(claims.get("email", "")).strip().casefold()
        if not email or email not in settings.cloudflare_access_allowed:
            raise ValueError("identidade não autorizada")
        return email
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token Cloudflare Access inválido") from exc


def _cloudflare_cookie_token(headers: Mapping[str, str]) -> str:
    """Return Access' signed browser token when the proxy omits its JWT header.

    Cloudflare Access normally forwards ``Cf-Access-Jwt-Assertion`` to the
    origin. Some tunnel/application combinations expose the same signed JWT
    only through the ``CF_Authorization`` cookie. Both transports retain the
    same issuer, audience, expiration, signature and owner checks below.
    """

    raw = headers.get("cookie", "")
    if not raw or len(raw) > 32_768:
        return ""
    try:
        cookies = SimpleCookie()
        cookies.load(raw)
    except CookieError:
        return ""
    item = cookies.get("CF_Authorization")
    return item.value.strip() if item is not None else ""


def cloudflare_access_token_issued_at(headers: Mapping[str, str], settings: Settings) -> float:
    """Return the issuance time of a fully validated Access application token.

    This is used only after a browser has crossed its persisted reauthentication
    gate. Requiring a token issued after that gate prevents an already-open
    Access session from silently extending a stricter per-browser policy.
    """

    token = headers.get("cf-access-jwt-assertion", "").strip() or _cloudflare_cookie_token(headers)
    if not token:
        return 0.0
    _verify_cloudflare_access(token, settings)
    try:
        _encoded_header, encoded_payload, _encoded_signature = token.split(".")
        claims = json.loads(_b64url_decode(encoded_payload))
        issued_at = float(claims.get("iat") or 0)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token Cloudflare Access sem data de emissão válida",
        ) from exc
    now = time.time()
    if issued_at <= 0 or issued_at > now + 30:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Data de emissão do Cloudflare Access inválida",
        )
    return issued_at


def network_identity(
    headers: Mapping[str, str], client_host: str | None, settings: Settings
) -> str:
    """Authorize Cloudflare Access, an exact Tailscale identity, or loopback."""

    cloudflare_token = headers.get("cf-access-jwt-assertion", "").strip() or _cloudflare_cookie_token(headers)
    cloudflare_email = headers.get("cf-access-authenticated-user-email", "").strip().casefold()
    if cloudflare_token or cloudflare_email:
        # Uvicorn intentionally replaces ``request.client`` with the original
        # browser IP when cloudflared supplies X-Forwarded-For. The security
        # boundary is therefore the loopback-only listener plus the signed JWT,
        # not the rewritten client address.
        if not cloudflare_token or not _is_loopback(settings.host):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cabeçalho Cloudflare fora do proxy local")
        verified_email = _verify_cloudflare_access(cloudflare_token, settings)
        if cloudflare_email and not hmac.compare_digest(cloudflare_email, verified_email):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Identidade Cloudflare inconsistente")
        return f"cloudflare:{verified_email}"

    tailscale_login = headers.get("tailscale-user-login", "").strip()
    if tailscale_login:
        # Tailscale Serve is the only component allowed to assert these headers.
        # The application itself binds to loopback, so a non-loopback peer with
        # this header indicates a proxy bypass or an unsafe future deployment.
        if not _is_loopback(client_host):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cabeçalho de identidade fora do proxy local")
        allowed = settings.tailscale_login_normalized
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acesso Tailscale desativado: configure CLC_ALLOWED_TAILSCALE_LOGIN.",
            )
        if not hmac.compare_digest(tailscale_login.casefold(), allowed):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Identidade não autorizada")
        return tailscale_login

    if is_playwright_automation_request(headers, client_host, settings):
        return PLAYWRIGHT_AUTOMATION_IDENTITY

    if settings.allow_localhost and _is_loopback(client_host):
        return "localhost"

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso permitido somente pelo localhost ou Tailscale Serve")


def require_http_session(
    request: Request,
    settings: Settings,
    sessions: SessionStore,
    require_csrf: bool = False,
) -> Session:
    identity = network_identity(request.headers, request.client.host if request.client else None, settings)
    session = sessions.get(request.cookies.get("clc_session"))
    if not session or not hmac.compare_digest(session.identity, identity):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão inválida ou expirada")
    if require_csrf:
        csrf = request.headers.get(settings.csrf_header_name, "")
        if not csrf or not hmac.compare_digest(csrf, session.csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token CSRF inválido")
    sessions.touch(session)
    return session


def websocket_session(websocket: WebSocket, settings: Settings, sessions: SessionStore) -> Session:
    identity = network_identity(
        websocket.headers,
        websocket.client.host if websocket.client else None,
        settings,
    )
    session = sessions.get(websocket.cookies.get("clc_session"))
    if not session or not hmac.compare_digest(session.identity, identity):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão WebSocket inválida")

    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if origin and host:
        origin_host = urlparse(origin).netloc
        if origin_host and not hmac.compare_digest(origin_host.casefold(), host.casefold()):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origem WebSocket inválida")

    sessions.touch(session)
    return session
