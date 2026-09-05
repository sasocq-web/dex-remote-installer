from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


PAIRING_TTL_SECONDS = 10 * 60
CHALLENGE_TTL_SECONDS = 90
MAX_DEVICES = 6
ENROLLMENT_REQUEST_TTL_SECONDS = 24 * 60 * 60
DAILY_REAUTHENTICATION_SECONDS = 24 * 60 * 60
WEEKLY_REAUTHENTICATION_SECONDS = 7 * DAILY_REAUTHENTICATION_SECONDS
MONTHLY_REAUTHENTICATION_SECONDS = 30 * DAILY_REAUTHENTICATION_SECONDS
DEFAULT_REAUTHENTICATION_SECONDS = MONTHLY_REAUTHENTICATION_SECONDS
REAUTHENTICATION_INTERVALS = {
    DAILY_REAUTHENTICATION_SECONDS,
    WEEKLY_REAUTHENTICATION_SECONDS,
    MONTHLY_REAUTHENTICATION_SECONDS,
}


def _b64url_decode(value: str) -> bytes:
    text = str(value or "").strip()
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode((text + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Valor base64url inválido") from exc


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _safe_text(value: Any, *, maximum: int, fallback: str = "") -> str:
    text = " ".join(str(value or "").replace("\x00", "").split()).strip()
    return (text[:maximum] or fallback).strip()


def _public_key_from_jwk(jwk: Dict[str, Any]) -> ec.EllipticCurvePublicKey:
    if not isinstance(jwk, dict):
        raise ValueError("Chave pública inválida")
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("Somente chaves ECDSA P-256 são aceitas")
    x_raw = _b64url_decode(str(jwk.get("x") or ""))
    y_raw = _b64url_decode(str(jwk.get("y") or ""))
    if len(x_raw) != 32 or len(y_raw) != 32:
        raise ValueError("Coordenadas da chave pública inválidas")
    numbers = ec.EllipticCurvePublicNumbers(
        int.from_bytes(x_raw, "big"),
        int.from_bytes(y_raw, "big"),
        ec.SECP256R1(),
    )
    try:
        return numbers.public_key()
    except ValueError as exc:
        raise ValueError("A chave pública não pertence à curva P-256") from exc


def _normalized_public_jwk(jwk: Dict[str, Any]) -> Dict[str, str]:
    _public_key_from_jwk(jwk)
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": str(jwk["x"]),
        "y": str(jwk["y"]),
        "ext": True,
        "key_ops": ["verify"],
    }


def _signature_to_der(signature: bytes) -> bytes:
    # WebCrypto returns IEEE P1363 (r || s) for ECDSA in current browsers.
    # Accept DER too so diagnostic clients can use common crypto libraries.
    if len(signature) == 64:
        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        return encode_dss_signature(r, s)
    if 8 <= len(signature) <= 80 and signature[:1] == b"0":
        return signature
    raise ValueError("Assinatura ECDSA inválida")


@dataclass
class DeviceRecord:
    id: str
    identity: str
    name: str
    public_jwk: Dict[str, Any]
    created_at: float
    last_seen_at: float
    last_user_agent: str = ""
    last_ip: str = ""
    revoked: bool = False
    access_history: list[Dict[str, Any]] = field(default_factory=list)
    reauthentication_interval_seconds: int = DEFAULT_REAUTHENTICATION_SECONDS
    last_reauthenticated_at: float = 0.0
    reauthentication_required_at: float = 0.0

    def public_dict(self) -> Dict[str, Any]:
        due_at = self.last_reauthenticated_at + self.reauthentication_interval_seconds
        required = bool(self.reauthentication_required_at or time.time() >= due_at)
        return {
            "id": self.id,
            "identity": self.identity,
            "name": self.name,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "last_user_agent": self.last_user_agent,
            "revoked": self.revoked,
            "fingerprint": device_fingerprint(self.public_jwk),
            "access_history": list(reversed(self.access_history[-50:])),
            "reauthentication_interval_seconds": self.reauthentication_interval_seconds,
            "last_reauthenticated_at": self.last_reauthenticated_at,
            "reauthentication_due_at": due_at,
            "reauthentication_required": required,
        }


@dataclass
class PairingTicket:
    id: str
    token_digest: str
    identity: str
    external_url: str
    expires_at: float
    used: bool = False


@dataclass
class DeviceChallenge:
    id: str
    device_id: str
    identity: str
    payload: str
    expires_at: float
    used: bool = False


@dataclass
class EnrollmentRequest:
    id: str
    identity: str
    name: str
    public_jwk: Dict[str, Any]
    created_at: float
    expires_at: float
    user_agent: str = ""
    client_ip: str = ""
    status: str = "pending"
    device_id: str = ""

    def public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "identity": self.identity,
            "email": self.identity.split(":", 1)[-1].casefold(),
            "name": self.name,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "device_id": self.device_id,
            "client_ip": self.client_ip,
            "user_agent": self.user_agent,
            "fingerprint": device_fingerprint(self.public_jwk),
        }


