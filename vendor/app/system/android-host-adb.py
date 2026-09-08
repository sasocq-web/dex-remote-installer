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

def validate(argv):
    if not argv: raise ValueError('Operação obrigatória')
    op=argv[0]; args=argv[1:]
    if op not in {'status','ui','apps','is-installed','open','play','back','home','tap','type'}: raise ValueError('Operação não permitida')
    expected=2 if op=='tap' else 1 if op in {'is-installed','open','play','type'} else 0
    if len(args)!=expected: raise ValueError('Argumentos inválidos')
    if op in {'is-installed','open','play'} and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+',args[0]): raise ValueError('Pacote inválido')
    if op=='tap' and any(not re.fullmatch(r'[0-9]{1,4}',a) or not 0<=int(a)<=8192 for a in args): raise ValueError('Coordenadas inválidas')
    if op=='type' and (not args[0] or len(args[0])>2000 or any(c in args[0] for c in '\x00\n\r')): raise ValueError('Texto inválido')
    return op,args

def main():
    op,args=validate(sys.argv[1:])
    if os.getuid()!=1001: raise RuntimeError('Requer a identidade de Projetos no host')
    if not pathlib.Path('/run/user/1001/sasocq-android-adb.sock').is_socket(): raise RuntimeError('Serviço ADB do host não está ativo')
    deadline=time.monotonic()+75
    while True:
        try:
            run(['connect',SERIAL],3)
            if run(['-s',SERIAL,'get-state'],3)=='device' and shell(['getprop','sys.boot_completed'],3)=='1': break
        except (RuntimeError,subprocess.TimeoutExpired): pass
        if time.monotonic()>=deadline: raise RuntimeError('Boot Android não concluído no prazo')
        time.sleep(1)
    if op=='status': out='runtime=host\ntransport=private-adb\nboot_completed=1\nandroid='+shell(['getprop','ro.build.version.release'])
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
    else:
        import shlex
        # ADB shell reparses arguments; quote text exactly once at that boundary.
        out=shell(['input','text',shlex.quote(args[0])])
    print(out)
if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError,IndexError,subprocess.SubprocessError) as e: sys.exit(str(e))
