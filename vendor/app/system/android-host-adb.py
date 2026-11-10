#!/usr/bin/python3
"""Android host transport using a private on-demand ADB server."""
import os, pathlib, re, subprocess, sys, time
ADB='/usr/bin/adb'
SOCKET='localfilesystem:/run/user/1001/sasocq-android-adb.sock'
SERIAL='192.168.240.112:5555'

def run(args,timeout=15,check=True):
    p=subprocess.run([ADB,'-L',SOCKET,*args],capture_output=True,text=True,timeout=timeout)
    if check and p.returncode: raise RuntimeError((p.stderr or p.stdout)[-1500:])
    return p.stdout.strip()

def shell(args,timeout=15): return run(['-s',SERIAL,'shell',*args],timeout)

# Mesa's Android screencap can emit this fixed missing optional PCI database
# diagnostic on stdout. Accept only this exact prefix, never arbitrary text.
_CAPTURE_DRIVER_NOTICE=b'/vendor/etc/hwdata/amdgpu.ids: No such file or directory\n'
def capture_png(data):
    if data.startswith(_CAPTURE_DRIVER_NOTICE):
        data=data[len(_CAPTURE_DRIVER_NOTICE):]
    if not data.startswith(bytes.fromhex('89504e470d0a1a0a')):
        raise ValueError('invalid_png')
    return data

def capture():
    """Read a complete PNG directly; no shared device-side temporary file.

    Only this read-only operation may retry. UI writes must never be replayed.
    Report bounded, non-private diagnostics even when Android emits no stderr.
    """
    from lab_conversation import image_url
    failures=[]
    for attempt in range(2):
        try:
            p=subprocess.run([ADB,'-L',SOCKET,'-s',SERIAL,'exec-out','screencap','-p'],
                             capture_output=True,timeout=15,check=False)
            if p.returncode:
                failures.append('code='+str(p.returncode)+' stdout_bytes='+str(len(p.stdout))+
                                ' stderr_bytes='+str(len(p.stderr)))
            else:
                try:
                    return image_url(capture_png(p.stdout))
                except (OSError,ValueError):
                    failures.append('invalid_image bytes='+str(len(p.stdout)))
        except subprocess.TimeoutExpired:
            failures.append('timeout=15s')
        if attempt==0: time.sleep(0.25)
    raise RuntimeError('Android screenshot failed after 2 bounded read attempts: '+'; '.join(failures))

def validate(argv):
    if not argv: raise ValueError('Operação obrigatória')
    op=argv[0]; args=argv[1:]
    if op not in {'status','ui','apps','is-installed','open','play','back','home','tap','type','screenshot','swipe','long_press','orientation','pinch','observe_nc'}: raise ValueError('Operação não permitida')
    expected=5 if op in {'swipe','pinch'} else 3 if op=='long_press' else 2 if op=='tap' else 1 if op in {'is-installed','open','play','type','orientation','observe_nc'} else 0
    if len(args)!=expected: raise ValueError('Argumentos inválidos')
    if op in {'is-installed','open','play'} and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+',args[0]): raise ValueError('Pacote inválido')
    if op in {'tap','swipe','long_press'}:
        coords=args[:-1] if op in {'swipe','long_press'} else args
        if any(not re.fullmatch(r'[0-9]{1,4}',a) or not 0<=int(a)<=8192 for a in coords): raise ValueError('Coordenadas inválidas')
    if op in {'swipe','long_press'} and (not re.fullmatch(r'[0-9]{1,4}',args[-1]) or not 100<=int(args[-1])<=5000): raise ValueError('Duração inválida')
    if op=='orientation' and args[0] not in {'portrait','landscape','auto'}: raise ValueError('Orientação inválida')
    if op=='pinch':
        if any(not re.fullmatch(r'[0-9]{1,4}',a) for a in args): raise ValueError('Pinch inválido')
        x,y,r1,r2,duration=map(int,args)
        if not (0<=x<=8192 and 0<=y<=8192 and 1<=r1<=4096 and 1<=r2<=4096 and r1!=r2 and 100<=duration<=5000 and x-max(r1,r2)>=0 and x+max(r1,r2)<=8192):
            raise ValueError('Pinch fora dos limites')
    if op=='type' and (not args[0] or len(args[0])>2000 or any(c in args[0] for c in '\x00\n\r')): raise ValueError('Texto inválido')
    if op=='observe_nc' and args[0] not in {'pdc','avaliacao','apontamento','ssm','controles'}: raise ValueError('Módulo não registrado')
    return op,args

