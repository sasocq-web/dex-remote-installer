#!/usr/bin/python3
"""Independent loopback front door and authenticated-edge Dex repair entry."""
import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
from recovery_auth import authorize, AccessDenied

BASE = Path('/home/codex/.local/share/dex-rescue')
PREFIX = '/__dex_recovery'
ORIGIN = 'https://dex.sasocq.com'
TOKEN = secrets.token_urlsafe(32)
LOG = logging.getLogger(__name__)
AUTH_PAGE = '''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Acesso à restauração do Dex</title><main><h1>Confirme seu acesso ao Dex</h1><p>Abra o Dex neste dispositivo para renovar sua sessão. Depois, volte à restauração.</p><a href="/">Abrir o Dex</a></main></html>'''
WATCH = '''(() => { let failed=0,busy=false; setInterval(async()=>{
if(busy||location.pathname.startsWith('/__dex_recovery'))return;busy=true;
try{const r=await fetch('/__dex_recovery/status',{cache:'no-store',signal:AbortSignal.timeout(7000)});
if(r.ok){const s=await r.json();failed=s.application_ready?0:failed+1;if(failed>=3)location.replace('/__dex_recovery/');}}
catch(_){}finally{busy=false;}},10000);})();'''
HTML = '''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Restaurar Dex</title><style>
body{margin:0;background:#101827;color:#edf3ff;font:17px system-ui;min-height:100dvh;display:grid;place-items:center}main{box-sizing:border-box;width:min(92%,620px);padding:36px 28px;background:#1b2940;border:1px solid #3a4b66;border-radius:24px}h1{font-size:30px;margin:10px 0 16px}p{line-height:1.6;color:#cbd8ec}button,a{font:inherit}button{width:100%;padding:16px;border:0;border-radius:12px;background:#96c5ff;color:#10213b;font-weight:700;cursor:pointer}button:disabled{opacity:.55}a{color:#b8d6ff}#status{min-height:58px;white-space:pre-wrap}small{color:#aabbd2}</style><main><small>DEX · RECUPERAÇÃO</small><h1>Vamos restaurar o Dex</h1><p>Este canal funciona separado do Dex. O botão envia um pedido autorizado diretamente ao Codex CLI do Linux para diagnosticar, corrigir e verificar o serviço.</p><button id="restore">Restaurar Dex</button><p id="status" role="status" aria-live="polite">Consultando o serviço…</p><a href="/">Voltar ao Dex</a><p><small>Suas conversas e arquivos serão preservados. A recuperação pode levar alguns minutos.</small></p></main><script>
const btn=document.querySelector('#restore'),out=document.querySelector('#status');
let submitting=false;
function renew(){btn.disabled=true;out.textContent='Abra o Dex para renovar sua sessão ou recarregue esta página.';}
async function check(){
  if(submitting)return;
  try{
    const r=await fetch('/__dex_recovery/status',{cache:'no-store',signal:AbortSignal.timeout(12000)});
    if(submitting)return;
    if(r.redirected||r.status===401||r.status===403){renew();return;}
    if(!r.ok)throw Error();
    const s=await r.json();
    btn.disabled=s.running||s.ready;
    const messages=[];
    if(s.running)messages.push('O Codex CLI está trabalhando na recuperação.');
    else if(s.last_result==='failed'&&!s.ready)messages.push('A última tentativa terminou sem restaurar todos os serviços.');
    if(s.application_ready)messages.push('O Dex está aceitando mensagens. Use “Voltar ao Dex”.');
    if(s.reason)messages.push(s.reason);
    if(!s.ready&&!s.running)messages.push('Use “Restaurar Dex” para enviar um novo pedido.');
    out.textContent=messages.join(' ');
  }catch(e){if(!submitting)out.textContent='Reconectando ao canal de recuperação…';}
}
btn.onclick=async()=>{
  submitting=true;btn.disabled=true;out.textContent='Enviando pedido ao Codex CLI…';
  try{
    const r=await fetch('/__dex_recovery/restore',{method:'POST',headers:{'X-Dex-Recovery':'__TOKEN__'},signal:AbortSignal.timeout(12000)});
    if(r.redirected||r.status===401||r.status===403){renew();return;}
    if(r.status!==202){out.textContent='O executor de recuperação está indisponível. Tente novamente.';btn.disabled=false;return;}
    out.textContent='Pedido aceito. Verificando o início da recuperação…';
  }catch(e){out.textContent='A confirmação do pedido não chegou. Aguarde a próxima consulta.';}
  finally{submitting=false;}
};check();setInterval(check,5000);
</script></html>'''

