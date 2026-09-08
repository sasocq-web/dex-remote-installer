#!/usr/bin/python3
import json, os, pathlib, signal, socket, struct, subprocess, sys, time, uuid
os.umask(0o077)
for path in ('/work/home','/work/artifacts','/tmp/runtime'): pathlib.Path(path).mkdir(parents=True,exist_ok=True)
internet=os.environ.get('SASOCQ_LAB_INTERNET')=='1'
if internet:
    proxy=subprocess.Popen(['/usr/bin/python3','/lab/egress.py','bridge'])
profile=os.environ.get('SASOCQ_LAB_PROFILE','generic')
if profile=='roblox':
    for _ in range(100):
        if pathlib.Path('/control/network.ready').exists():break
        time.sleep(.1)
    else:sys.exit('Rede isolada indisponível')
gui='--gui' in sys.argv
if gui:
    os.environ['DISPLAY']=':99'
    log=open('/work/artifacts/display.log','w')
    display=subprocess.Popen(['/usr/bin/Xtigervnc',':99','-geometry','1280x800','-depth','24','-nolisten','tcp',
        '-rfbport','-1','-rfbunixpath','/control/view.sock','-rfbunixmode','0600','-SecurityTypes','None',('+extension' if profile=='roblox' else '-extension'),'GLX'],stdout=log,stderr=subprocess.STDOUT)
    for _ in range(60):
        if display.poll() is not None: sys.exit('Tela virtual não iniciou')
        if pathlib.Path('/tmp/.X11-unix/X99').exists(): break
        time.sleep(.1)
profile=os.environ.get('SASOCQ_LAB_PROFILE','generic')
if gui:
    wm_log=open('/work/artifacts/window-manager.log','w')
    wm=subprocess.Popen(['/usr/bin/openbox','--sm-disable'],stdout=wm_log,stderr=subprocess.STDOUT)
children=[]
with socket.socket(socket.AF_UNIX) as server:
    server.bind('/control/control.sock'); server.listen(4); server.settimeout(1)
    while True:
        children[:]=[p for p in children if p.poll() is None]
        try: conn,_=server.accept()
        except socket.timeout: continue
        with conn:
            conn.settimeout(5)
            try:
                pid,uid,gid=struct.unpack('3i',conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if uid not in (os.getuid(),65534): raise ValueError('Identidade não autorizada')
                data=b''
                while b'\n' not in data:
                    part=conn.recv(8192)
                    if not part: raise ValueError('Pedido incompleto')
                    data+=part
                    if len(data)>65536: raise ValueError('Pedido grande demais')
                req=json.loads(data.split(b'\n')[0]); op=req.get('op')
                if op=='status': result={'ok':True,'runtime':'host-bubblewrap','hostname':socket.gethostname(),'network':'public-ipv4-filtered' if profile=='roblox' else 'public-web-proxy' if internet else 'none','gui':gui,'children':len(children),'work':'/work','vm_used':False,'profile':profile}
                elif op=='screenshot':
                    if not gui: raise ValueError('Inicie com --gui')
                    out='/work/artifacts/screenshot.png'
                    p=subprocess.run(['/usr/bin/import','-display',':99','-window','root',out],capture_output=True,text=True,timeout=15)
                    result={'ok':p.returncode==0,'path':out,'error':p.stderr[-2000:]}
                elif op in ('exec','launch'):
                    argv=req.get('argv')
                    if not isinstance(argv,list) or not 1<=len(argv)<=128 or any(not isinstance(s,str) or '\0' in s or len(s)>16384 for s in argv): raise ValueError('argv inválido')
                    if op=='launch':
                        ident=uuid.uuid4().hex
                        with open('/work/artifacts/'+ident+'.log','wb') as log:
                            p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                        if argv[0]=='/lab/bin/roblox-studio':
                            try:
                                rc=p.wait(timeout=.25)
                                if rc: raise ValueError('Studio recusou a abertura; código '+str(rc)+'. Consulte /work/artifacts/'+ident+'.log')
                            except subprocess.TimeoutExpired: pass
                        children.append(p); result={'ok':True,'pid':p.pid,'log':'/work/artifacts/'+ident+'.log'}
                    else:
                        # Bounded output goes to disk, not unbounded controller RAM.
                        path='/work/artifacts/exec-'+uuid.uuid4().hex+'.log'
                        with open(path,'wb') as log:
                            p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                            try: rc=p.wait(timeout=60)
                            except subprocess.TimeoutExpired:
                                os.killpg(p.pid,signal.SIGKILL); p.wait(); rc=124
                        with open(path,'rb') as log:
                            log.seek(0,2); size=log.tell(); log.seek(max(0,size-256000)); output=log.read().decode(errors='replace')
                        result={'ok':rc==0,'returncode':rc,'output':output,'log':path,'truncated':size>256000}
                else: raise ValueError('Operação desconhecida')
            except Exception as e: result={'ok':False,'error':str(e)}
            try: conn.sendall(json.dumps(result).encode()+b'\n')
            except (OSError,ValueError): pass
