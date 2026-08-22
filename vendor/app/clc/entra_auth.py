from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes

from .config import Settings
from .security import Session, SessionStore


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _json_urlopen(url: str, *, data: bytes | None = None, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(url, data=data)
    request.add_header('Accept', 'application/json')
    if data is not None:
        request.add_header('Content-Type', 'application/x-www-form-urlencoded')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[-2000:]
        raise RuntimeError(f'Microsoft Entra respondeu HTTP {exc.code}: {detail}') from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'Não foi possível concluir a autenticação Microsoft Entra: {exc}') from exc
    if not isinstance(value, dict):
        raise RuntimeError('Resposta inválida do Microsoft Entra')
    return value


@dataclass
class PendingLogin:
    session_id: str
    verifier: str
    nonce: str
    created_at: float
    redirect_uri: str
    step_up: bool


class EntraAuthManager:
    def __init__(self, settings: Settings, sessions: SessionStore) -> None:
        self.settings = settings
        self.sessions = sessions
        self._pending: dict[str, PendingLogin] = {}
        self._discovery: tuple[float, dict[str, Any]] | None = None
        self._jwks: tuple[float, dict[str, Any]] | None = None

    def configured(self) -> bool:
        return self.settings.entra_configured

    def _openid(self) -> dict[str, Any]:
        now = time.time()
        if self._discovery and self._discovery[0] > now:
            return self._discovery[1]
        value = _json_urlopen(self.settings.entra_authority + '/.well-known/openid-configuration')
        self._discovery = (now + 3600, value)
        return value

    def _redirect_uri(self, origin: str) -> str:
        configured = self.settings.entra_redirect_uri.strip()
        if configured:
            return configured
        return origin.rstrip('/') + '/api/auth/entra/callback'

    def start(self, session: Session, origin: str, *, step_up: bool = False) -> dict[str, Any]:
        if not self.configured():
            raise ValueError('Configure o Client ID do aplicativo Microsoft Entra antes de ativar o acesso remoto')
        verifier = _b64encode(secrets.token_bytes(64))
        challenge = _b64encode(hashlib.sha256(verifier.encode('ascii')).digest())
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        redirect_uri = self._redirect_uri(origin)
        self._pending[state] = PendingLogin(
            session_id=session.session_id,
            verifier=verifier,
            nonce=nonce,
            created_at=time.time(),
            redirect_uri=redirect_uri,
            step_up=step_up,
        )
        discovery = self._openid()
        params: dict[str, str] = {
            'client_id': self.settings.entra_client_id.strip(),
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'response_mode': 'query',
            'scope': 'openid profile email',
            'state': state,
            'nonce': nonce,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        }
        if session.entra_email:
            params['login_hint'] = session.entra_email
        required_acr = self.settings.entra_required_acr.strip()
        if required_acr:
            # Microsoft Entra authentication-context step-up. The ID token must
            # carry the requested ``acrs`` value after Conditional Access has
            # enforced the phishing-resistant authentication strength.
            params['claims'] = json.dumps({
                'id_token': {
                    'acrs': {'essential': True, 'values': [required_acr]},
                }
            }, separators=(',', ':'))
        if step_up:
            params['prompt'] = 'login'
            params['max_age'] = '0'
        url = str(discovery['authorization_endpoint']) + '?' + urllib.parse.urlencode(params)
        self.cleanup()
        return {'url': url, 'state': state, 'step_up': step_up, 'redirect_uri': redirect_uri}

    def cleanup(self) -> None:
        cutoff = time.time() - 600
        for key in [key for key, value in self._pending.items() if value.created_at < cutoff]:
            self._pending.pop(key, None)

    def _keys(self) -> dict[str, Any]:
        now = time.time()
        if self._jwks and self._jwks[0] > now:
            return self._jwks[1]
        value = _json_urlopen(str(self._openid()['jwks_uri']))
        self._jwks = (now + 3600, value)
        return value

    def _verify_jwt(self, token: str, nonce: str) -> dict[str, Any]:
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError('ID token Microsoft inválido')
        try:
            header = json.loads(_b64decode(parts[0]))
            claims = json.loads(_b64decode(parts[1]))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError('ID token Microsoft malformado') from exc
        if header.get('alg') != 'RS256':
            raise ValueError('algoritmo de assinatura Microsoft não permitido')
        kid = str(header.get('kid') or '')
        key = next((item for item in self._keys().get('keys', []) if item.get('kid') == kid), None)
        if not key:
            self._jwks = None
            key = next((item for item in self._keys().get('keys', []) if item.get('kid') == kid), None)
        if not key or key.get('kty') != 'RSA':
            raise ValueError('chave de assinatura Microsoft não encontrada')
        public = rsa.RSAPublicNumbers(
            int.from_bytes(_b64decode(str(key['e'])), 'big'),
            int.from_bytes(_b64decode(str(key['n'])), 'big'),
        ).public_key()
        try:
            public.verify(
                _b64decode(parts[2]),
                (parts[0] + '.' + parts[1]).encode('ascii'),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception as exc:
            raise ValueError('assinatura do ID token Microsoft inválida') from exc
        now = int(time.time())
        if int(claims.get('exp') or 0) < now - 30 or int(claims.get('nbf') or 0) > now + 30:
            raise ValueError('ID token Microsoft expirado ou ainda não válido')
        audience = claims.get('aud')
        if isinstance(audience, list):
            valid_aud = self.settings.entra_client_id in audience
        else:
            valid_aud = str(audience or '') == self.settings.entra_client_id
        if not valid_aud:
            raise ValueError('ID token emitido para outro aplicativo')
        if str(claims.get('nonce') or '') != nonce:
            raise ValueError('nonce da autenticação Microsoft não confere')
        issuer = str(claims.get('iss') or '')
        expected = str(self._openid().get('issuer') or '')
        if '{tenantid}' in expected:
            expected = expected.replace('{tenantid}', str(claims.get('tid') or ''))
        if issuer.rstrip('/') != expected.rstrip('/'):
            raise ValueError('emissor do ID token Microsoft não autorizado')
        return claims

    def _identity_allowed(self, claims: dict[str, Any]) -> bool:
        allowed = self.settings.entra_allowed
        if not allowed:
            # During first local setup the chosen identity is persisted by the UI.
            return True
        candidates = {
            str(claims.get('sub') or '').casefold(),
            str(claims.get('oid') or '').casefold(),
            str(claims.get('preferred_username') or '').casefold(),
            str(claims.get('email') or '').casefold(),
        }
        return bool(allowed & {value for value in candidates if value})

    def _strong_method(self, claims: dict[str, Any]) -> tuple[bool, str]:
        amr = claims.get('amr') or []
        if isinstance(amr, str):
            amr = [amr]
        methods = {str(item).casefold() for item in amr}
        acr = str(claims.get('acr') or '').casefold()
        acrs_raw = claims.get('acrs') or []
        if isinstance(acrs_raw, str):
            acrs_raw = [acrs_raw]
        acrs = {str(item).casefold() for item in acrs_raw}
        required_acr = self.settings.entra_required_acr.strip().casefold()
        if required_acr:
            if acr == required_acr or required_acr in acrs:
                return True, f'acr:{required_acr}'
            # An explicitly configured authentication context is authoritative:
            # generic MFA/push/OTP must never silently satisfy it.
            return False, f'missing-acr:{required_acr}'

        phishing_resistant = {
            'fido', 'fido2', 'passkey', 'webauthn', 'windowshello', 'whfb',
            'certificate', 'x509', 'cba',
        }
        matched = sorted(methods & phishing_resistant)
        if matched:
            return True, '+'.join(matched)
        if self.settings.entra_require_phishing_resistant:
            return False, '+'.join(sorted(methods)) or 'unknown'

        # Compatibility mode may accept ordinary MFA, but it is deliberately
        # opt-in and is not the default for remote root administration.
        ordinary_mfa = {'mfa', 'otp', 'phoneapp', 'ngcmfa', 'rsa'}
        matched = sorted(methods & ordinary_mfa)
        if matched:
            return True, '+'.join(matched)
        if not self.settings.entra_require_mfa:
            return True, '+'.join(sorted(methods)) or 'oidc'
        return False, '+'.join(sorted(methods)) or 'unknown'

    def callback(self, *, code: str, state: str, error: str = '', error_description: str = '') -> tuple[Session, dict[str, Any]]:
        self.cleanup()
        pending = self._pending.pop(state, None)
        if error:
            raise ValueError(error_description or error)
        if not pending or time.time() - pending.created_at > 600:
            raise ValueError('solicitação Microsoft expirada ou já utilizada')
        session = self.sessions.get(pending.session_id)
        if not session:
            raise ValueError('sessão do dispositivo expirou; entre novamente')
        discovery = self._openid()
        body = urllib.parse.urlencode({
            'client_id': self.settings.entra_client_id.strip(),
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': pending.redirect_uri,
            'code_verifier': pending.verifier,
            'scope': 'openid profile email',
        }).encode('utf-8')
        tokens = _json_urlopen(str(discovery['token_endpoint']), data=body)
        id_token = str(tokens.get('id_token') or '')
        if not id_token:
            raise ValueError('Microsoft Entra não retornou ID token')
        claims = self._verify_jwt(id_token, pending.nonce)
        if not self.settings.entra_allowed and (session.identity != 'localhost' or self.settings.setup_completed):
            raise ValueError('a identidade administrativa Microsoft precisa ser cadastrada localmente antes do acesso remoto')
        if not self._identity_allowed(claims):
            raise ValueError('esta identidade Microsoft não está autorizada para o mini PC')
        strong, method = self._strong_method(claims)
        if not strong:
            raise ValueError(
                'A política Microsoft Entra não comprovou passkey/FIDO ou o Authentication Context exigido. Configure Authentication Strength resistente a phishing para este aplicativo.'
            )
        subject = str(claims.get('oid') or claims.get('sub') or '')
        email = str(claims.get('preferred_username') or claims.get('email') or '')
        now = time.time()
        auth_time = float(claims.get('auth_time') or now)
        # A step-up request uses prompt=login,max_age=0 and the requested ACR.
        # Record the actual token authentication time, bounded to the present,
        # instead of granting a fresh window to an old token.
        strong_auth_time = min(now, max(0.0, auth_time))
        self.sessions.mark_entra(
            session,
            subject=subject,
            email=email,
            auth_time=auth_time,
            strong_auth_time=strong_auth_time,
            auth_method=method,
        )
        return session, {
            'subject': subject,
            'email': email,
            'tenant': claims.get('tid'),
            'method': method,
            'step_up': pending.step_up,
            'strong': True,
        }
