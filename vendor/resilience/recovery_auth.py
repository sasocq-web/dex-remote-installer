"""Same Dex identity/device policy; offline proof only from a validated session."""
import base64
import hashlib
import hmac
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import sys
import time

sys.path.insert(0,str(Path('/opt/codex-linux-control/current').resolve()))
from clc.config import load_settings
from clc.device_auth import DeviceAuthStore
from clc.security import network_identity,PLAYWRIGHT_AUTOMATION_IDENTITY

BASE=Path('/home/codex/.local/share/dex-rescue')
COOKIE='__Host-dex_rescue_access'
class AccessDenied(Exception):
    def __init__(self,status=401): self.status=status

def settings():return load_settings(Path('/home/codex/.config/codex-linux-control/config.json'))
def identity(headers,config):return network_identity(headers,'127.0.0.1',config)
def record(device,config):
    store=DeviceAuthStore(config.resolved_devices_file)
    value=store.get(device)
    if not value or store.reauthentication_required(device,mark=False):raise AccessDenied(403)
    return value
def key():
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=BASE/'access.key'
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:pass
    else:
        try:os.write(fd,secrets.token_bytes(32));os.fsync(fd)
        finally:os.close(fd)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size!=32:raise AccessDenied(403)
        return os.read(fd,32)
    finally:os.close(fd)
def encode(value):return base64.urlsafe_b64encode(value).decode().rstrip('=')
def decode(value):return base64.urlsafe_b64decode(value+'='*(-len(value)%4))
def sign(data):
    body=encode(json.dumps(data,sort_keys=True,separators=(',',':')).encode())
    return body+'.'+encode(hmac.digest(key(),body.encode(),'sha256'))
def unpack(token):
    if len(token)>8192:raise AccessDenied()
    try:
        body,signature=token.split('.')
        if not hmac.compare_digest(decode(signature),hmac.digest(key(),body.encode(),'sha256')):raise AccessDenied()
        value=json.loads(decode(body))
        if value.get('exp',0)<=time.time():raise AccessDenied()
        return value
    except (ValueError,TypeError,KeyError):raise AccessDenied() from None
def probe(headers):
    conn=http.client.HTTPConnection('127.0.0.1',8788,timeout=3)
    try:
        forwarded={k:v for k,v in headers.items() if k in {'host','cookie','cf-access-jwt-assertion','cf-access-authenticated-user-email','cf-connecting-ip','tailscale-user-login','forwarded','x-forwarded-for','x-forwarded-host','x-forwarded-proto','user-agent'}}
        forwarded['Connection']='close'
        conn.request('GET','/api/session',headers=forwarded)
        response=conn.getresponse();data=response.read(262144)
        if response.status>=500:return None
        if response.status!=200:raise AccessDenied(403 if response.status==403 else 401)
        return json.loads(data)
    except (OSError,http.client.HTTPException):return None
    except ValueError:raise AccessDenied() from None
    finally:conn.close()
def fingerprint(value):return hashlib.sha256(json.dumps(value.public_jwk,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def authorize(headers):
    try:
        config=settings()
        who=identity(headers,config)
        if who in ('localhost',PLAYWRIGHT_AUTOMATION_IDENTITY):raise AccessDenied(403)
        cookies=SimpleCookie();cookies.load(headers.get('cookie',''))
        session=cookies.get('clc_session')
        if not session or not session.value:raise AccessDenied()
        session_hash=hashlib.sha256(session.value.encode()).hexdigest()
        live=probe(headers)
        if live is not None:
            if live.get('identity')!=who or live.get('read_only_automation') or not live.get('device_id'):raise AccessDenied(403)
            device=record(live['device_id'],config)
            entra=live.get('entra',{}).get('verified') is True
            if config.entra_configured and not entra:raise AccessDenied()
            expires=min(time.time()+config.session_ttl_seconds,device.last_reauthenticated_at+device.reauthentication_interval_seconds)
            proof={'v':1,'identity':who,'device':device.id,'fingerprint':fingerprint(device),'session':session_hash,'entra':entra,'exp':expires}
            if device.identity.casefold()!=who.casefold() or expires<=time.time():raise AccessDenied(403)
            token=sign(proof)
            # No broader duration than the Dex session or device authorization.
            return f'Set-Cookie: {COOKIE}={token}; Path=/; Max-Age={max(0,int(expires-time.time()))}; Secure; HttpOnly; SameSite=Strict\r\n'
        item=cookies.get(COOKIE)
        if not item:raise AccessDenied()
        proof=unpack(item.value)
        if proof.get('v')!=1 or proof.get('identity')!=who or proof.get('session')!=session_hash:raise AccessDenied(403)
        device=record(proof.get('device',''),config)
        if device.identity.casefold()!=who.casefold() or proof.get('fingerprint')!=fingerprint(device):raise AccessDenied(403)
        if config.entra_configured and proof.get('entra') is not True:raise AccessDenied()
        return ''
    except AccessDenied:raise
    except Exception:raise AccessDenied(403) from None