async def health(port=8788, require_accepting=True):
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 2)
        writer.write(b'GET /api/health HTTP/1.0\r\nHost: localhost\r\n\r\n')
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(262144), 3)
        head, body = raw.split(b'\r\n\r\n', 1)
        # HTTP/1.0 response closes; body might need another read.
        if b' 200 ' not in head.split(b'\r\n')[0]: return False
        try: data=json.loads(body)
        except json.JSONDecodeError:
            body += await asyncio.wait_for(reader.read(262144), 2)
            data=json.loads(body)
        return data.get('ok') is True and (not require_accepting or data.get('accepting_turns') is True)
    except (OSError, ValueError, asyncio.TimeoutError): return False
    finally:
        if writer is not None:
            writer.close()
            try: await writer.wait_closed()
            except OSError: pass

async def command(*args):
    proc=await asyncio.create_subprocess_exec('/usr/bin/systemctl','--user',*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out,_=await asyncio.wait_for(proc.communicate(),8)
    except TimeoutError:
        if proc.returncode is None:
            try: proc.kill()
            except ProcessLookupError: pass
        await proc.communicate()
        raise
    return proc.returncode,out.decode().strip()

async def broker_ready():
    writer = None
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_unix_connection("/run/sasocq-control/broker.sock")
            query = {"action":"broker.capacity","request_id":secrets.token_hex(16),"params":{}}
            writer.write(json.dumps(query).encode() + b"\n")
            await writer.drain()
            result = json.loads(await reader.readline())
            return result.get("ok") is True and result.get("result",{}).get("reconciliation_required") is False
    except (OSError, ValueError, TimeoutError):
        return False
    finally:
        if writer is not None: writer.close()

async def readiness():
    backend, front, broker = await asyncio.gather(health(8788),health(8787),broker_ready())
    return {"backend":backend, "front":front, "broker":broker}

async def ready():
    return all((await readiness()).values())

async def application_ready():
    return all(await asyncio.gather(health(8788), health(8787)))

async def status():
    checks, (_, state) = await asyncio.gather(readiness(), command("is-active","dex-restore.service"))
    reasons = {"backend":"O Dex não está aceitando mensagens.", "front":"A entrada do Dex está indisponível.", "broker":"Operações administrativas podem estar bloqueadas enquanto o broker requer recuperação."}
    value = {"running":state in ("active","activating","deactivating"), "ready":all(checks.values()),
             "application_ready":checks["backend"] and checks["front"],
             "reason":" ".join(reason for key,reason in reasons.items() if not checks[key])}
    try: value["last_result"]=json.loads((BASE/"result.json").read_text()).get("result")
    except (OSError,ValueError): pass
    return value

async def respond(writer,code,body,kind='application/json',extra=''):
    if isinstance(body,str): body=body.encode()
    writer.write((f'HTTP/1.1 {code}\r\nContent-Type: {kind}; charset=utf-8\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n'+extra+'\r\n').encode()+body)
    await writer.drain()

async def client(reader,writer):
    upstream=None
    try:
        raw=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),10)
        if len(raw)>32768: return
        first,*lines=raw.decode('latin1').split('\r\n')
        method,target,_=first.split(' ',2)
        path=urlsplit(target).path
        headers={}
        for line in lines:
            if ':' in line:
                key,val=line.split(':',1)
                if key.lower() in headers: raise ValueError('duplicate header')
                headers[key.lower()]=val.strip()
        access_cookie=''
        if path.startswith(PREFIX) and path!=PREFIX+'/watch.js':
            try:access_cookie=await asyncio.to_thread(authorize,headers)
            except AccessDenied as error:
                code = '403 Forbidden' if error.status == 403 else '401 Unauthorized'
                if method == 'GET' and path in (PREFIX, PREFIX + '/'):
                    return await respond(writer, code, AUTH_PAGE, 'text/html; charset=utf-8',
                                         "Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'\r\n")
                return await respond(writer, code, json.dumps({'detail':'Autenticação e dispositivo autorizado do Dex necessários.'}))
        if path==PREFIX or path==PREFIX+'/':
            if method!='GET': return await respond(writer,'405 Method Not Allowed','{}')
            return await respond(writer,'200 OK',HTML.replace('__TOKEN__',TOKEN),'text/html',access_cookie+"Content-Security-Policy: frame-ancestors 'none'; base-uri 'none'\r\n")
        if path==PREFIX+'/status' and method=='GET':
            try: value = await status()
            except (OSError, TimeoutError):
                LOG.exception("Unable to query Dex recovery status")
                return await respond(writer,'503 Service Unavailable','{}',extra=access_cookie)
            return await respond(writer,'200 OK',json.dumps(value),extra=access_cookie)
        if path==PREFIX+'/watch.js' and method=='GET':
            return await respond(writer,'200 OK',WATCH,'application/javascript')
        if path==PREFIX+'/restore':
            if method!='POST': return await respond(writer,'405 Method Not Allowed','{}')
            if (headers.get('origin')!=ORIGIN or headers.get('sec-fetch-site','same-origin')!='same-origin'
                    or not hmac.compare_digest(headers.get('x-dex-recovery',''),TOKEN)
                    or headers.get('transfer-encoding') or headers.get('content-length','0')!='0'):
                return await respond(writer,'403 Forbidden','{}')
            # Fixed unit and fixed prompt; no user-supplied shell or CLI parameters.
            try: rc,_=await command('start','--no-block','dex-restore.service')
            except (OSError, TimeoutError):
                LOG.exception("Unable to submit Dex recovery")
                return await respond(writer,'503 Service Unavailable','{"accepted":false}',extra=access_cookie)
            return await respond(writer,'202 Accepted' if rc==0 else '503 Service Unavailable',json.dumps({'accepted':rc==0}),extra=access_cookie)
        if path.startswith(PREFIX): return await respond(writer,'404 Not Found','{}')
        if method in ('GET','HEAD') and 'text/html' in headers.get('accept','') and not await application_ready():
            return await respond(writer,'302 Found','',extra=f'Location: {PREFIX}/\r\n')
        try: remote,upstream=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',8787),3)
        except (OSError,asyncio.TimeoutError):
            if method in ('GET','HEAD'): return await respond(writer,'302 Found','',extra=f'Location: {PREFIX}/\r\n')
            return await respond(writer,'503 Service Unavailable',json.dumps({'recovery_url':PREFIX+'/'}))
        # Route EVERY HTTP request through this gateway. A pooled keep-alive
        # connection must not send a later /restore request straight to Dex.
        # WebSocket upgrades remain transparent, as do streaming response bodies.
        if headers.get('upgrade','').lower()!='websocket':
            excluded={'connection','proxy-connection'}
            inject=method=='GET' and path in ('/','/index.html')
            if inject: excluded|={'accept-encoding','if-none-match','if-modified-since'}
            kept=[line for line in lines if line and line.split(':',1)[0].lower() not in excluded]
            if inject:kept.append('Accept-Encoding: identity')
            raw=(first+'\r\n'+'\r\n'.join(kept)+'\r\nConnection: close\r\n\r\n').encode('latin1')
        else:inject=False
        upstream.write(raw);await upstream.drain()
        if inject:
            response_head=await asyncio.wait_for(remote.readuntil(b'\r\n\r\n'),10)
            response_lines=response_head.decode('latin1').split('\r\n')
            response_headers={line.split(':',1)[0].lower():line.split(':',1)[1].strip() for line in response_lines[1:] if ':' in line}
            size=int(response_headers.get('content-length','-1'))
            if (b' 200 ' in response_head.split(b'\r\n')[0] and 'text/html' in response_headers.get('content-type','')
                    and not response_headers.get('transfer-encoding') and not response_headers.get('content-encoding') and 0<=size<=8388608):
                body=await asyncio.wait_for(remote.readexactly(size),15)
                tag=b'<script defer src="/__dex_recovery/watch.js"></script>'
                if b'</head>' in body: body=body.replace(b'</head>',tag+b'</head>',1)
                kept=[line for line in response_lines[1:] if line and line.split(':',1)[0].lower() not in ('content-length','etag','connection','cache-control')]
                writer.write((response_lines[0]+'\r\n'+'\r\n'.join(kept)+f'\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n').encode('latin1')+body)
                await writer.drain();return
            writer.write(response_head);await writer.drain()
        async def copy(src,dst):
            try:
                while chunk:=await src.read(65536): dst.write(chunk);await dst.drain()
            except (OSError,ConnectionError): pass
            finally:
                try: dst.write_eof()
                except (OSError,AttributeError): pass
        await asyncio.gather(copy(reader,upstream),copy(remote,writer))
    except (OSError,ValueError,asyncio.TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError): pass
    finally:
        for stream in (upstream,writer):
            if stream:
                stream.close()
                try: await stream.wait_closed()
                except OSError: pass

