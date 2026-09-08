#!/usr/bin/python3
import fcntl, json, os, pathlib, re, select, shutil, subprocess, sys, time
name=sys.argv[1]
assert re.fullmatch('[a-z][a-z0-9-]{0,39}',name)
root=pathlib.Path('/srv/sasocq/lab'); work=root/name
control=pathlib.Path('/run/user/1001/sasocq-lab')/name
(control/'control.sock').unlink(missing_ok=True)
roblox='--roblox' in sys.argv
if roblox:
    lease=open('/run/user/1001/sasocq-lab/roblox.lock','w')
    try: fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: sys.exit('Outro laboratório Roblox já está ativo')
internet='--internet' in sys.argv
if internet and not roblox:
    from egress import start_host
    egress=start_host(str(control/'egress.sock'))
cmd=['/usr/bin/bwrap','--unshare-all','--die-with-parent','--new-session','--cap-drop','ALL',
    '--hostname','sasocq-lab','--clearenv',
    '--ro-bind','/usr','/usr','--symlink','usr/bin','/bin','--symlink','usr/sbin','/sbin',
    '--symlink','usr/lib','/lib','--symlink','usr/lib64','/lib64',
    '--proc','/proc','--dev','/dev','--size',str(256*1024**2),'--tmpfs','/tmp',
    '--size',str(128*1024**2),'--tmpfs','/dev/shm','--dir','/run',
    '--dir','/etc','--ro-bind','/usr/local/lib/sasocq-lab/etc/passwd','/etc/passwd',
    '--ro-bind','/usr/local/lib/sasocq-lab/etc/group','/etc/group',
    '--ro-bind','/usr/local/lib/sasocq-lab/etc/nsswitch.conf','/etc/nsswitch.conf',
    '--ro-bind','/usr/local/lib/sasocq-lab/etc/hosts','/etc/hosts',
    '--ro-bind','/usr/local/lib/sasocq-lab','/lab',
    '--bind',str(work),'/work','--bind',str(control),'/control',
    '--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/work/home',
    '--setenv','LANG','C.UTF-8','--setenv','USER','lab','--setenv','LOGNAME','lab',
    '--setenv','XDG_RUNTIME_DIR','/tmp/runtime','--setenv','XDG_CACHE_HOME','/work/home/.cache',
    '--setenv','XDG_CONFIG_HOME','/work/home/.config','--setenv','XDG_DATA_HOME','/work/home/.local/share',
    '--setenv','WINEPREFIX','/work/home/.wine','--chdir','/work']
for path in ('/etc/fonts','/etc/alternatives','/etc/ld.so.cache','/etc/localtime','/etc/ssl/certs'):
    if os.path.exists(path): cmd += ['--ro-bind',path,path]
if '--gpu' in sys.argv:
    nodes=list(pathlib.Path('/dev/dri').glob('renderD*'))
    if not nodes or not all(os.access(p,os.R_OK|os.W_OK) for p in nodes):
        sys.exit('GPU render não está autorizada para esta identidade; nenhum acesso foi ampliado')
    for node in nodes: cmd += ['--dev-bind',str(node),str(node)]
if '--wine' in sys.argv:
    runtime='/var/lib/sasocq-lab/runtimes/wine10'
    if not pathlib.Path(runtime+'/usr/lib/x86_64-linux-gnu/wine/wine').is_file():
        sys.exit('Runtime Wine ainda não preparado pelo Sistema')
    cmd += ['--ro-bind',runtime+'/usr/share/wine','/usr/share/wine','--ro-bind',runtime+'/usr/lib/x86_64-linux-gnu/wine','/usr/lib/x86_64-linux-gnu/wine','--ro-bind',runtime,'/opt/wine','--setenv','PATH','/lab/bin:/opt/wine/usr/bin:/usr/bin:/bin',
        '--setenv','LD_LIBRARY_PATH','/opt/wine/usr/lib/x86_64-linux-gnu',
        '--setenv','WINEDLLPATH','/opt/wine/usr/lib/x86_64-linux-gnu/wine',
        '--setenv','WINEDEBUG','-all','--setenv','LIBGL_ALWAYS_SOFTWARE','1']
