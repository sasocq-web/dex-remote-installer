from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable


SYSTEMD_CREDS = "/usr/bin/systemd-creds"
CREDENTIAL_NAME = "sasocq-browser-credentials"
STORED_FIELDS = frozenset({"login", "password"})


class BrowserCredentialVaultError(RuntimeError):
    """The encrypted credential vault could not be read or updated."""


def canonical_secure_origin(value: str) -> str:
    """Return an exact HTTPS (or loopback HTTP) origin, or an empty string."""

    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if not host or parsed.username or parsed.password:
        return ""
    host = host.casefold().rstrip(".")
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    authority_host = f"[{host}]" if ":" in host else host
    authority = authority_host if port in {None, default_port} else f"{authority_host}:{port}"
    return f"{parsed.scheme}://{authority}"


class BrowserCredentialVault:
    """TPM/host-bound encrypted storage for browser logins.

    Plaintext exists only in process memory and in the stdin/stdout pipes of
    ``systemd-creds``. The file on disk is always an encrypted credential.
    """

    def __init__(
        self,
        path: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self.path = Path(path)
        self._runner = runner
        self._lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "entries": {}}

    @staticmethod
    def _environment() -> dict[str, str]:
        return {
            "HOME": os.environ.get("HOME", "/home/codex"),
            "PATH": "/usr/bin:/bin",
            "USER": os.environ.get("USER", "codex"),
        }

    def _run(self, argv: list[str], payload: bytes) -> bytes:
        try:
            result = self._runner(
                argv,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=20,
                env=self._environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrowserCredentialVaultError("o cofre criptografado não respondeu") from exc
        if result.returncode:
            raise BrowserCredentialVaultError("o TPM recusou o cofre criptografado")
        return bytes(result.stdout)

    def _decrypt(self, ciphertext: bytes) -> dict[str, Any]:
        plaintext = bytearray(self._run([
            SYSTEMD_CREDS,
            "--user",
            f"--name={CREDENTIAL_NAME}",
            "decrypt",
            "-",
            "-",
        ], ciphertext))
        try:
            value = json.loads(plaintext.decode("utf-8"))
            if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("entries"), dict):
                raise ValueError("invalid vault")
            return value
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise BrowserCredentialVaultError("o cofre criptografado está inválido") from exc
        finally:
            plaintext[:] = b"\0" * len(plaintext)

    def _encrypt(self, value: dict[str, Any]) -> bytes:
        plaintext = bytearray(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        try:
            return self._run([
                SYSTEMD_CREDS,
                "--user",
                "--with-key=host+tpm2",
                f"--name={CREDENTIAL_NAME}",
                "encrypt",
                "-",
                "-",
            ], bytes(plaintext))
        finally:
            plaintext[:] = b"\0" * len(plaintext)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            ciphertext = self.path.read_bytes()
        except OSError as exc:
            raise BrowserCredentialVaultError("o cofre criptografado não pôde ser lido") from exc
        return self._decrypt(ciphertext)

    def lookup(self, origin: str, fields: Iterable[str]) -> dict[str, str] | None:
        canonical = canonical_secure_origin(origin)
        requested = list(dict.fromkeys(str(field) for field in fields))
        if not canonical or not requested or any(field not in STORED_FIELDS for field in requested):
            return None
        with self._lock:
            entry = self._read_unlocked()["entries"].get(canonical)
            if not isinstance(entry, dict):
                return None
            result: dict[str, str] = {}
            for field in requested:
                value = entry.get(field)
                if not isinstance(value, str) or not value:
                    return None
                result[field] = value
            return result

    def store(self, origin: str, values: dict[str, Any]) -> bool:
        canonical = canonical_secure_origin(origin)
        accepted = {
            field: value
            for field, value in values.items()
            if field in STORED_FIELDS and isinstance(value, str) and value
        }
        if not canonical or not accepted:
            return False
        with self._lock:
            value = self._read_unlocked()
            current = value["entries"].get(canonical)
            entry = dict(current) if isinstance(current, dict) else {}
            entry.update(accepted)
            entry["updated_at"] = int(time.time())
            value["entries"][canonical] = entry
            ciphertext = self._encrypt(value)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(ciphertext)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            return True

    def count(self) -> int:
        with self._lock:
            return len(self._read_unlocked()["entries"])
