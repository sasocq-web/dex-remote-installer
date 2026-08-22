from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PUSH_HOST_SUFFIXES = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
    "push.apple.com",
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hkdf_extract(salt: bytes, value: bytes) -> bytes:
    return hmac.new(salt, value, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    output = b""
    previous = b""
    counter = 1
    while len(output) < length:
        previous = hmac.new(prk, previous + info + bytes([counter]), hashlib.sha256).digest()
        output += previous
        counter += 1
    return output[:length]


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _valid_endpoint(endpoint: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").rstrip(".").casefold()
    return (
        parsed.scheme == "https"
        and port in {None, 443}
        and bool(parsed.path)
        and any(host == suffix or host.endswith(f".{suffix}") for suffix in PUSH_HOST_SUFFIXES)
    )


class WebPushStore:
    """Portable Web Push subscription store and RFC 8291 sender."""

    def __init__(self, path: Path, subject: str = "https://dex.sasocq.com") -> None:
        self.path = path
        self.subject = subject
        self._lock = asyncio.Lock()
        self._data = self._load()
        if not self._data.get("vapid_private_key"):
            private_key = ec.generate_private_key(ec.SECP256R1())
            scalar = private_key.private_numbers().private_value.to_bytes(32, "big")
            self._data["vapid_private_key"] = _b64url(scalar)
            self._save()

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.setdefault("subscriptions", [])
                return value
        except (OSError, ValueError):
            pass
        return {"version": 1, "subscriptions": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def _private_key(self) -> ec.EllipticCurvePrivateKey:
        scalar = int.from_bytes(_unb64url(str(self._data["vapid_private_key"])), "big")
        return ec.derive_private_key(scalar, ec.SECP256R1())

    def public_key(self) -> str:
        return _b64url(_public_bytes(self._private_key().public_key()))

    async def subscribe(self, value: dict[str, Any], device_id: str = "", name: str = "") -> int:
        endpoint = str(value.get("endpoint") or "").strip()
        keys = value.get("keys") if isinstance(value.get("keys"), dict) else {}
        p256dh = str(keys.get("p256dh") or "")
        auth = str(keys.get("auth") or "")
        if not _valid_endpoint(endpoint):
            raise ValueError("Endpoint de push não permitido")
        try:
            client_key = _unb64url(p256dh)
            auth_secret = _unb64url(auth)
        except (ValueError, TypeError) as exc:
            raise ValueError("Chaves da assinatura de push inválidas") from exc
        if len(client_key) != 65 or client_key[0] != 4 or len(auth_secret) != 16:
            raise ValueError("Chaves da assinatura de push inválidas")
        record = {
            "endpoint": endpoint,
            "keys": {"p256dh": p256dh, "auth": auth},
            "device_id": str(device_id or ""),
            "name": str(name or "")[:100],
            "updated_at": int(time.time()),
        }
        async with self._lock:
            subscriptions = [item for item in self._data["subscriptions"] if item.get("endpoint") != endpoint]
            subscriptions.append(record)
            self._data["subscriptions"] = subscriptions[-50:]
            self._save()
            return len(subscriptions)

    async def unsubscribe(self, endpoint: str) -> int:
        async with self._lock:
            subscriptions = [item for item in self._data["subscriptions"] if item.get("endpoint") != endpoint]
            self._data["subscriptions"] = subscriptions
            self._save()
            return len(subscriptions)

    def status(self, endpoint: str = "") -> dict[str, Any]:
        subscriptions = self._data.get("subscriptions") or []
        return {"supported": True, "subscribed": any(item.get("endpoint") == endpoint for item in subscriptions), "count": len(subscriptions)}

    def _vapid_authorization(self, endpoint: str) -> str:
        parsed = urllib.parse.urlsplit(endpoint)
        audience = f"{parsed.scheme}://{parsed.netloc}"
        header = _b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = _b64url(json.dumps({"aud": audience, "exp": int(time.time()) + 12 * 3600, "sub": self.subject}, separators=(",", ":")).encode())
        unsigned = f"{header}.{claims}".encode("ascii")
        der = self._private_key().sign(unsigned, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={header}.{claims}.{signature}, k={self.public_key()}"

    @staticmethod
    def _encrypt(subscription: dict[str, Any], payload: bytes) -> bytes:
        client_public_bytes = _unb64url(subscription["keys"]["p256dh"])
        auth_secret = _unb64url(subscription["keys"]["auth"])
        client_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_public_bytes)
        server_private = ec.generate_private_key(ec.SECP256R1())
        server_public_bytes = _public_bytes(server_private.public_key())
        shared_secret = server_private.exchange(ec.ECDH(), client_public)
        key_prk = _hkdf_extract(auth_secret, shared_secret)
        ikm = _hkdf_expand(key_prk, b"WebPush: info\x00" + client_public_bytes + server_public_bytes, 32)
        salt = os.urandom(16)
        prk = _hkdf_extract(salt, ikm)
        content_key = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
        ciphertext = AESGCM(content_key).encrypt(nonce, payload + b"\x02", None)
        return salt + struct.pack("!I", 4096) + bytes([len(server_public_bytes)]) + server_public_bytes + ciphertext

    def _send_one(self, subscription: dict[str, Any], payload: bytes) -> int:
        endpoint = str(subscription["endpoint"])
        body = self._encrypt(subscription, payload)
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": self._vapid_authorization(endpoint),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(body)),
                "TTL": "86400",
                "Urgency": "normal",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status)

    async def send(self, value: dict[str, Any]) -> dict[str, int]:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > 3000:
            payload = json.dumps({**value, "body": str(value.get("body") or "")[:800]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        async with self._lock:
            subscriptions = list(self._data.get("subscriptions") or [])
        sent = 0
        expired: set[str] = set()
        for subscription in subscriptions:
            try:
                status = await asyncio.to_thread(self._send_one, subscription, payload)
                if 200 <= status < 300:
                    sent += 1
            except urllib.error.HTTPError as exc:
                if exc.code in {404, 410}:
                    expired.add(str(subscription.get("endpoint") or ""))
            except (OSError, ValueError):
                continue
        if expired:
            async with self._lock:
                self._data["subscriptions"] = [item for item in self._data["subscriptions"] if item.get("endpoint") not in expired]
                self._save()
        return {"sent": sent, "expired": len(expired), "total": len(subscriptions)}