if roblox:
    metadata=json.loads((work/'vm-import/manifest.json').read_text())
    source=metadata['source']; prefix=metadata['prefix']
    if not re.fullmatch(r'/srv/sasocq/projects/[A-Za-z0-9_. -]{1,80}/test-client',source) or not re.fullmatch(r'wine-[a-z0-9-]{1,60}',prefix):
        sys.exit('Manifesto de importação inválido')
    cmd += ['--symlink','/work/vm-import/test-client',source,
        '--ro-bind','/usr/local/lib/sasocq-lab/etc/roblox-passwd','/etc/passwd',
        '--setenv','USER','brotalume-test','--setenv','LOGNAME','brotalume-test',
        '--setenv','WINEPREFIX','/work/vm-import/test-client/'+prefix,
        '--setenv','SASOCQ_LAB_PROFILE','roblox',
        '--ro-bind','/usr/local/lib/sasocq-lab/etc/net-resolv.conf','/etc/resolv.conf',
        '--ro-bind','/usr/local/lib/sasocq-lab/etc/net-nsswitch.conf','/etc/nsswitch.conf']
if internet and not roblox:
    for key in ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY'):
        cmd += ['--setenv',key,'http://127.0.0.1:3128']
    cmd += ['--setenv','no_proxy','localhost,127.0.0.1','--setenv','SASOCQ_LAB_INTERNET','1']
if roblox:
    info_r,info_w=os.pipe()
    cmd += ['--json-status-fd',str(info_w)]
cmd += ['--','/usr/bin/python3','/lab/session.py'] + (['--gui'] if '--gui' in sys.argv else [])
print(json.dumps({'event':'lab_start','name':name,'network':'public-ipv4-filtered' if roblox else 'public-web-proxy' if internet else 'none','gui':'--gui' in sys.argv,'gpu':'--gpu' in sys.argv}),flush=True)
(control/'network.ready').unlink(missing_ok=True)
child=subprocess.Popen(cmd,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},pass_fds=(info_w,) if roblox else ())
if roblox: os.close(info_w)
network=None
if roblox:
    try:
        if not select.select([info_r],[],[],5)[0]: raise RuntimeError('Namespace não iniciou')
        info=os.read(info_r,4096).decode(); child_pid=json.loads(info.splitlines()[0])['child-pid']
        nr,nw=os.pipe()
        netroot='/var/lib/sasocq-lab/runtimes/net'
        network=subprocess.Popen([netroot+'/usr/bin/slirp4netns','--configure','--disable-host-loopback',
            '--disable-dns','--enable-seccomp','--ready-fd='+str(nw),str(child_pid),'tap0'],
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8',
                 'LD_LIBRARY_PATH':netroot+'/usr/lib/x86_64-linux-gnu',
                 'LD_PRELOAD':'/usr/local/lib/sasocq-lab/public-egress.so'},pass_fds=(nw,))
        os.close(nw)
        if not select.select([nr],[],[],5)[0] or os.read(nr,1) not in (b'\x01',b'1'):
            raise RuntimeError('Rede privada não iniciou')
        os.close(nr)
        if '/usr/local/lib/sasocq-lab/public-egress.so' not in pathlib.Path('/proc/'+str(network.pid)+'/maps').read_text():
            raise RuntimeError('Filtro de rede não carregado')
        (control/'network.ready').touch(mode=0o600)
    except Exception:
        child.terminate()
        if network: network.terminate()
        raise
if roblox: os.close(info_r)
last_scan=0
while child.poll() is None:
    if network and network.poll() is not None:
        child.terminate(); break
    # Dedicated hard CPU/RAM/PID ceilings are enforced by the parent cgroup.
    # Disk has a 4 GiB per-file hard ceiling and a monitored aggregate guard.
    if shutil.disk_usage(root).free < 50*1024**3:
        print('disk_reserve_guard',flush=True); child.terminate(); break
    if time.monotonic()-last_scan>10:
        total=0
        for base,dirs,files in os.walk(root,followlinks=False):
            for filename in files:
                try: total+=os.lstat(os.path.join(base,filename)).st_blocks*512
                except OSError: pass
        if total>40*1024**3:
            print('lab_storage_guard',flush=True); child.terminate(); break
        last_scan=time.monotonic()
    time.sleep(1)
try: rc=child.wait(timeout=5)
except subprocess.TimeoutExpired: child.kill(); rc=child.wait()
if network:
    network.terminate()
    try: network.wait(timeout=3)
    except subprocess.TimeoutExpired: network.kill()
print(json.dumps({'event':'lab_stop','name':name,'returncode':rc}),flush=True)
(control/'control.sock').unlink(missing_ok=True)
sys.exit(rc if rc>=0 else 1)