def device_fingerprint(jwk: Dict[str, Any]) -> str:
    canonical = json.dumps(
        {"crv": jwk.get("crv"), "kty": jwk.get("kty"), "x": jwk.get("x"), "y": jwk.get("y")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest().upper()
    return ":".join(digest[index : index + 4] for index in range(0, 24, 4))


class DeviceAuthStore:
    """Device-bound public-key authentication for remote browsers.

    Tailscale proves the account identity and transport. This store adds a second,
    local authorization factor: only a browser paired from the Linux computer can
    create an application session. Private keys never reach the server.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._devices: Dict[str, DeviceRecord] = {}
        self._pairings: Dict[str, PairingTicket] = {}
        self._challenges: Dict[str, DeviceChallenge] = {}
        self._enrollment_requests: Dict[str, EnrollmentRequest] = {}
        self._lock = RLock()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        devices = raw.get("devices", []) if isinstance(raw, dict) else []
        if not isinstance(devices, list):
            return
        for item in devices:
            if not isinstance(item, dict):
                continue
            try:
                normalized = _normalized_public_jwk(dict(item.get("public_jwk") or {}))
                record = DeviceRecord(
                    id=str(item["id"]),
                    identity=str(item["identity"]),
                    name=_safe_text(item.get("name"), maximum=100, fallback="Dispositivo"),
                    public_jwk=normalized,
                    created_at=float(item.get("created_at") or time.time()),
                    last_seen_at=float(item.get("last_seen_at") or 0),
                    last_user_agent=_safe_text(item.get("last_user_agent"), maximum=300),
                    last_ip=_safe_text(item.get("last_ip"), maximum=100),
                    revoked=bool(item.get("revoked", False)),
                    access_history=list(item.get("access_history") or [])[-100:],
                    reauthentication_interval_seconds=self._normalized_reauthentication_interval(
                        item.get("reauthentication_interval_seconds", DEFAULT_REAUTHENTICATION_SECONDS)
                    ),
                    last_reauthenticated_at=float(
                        item.get("last_reauthenticated_at")
                        or item.get("last_seen_at")
                        or item.get("created_at")
                        or time.time()
                    ),
                    reauthentication_required_at=float(item.get("reauthentication_required_at") or 0),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._devices[record.id] = record
        requests = raw.get("enrollment_requests", []) if isinstance(raw, dict) else []
        for item in requests if isinstance(requests, list) else []:
            try:
                pending = EnrollmentRequest(
                    id=str(item["id"]),
                    identity=str(item["identity"]),
                    name=_safe_text(item.get("name"), maximum=100, fallback="Dispositivo remoto"),
                    public_jwk=_normalized_public_jwk(dict(item.get("public_jwk") or {})),
                    created_at=float(item.get("created_at") or time.time()),
                    expires_at=float(item.get("expires_at") or 0),
                    user_agent=_safe_text(item.get("user_agent"), maximum=300),
                    client_ip=_safe_text(item.get("client_ip"), maximum=100),
                    status=str(item.get("status") or "pending"),
                    device_id=str(item.get("device_id") or ""),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._enrollment_requests[pending.id] = pending

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 3,
            "devices": [asdict(item) for item in self._devices.values()],
            "enrollment_requests": [asdict(item) for item in self._enrollment_requests.values()],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)

    def cleanup(self) -> None:
        now = time.time()
        self._pairings = {
            key: value for key, value in self._pairings.items() if not value.used and value.expires_at > now
        }
        self._challenges = {
            key: value for key, value in self._challenges.items() if not value.used and value.expires_at > now
        }
        for request in self._enrollment_requests.values():
            if request.status == "pending" and request.expires_at <= now:
                request.status = "expired"

    def list_devices(self, identity: Optional[str] = None, *, include_revoked: bool = False) -> list[Dict[str, Any]]:
        with self._lock:
            result: list[DeviceRecord] = []
            normalized = str(identity or "").casefold()
            for record in self._devices.values():
                if normalized and record.identity.casefold() != normalized:
                    continue
                if record.revoked and not include_revoked:
                    continue
                result.append(record)
            result.sort(key=lambda item: item.last_seen_at or item.created_at, reverse=True)
            return [item.public_dict() for item in result]

    def active_count(self, identity: Optional[str] = None) -> int:
        return len(self.list_devices(identity))

    @staticmethod
    def _normalized_reauthentication_interval(value: Any) -> int:
        try:
            interval = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Prazo de reautenticação inválido") from exc
        if interval not in REAUTHENTICATION_INTERVALS:
            raise ValueError("Escolha reautenticação diária, semanal ou a cada 30 dias")
        return interval

    @staticmethod
    def _record_event(
        record: DeviceRecord,
        event: str,
        *,
        user_agent: str = "",
        client_ip: str = "",
    ) -> None:
        email = record.identity.split(":", 1)[-1].casefold()
        record.access_history.append(
            {
                "event": _safe_text(event, maximum=40),
                "at": time.time(),
                "email": _safe_text(email, maximum=320),
                "ip": _safe_text(client_ip, maximum=100),
                "user_agent": _safe_text(user_agent, maximum=300),
            }
        )
        record.access_history = record.access_history[-100:]

    def create_pairing(self, identity: str, external_url: str, ttl_seconds: int = PAIRING_TTL_SECONDS) -> Dict[str, Any]:
        identity = _safe_text(identity, maximum=320)
        external_url = str(external_url or "").strip().rstrip("/")
        if not identity:
            raise ValueError("A identidade Tailscale ainda não foi configurada")
        if not external_url.startswith("https://"):
            raise ValueError("Ative primeiro o endereço HTTPS privado do Tailscale")
        with self._lock:
            self.cleanup()
            token = secrets.token_urlsafe(32)
            ticket = PairingTicket(
                id=secrets.token_urlsafe(18),
                token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                identity=identity,
                external_url=external_url,
                expires_at=time.time() + max(120, min(int(ttl_seconds), 1800)),
            )
            self._pairings[ticket.id] = ticket
            return {
                "ticket_id": ticket.id,
                "token": token,
                "identity": ticket.identity,
                "expires_at": ticket.expires_at,
                # Fragment identifiers are not sent in HTTP requests or proxy logs.
                "pairing_url": f"{ticket.external_url}/#pair={token}",
            }

    def _take_pairing(self, token: str, identity: str, origin: str = "") -> PairingTicket:
        token_digest = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        with self._lock:
            self.cleanup()
            for ticket in self._pairings.values():
                if ticket.used:
                    continue
                if not hmac.compare_digest(ticket.token_digest, token_digest):
                    continue
                if ticket.identity.casefold() != str(identity or "").casefold():
                    raise ValueError("A identidade do pareamento não corresponde à identidade autorizada")
                normalized_origin = str(origin or "").strip().rstrip("/").casefold()
                if normalized_origin and not hmac.compare_digest(ticket.external_url.casefold(), normalized_origin):
                    raise ValueError("O pareamento precisa ser concluído no endereço HTTPS privado exibido no QR Code")
                ticket.used = True
                return ticket
        raise ValueError("O código de pareamento é inválido, expirou ou já foi utilizado")

    def register(
        self,
        *,
        token: str,
        identity: str,
        public_jwk: Dict[str, Any],
        name: str,
        user_agent: str = "",
        client_ip: str = "",
        origin: str = "",
    ) -> DeviceRecord:
        normalized_jwk = _normalized_public_jwk(public_jwk)
        self._take_pairing(token, identity, origin)
        with self._lock:
            active = [item for item in self._devices.values() if not item.revoked]
            if len(active) >= MAX_DEVICES:
                raise ValueError("O limite de dispositivos pareados foi alcançado; revogue um dispositivo antigo")
            now = time.time()
            record = DeviceRecord(
                id=secrets.token_urlsafe(18),
                identity=_safe_text(identity, maximum=320),
                name=_safe_text(name, maximum=100, fallback="Dispositivo remoto"),
                public_jwk=normalized_jwk,
                created_at=now,
                last_seen_at=now,
                last_user_agent=_safe_text(user_agent, maximum=300),
                last_ip=_safe_text(client_ip, maximum=100),
                last_reauthenticated_at=now,
            )
            self._devices[record.id] = record
            self._record_event(record, "registered", user_agent=user_agent, client_ip=client_ip)
            self._save()
            return record

    def register_verified_identity(
        self,
        *,
        identity: str,
        public_jwk: Dict[str, Any],
        name: str,
        user_agent: str = "",
        client_ip: str = "",
    ) -> DeviceRecord:
        """Enroll after the reverse proxy cryptographically verified the owner.

        The caller must verify a signed Cloudflare Access JWT before invoking
        this method. The browser still creates and retains its own P-256 private
        key; only the public key is stored here.
        """
        normalized_jwk = _normalized_public_jwk(public_jwk)
        normalized_identity = _safe_text(identity, maximum=320)
        if not normalized_identity:
            raise ValueError("A identidade verificada não foi informada")
        with self._lock:
            active = [item for item in self._devices.values() if not item.revoked]
            if len(active) >= MAX_DEVICES:
                raise ValueError(f"O limite de {MAX_DEVICES} dispositivos cadastrados foi alcançado; revogue um dispositivo antigo")
            now = time.time()
            record = DeviceRecord(
                id=secrets.token_urlsafe(18),
                identity=normalized_identity,
                name=_safe_text(name, maximum=100, fallback="Navegador remoto"),
                public_jwk=normalized_jwk,
                created_at=now,
                last_seen_at=now,
                last_user_agent=_safe_text(user_agent, maximum=300),
                last_ip=_safe_text(client_ip, maximum=100),
                last_reauthenticated_at=now,
            )
            self._devices[record.id] = record
            self._record_event(record, "registered", user_agent=user_agent, client_ip=client_ip)
            self._save()
            return record

    def request_verified_identity(
        self,
        *,
        identity: str,
        public_jwk: Dict[str, Any],
        name: str,
        user_agent: str = "",
        client_ip: str = "",
    ) -> EnrollmentRequest:
        normalized_jwk = _normalized_public_jwk(public_jwk)
        normalized_identity = _safe_text(identity, maximum=320)
        if not normalized_identity:
            raise ValueError("A identidade verificada não foi informada")
        fingerprint = device_fingerprint(normalized_jwk)
        with self._lock:
            self.cleanup()
            if self.active_count() >= MAX_DEVICES:
                raise ValueError(f"O limite de {MAX_DEVICES} dispositivos cadastrados foi alcançado")
            for item in self._enrollment_requests.values():
                if (
                    item.status == "pending"
                    and item.identity.casefold() == normalized_identity.casefold()
                    and device_fingerprint(item.public_jwk) == fingerprint
                ):
                    return item
            now = time.time()
            pending = EnrollmentRequest(
                id=secrets.token_urlsafe(18),
                identity=normalized_identity,
                name=_safe_text(name, maximum=100, fallback="Dispositivo remoto"),
                public_jwk=normalized_jwk,
                created_at=now,
                expires_at=now + ENROLLMENT_REQUEST_TTL_SECONDS,
                user_agent=_safe_text(user_agent, maximum=300),
                client_ip=_safe_text(client_ip, maximum=100),
            )
            self._enrollment_requests[pending.id] = pending
            self._save()
            return pending

    def enrollment_request(self, request_id: str, identity: str = "") -> Optional[EnrollmentRequest]:
        with self._lock:
            self.cleanup()
            pending = self._enrollment_requests.get(str(request_id or ""))
            if not pending:
                return None
            if identity and pending.identity.casefold() != str(identity).casefold():
                return None
            return pending

    def list_enrollment_requests(self, *, include_finished: bool = False) -> list[Dict[str, Any]]:
        with self._lock:
            self.cleanup()
            values = [
                item for item in self._enrollment_requests.values()
                if include_finished or item.status == "pending"
            ]
            values.sort(key=lambda item: item.created_at, reverse=True)
            return [item.public_dict() for item in values]

    def approve_enrollment_request(self, request_id: str) -> DeviceRecord:
        with self._lock:
            pending = self.enrollment_request(request_id)
            if not pending or pending.status != "pending":
                raise ValueError("Solicitação inexistente, expirada ou já processada")
            record = self.register_verified_identity(
                identity=pending.identity,
                public_jwk=pending.public_jwk,
                name=pending.name,
                user_agent=pending.user_agent,
                client_ip=pending.client_ip,
            )
            pending.status = "approved"
            pending.device_id = record.id
            self._save()
            return record

    def reject_enrollment_request(self, request_id: str) -> EnrollmentRequest:
        with self._lock:
            pending = self.enrollment_request(request_id)
            if not pending or pending.status != "pending":
                raise ValueError("Solicitação inexistente, expirada ou já processada")
            pending.status = "rejected"
            self._save()
            return pending

    def get(self, device_id: str) -> Optional[DeviceRecord]:
        with self._lock:
            record = self._devices.get(str(device_id or ""))
            if not record or record.revoked:
                return None
            return record

    def set_reauthentication_interval(self, device_id: str, interval_seconds: int) -> DeviceRecord:
        interval = self._normalized_reauthentication_interval(interval_seconds)
        with self._lock:
            record = self.get(device_id)
            if not record:
                raise ValueError("Dispositivo não encontrado ou revogado")
            record.reauthentication_interval_seconds = interval
            self._record_event(record, "reauthentication_policy")
            self._save()
            return record

    def reauthentication_required(self, device_id: str, *, mark: bool = False) -> bool:
        """Return whether this browser must establish a fresh identity session.

        When the gate is first observed, persist an integer-second marker. A
        Cloudflare token issued before (or during) that request cannot satisfy
        the gate; the browser must pass through Access again afterwards.
        """
        with self._lock:
            record = self.get(device_id)
            if not record:
                return True
            now = time.time()
            required = bool(
                record.reauthentication_required_at
                or now >= record.last_reauthenticated_at + record.reauthentication_interval_seconds
            )
            if required and mark and not record.reauthentication_required_at:
                record.reauthentication_required_at = float(int(now))
                self._record_event(record, "reauthentication_required")
                self._save()
            return required

    def complete_reauthentication(self, device_id: str, token_issued_at: float = 0.0) -> bool:
        """Accept a new Cloudflare Access token after the per-browser gate.

        Tailscale-only installations do not have an Access token; for those,
        the already verified device-key challenge is the available identity
        boundary and its completion renews the device policy.
        """
        with self._lock:
            record = self.get(device_id)
            if not record:
                raise ValueError("Dispositivo não encontrado ou revogado")
            now = time.time()
            due = bool(
                record.reauthentication_required_at
                or now >= record.last_reauthenticated_at + record.reauthentication_interval_seconds
            )
            if not due:
                return True
            if not record.reauthentication_required_at:
                record.reauthentication_required_at = float(int(now))
                self._record_event(record, "reauthentication_required")
                self._save()
                return False
            issued_at = float(token_issued_at or 0)
            if record.identity.casefold().startswith("cloudflare:"):
                if issued_at <= record.reauthentication_required_at:
                    return False
                completed_at = max(issued_at, now)
            else:
                completed_at = now
            record.last_reauthenticated_at = completed_at
            record.reauthentication_required_at = 0.0
            self._record_event(record, "reauthenticated")
            self._save()
            return True

    def create_challenge(self, *, device_id: str, identity: str, origin: str) -> DeviceChallenge:
        with self._lock:
            self.cleanup()
            record = self.get(device_id)
            if not record or record.identity.casefold() != str(identity or "").casefold():
                raise ValueError("Dispositivo não reconhecido ou revogado")
            challenge_id = secrets.token_urlsafe(18)
            nonce = secrets.token_urlsafe(32)
            safe_origin = _safe_text(origin, maximum=500)
            payload = "\n".join(
                [
                    "CODEX-LINUX-CONTROL-DEVICE-AUTH/1",
                    challenge_id,
                    record.id,
                    nonce,
                    safe_origin,
                ]
            )
            challenge = DeviceChallenge(
                id=challenge_id,
                device_id=record.id,
                identity=record.identity,
                payload=payload,
                expires_at=time.time() + CHALLENGE_TTL_SECONDS,
            )
            self._challenges[challenge.id] = challenge
            return challenge

    def verify(
        self,
        *,
        challenge_id: str,
        device_id: str,
        identity: str,
        signature: str,
        user_agent: str = "",
        client_ip: str = "",
    ) -> DeviceRecord:
        with self._lock:
            self.cleanup()
            challenge = self._challenges.get(str(challenge_id or ""))
            if not challenge or challenge.used:
                raise ValueError("Desafio de autenticação inválido ou expirado")
            if challenge.device_id != str(device_id or ""):
                raise ValueError("O desafio não pertence a este dispositivo")
            if challenge.identity.casefold() != str(identity or "").casefold():
                raise ValueError("A identidade do desafio não corresponde à conexão")
            record = self.get(device_id)
            if not record:
                raise ValueError("Dispositivo não reconhecido ou revogado")
            challenge.used = True
            payload = challenge.payload.encode("utf-8")
            public_key = _public_key_from_jwk(record.public_jwk)
            signature_der = _signature_to_der(_b64url_decode(signature))
            try:
                public_key.verify(signature_der, payload, ec.ECDSA(hashes.SHA256()))
            except InvalidSignature as exc:
                raise ValueError("A assinatura do dispositivo não é válida") from exc
            record.last_seen_at = time.time()
            record.last_user_agent = _safe_text(user_agent, maximum=300)
            record.last_ip = _safe_text(client_ip, maximum=100)
            self._record_event(record, "authenticated", user_agent=user_agent, client_ip=client_ip)
            self._save()
            return record

    def revoke(self, device_id: str) -> DeviceRecord:
        with self._lock:
            record = self._devices.get(str(device_id or ""))
            if not record:
                raise ValueError("Dispositivo não encontrado")
            record.revoked = True
            self._record_event(record, "revoked")
            self._save()
            return record


def pairing_qr_png(value: str) -> bytes:
    try:
        import qrcode
    except ImportError as exc:  # pragma: no cover - package dependency on supported systems
        raise RuntimeError("O componente gráfico de QR Code não está instalado") from exc
    image = qrcode.make(value)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


__all__ = [
    "CHALLENGE_TTL_SECONDS",
    "DAILY_REAUTHENTICATION_SECONDS",
    "DEFAULT_REAUTHENTICATION_SECONDS",
    "DeviceAuthStore",
    "DeviceChallenge",
    "DeviceRecord",
    "PAIRING_TTL_SECONDS",
    "REAUTHENTICATION_INTERVALS",
    "WEEKLY_REAUTHENTICATION_SECONDS",
    "MONTHLY_REAUTHENTICATION_SECONDS",
    "device_fingerprint",
    "pairing_qr_png",
]