DEX_SHA256='e6934356b9c46296dc36a9e68648fe64e2c040fbdafdc814dd70094ace47b626'
def pinch(args):
    import hashlib
    dex=pathlib.Path(__file__).resolve().with_name('android-gestures.dex')
    if dex.is_symlink() or not dex.is_file() or dex.stat().st_size>100000:
        raise RuntimeError('Helper multitouch indisponível')
    if hashlib.sha256(dex.read_bytes()).hexdigest()!=DEX_SHA256:
        raise RuntimeError('Integridade do helper multitouch inválida')
    remote='/data/local/tmp/sasocq-android-gestures.dex'
    command=['timeout','10','env','CLASSPATH='+remote,'app_process','/','SasocqPinch']
    try:
        # Fixed destination, under the lifecycle's exclusive conversation lease.
        shell(['rm','-f',remote],10)
        run(['-s',SERIAL,'push',str(dex),remote],15)
        shell(['chmod','0444',remote],10)
        if shell(['sha256sum',remote]).split()[0]!=DEX_SHA256:
            raise RuntimeError('Integridade Android do helper inválida')
        try:
            out=shell([*command,*args],15)
            if 'pinch_completed=true pointers=2' not in out:
                raise RuntimeError('Android não confirmou gesto multitouch')
            return out
        except BaseException:
            # Separate bounded invocation also cancels if timeout killed the main helper.
            shell([*command,'cancel'],15)
            raise
    finally:
        shell(['rm','-f',remote],10)

def main():
    op,args=validate(sys.argv[1:])
    if os.getuid()!=1001: raise RuntimeError('Requer a identidade de Projetos no host')
    if not pathlib.Path('/run/user/1001/sasocq-android-adb.sock').is_socket(): raise RuntimeError('Serviço ADB do host não está ativo')
    if op=='observe_nc':
        # Never enter bootstrap/connect/retry or invoke the auto-starting ADB CLI.
        import json
        from android_nc_probe import collect
        print(json.dumps(collect(args[0])))
        return
    deadline=time.monotonic()+75
    while True:
        try:
            run(['connect',SERIAL],3)
            if run(['-s',SERIAL,'get-state'],3)=='device' and shell(['getprop','sys.boot_completed'],3)=='1': break
        except (RuntimeError,subprocess.TimeoutExpired): pass
        if time.monotonic()>=deadline: raise RuntimeError('Boot Android não concluído no prazo')
        time.sleep(1)
    if op=='status': out='runtime=host\ntransport=private-adb\nboot_completed=1\nandroid='+shell(['getprop','ro.build.version.release'])
    elif op=='screenshot':
        import json
        out=json.dumps({'image_url':capture()})
    elif op=='ui':
        shell(['uiautomator','dump','/sdcard/sasocq-window.xml'],30)
        out=run(['-s',SERIAL,'exec-out','cat','/sdcard/sasocq-window.xml'])
        if '<hierarchy' not in out: raise RuntimeError('Hierarquia Android indisponível')
    elif op=='apps': out=shell(['pm','list','packages'])
    elif op=='is-installed': out=shell(['pm','path',args[0]])
    elif op=='open':
        component=shell(['cmd','package','resolve-activity','--brief','-a','android.intent.action.MAIN','-c','android.intent.category.LAUNCHER',args[0]]).splitlines()[-1]
        if not re.fullmatch(r'[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+',component): raise RuntimeError('Pacote sem atividade inicializável')
        out=shell(['am','start','-W','-n',component],30)
        if 'Error:' in out or 'Exception' in out: raise RuntimeError(out)
    elif op=='play': out=shell(['am','start','-a','android.intent.action.VIEW','-d','market://details?id='+args[0],'-p','com.android.vending'])
    elif op in {'back','home'}: out=shell(['input','keyevent','KEYCODE_'+op.upper()])
    elif op=='tap': out=shell(['input','tap',*args])
    elif op=='pinch': out=pinch(args)
    elif op=='swipe': out=shell(['input','touchscreen','swipe',*args])
    elif op=='long_press': out=shell(['input','touchscreen','swipe',args[0],args[1],args[0],args[1],args[2]])
    elif op=='orientation':
        rotation=['free']
        if args[0]!='auto':
            sizes=re.findall(r'(?:Physical|Override) size: ([0-9]+)x([0-9]+)',shell(['wm','size']))
            if not sizes: raise RuntimeError('Dimensões naturais Android indisponíveis')
            width,height=map(int,sizes[-1])
            portrait=0 if width<=height else 1
            rotation=['lock',str(portrait if args[0]=='portrait' else 1-portrait)]
        out=shell(['wm','user-rotation',*rotation])
        if 'Error' in out or 'Exception' in out: raise RuntimeError(out)
        out=out+'\nuser-rotation='+shell(['wm','user-rotation'])
    else:
        import shlex
        # ADB shell reparses arguments; quote text exactly once at that boundary.
        out=shell(['input','text',shlex.quote(args[0])])
    print(out)
if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError,IndexError,subprocess.SubprocessError) as e: sys.exit(str(e))