def planned_update():
    rollout = Path.home()/".local/share/codex-linux-control/rollout/update-transaction.json"
    try:
        tx = json.loads(rollout.read_bytes())
        return tx.get("phase") in {"applying","verifying","rollback"} and tx.get("deadline",0)>time.time()
    except (OSError,ValueError,TypeError):
        return False

async def monitor():
    markers = {"application": BASE/"application-incident-attempt.json",
               "broker": BASE/"incident-attempt.json"}
    failures = dict.fromkeys(markers, 0)
    successes = dict.fromkeys(markers, 0)
    while True:
        await asyncio.sleep(20)
        checks = await readiness()
        healthy = {"application": checks["backend"] and checks["front"], "broker": checks["broker"]}
        for scope, up in healthy.items():
            if up:
                failures[scope] = 0
                successes[scope] += 1
                if successes[scope] >= 3: markers[scope].unlink(missing_ok=True)
            else:
                successes[scope] = 0
                failures[scope] += 1
        scope = "broker" if healthy["application"] else "application"
        attempted = markers[scope]
        if healthy[scope] or failures[scope] < 3 or planned_update() or attempted.exists(): continue
        try: _, state = await command("is-active","dex-restore.service")
        except (OSError, TimeoutError):
            LOG.exception("Unable to query automatic Dex recovery")
            continue
        if state in ("active","activating","deactivating"): continue
        BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with attempted.open("x") as out:
                json.dump({"started_at":time.time(), "scope":scope},out)
                out.flush()
                os.fsync(out.fileno())
        except FileExistsError:
            continue
        fd = os.open(BASE,os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        try: rc,_ = await command("start","--no-block","dex-restore.service")
        except (OSError, TimeoutError):
            # Submission may have reached systemd. Keep the incident marker.
            LOG.exception("Automatic Dex recovery submission could not be confirmed")
        else:
            if rc:
                attempted.unlink(missing_ok=True)
                LOG.warning("Automatic Dex recovery submission was rejected (exit %s)", rc)

async def main():
    server=await asyncio.start_server(client,'127.0.0.1',8789,limit=32768)
    async with server: await asyncio.gather(server.serve_forever(),monitor())

if __name__=='__main__': asyncio.run(main())
