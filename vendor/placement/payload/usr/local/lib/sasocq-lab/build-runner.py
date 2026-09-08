#!/usr/bin/python3
import fcntl,json,os,pathlib,re,shutil,signal,subprocess,sys,time
root=pathlib.Path('/srv/sasocq/development');name=sys.argv[1]
assert re.fullmatch('[a-z][a-z0-9-]{0,39}',name)
job=root/'jobs'/name;req=json.loads((job/'request.json').read_text())
status={'name':name,'phase':'starting','started':time.time(),'vm_used':False,'rootless':True}
def save(**v):
 status.update(v);temp=job/'status.tmp';temp.write_text(json.dumps(status,indent=2));temp.replace(job/'status.json')
save()
def interrupted(signum,frame):
 save(phase='stopped',completed=time.time(),reason='service-stop-or-timeout')
 raise SystemExit(128+signum)
signal.signal(signal.SIGTERM,interrupted)
lock=(root/'builder.lock').open('w')
try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:save(phase='refused',error='Outra construção/execução de container já está ativa');sys.exit(75)
for folder in ['builder-home','container-store','artifacts']:(root/folder).mkdir(mode=0o700,exist_ok=True)
runtime='/run/user/1001/sasocq-builder';pathlib.Path(runtime).mkdir(mode=0o700,exist_ok=True)
env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(root/'builder-home'),'USER':'codex-worker','LOGNAME':'codex-worker','LANG':'C.UTF-8','XDG_RUNTIME_DIR':'/run/user/1001','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1001/bus','CONTAINERS_CONF':'/usr/local/lib/sasocq-lab/containers.conf','REGISTRY_AUTH_FILE':str(root/'builder-home/auth.json')}
auth=root/'builder-home/auth.json'
if not auth.exists():auth.write_text('{"auths":{}}')
podman=['/usr/bin/podman','--root',str(root/'container-store'),'--runroot',runtime,'--cgroup-manager=cgroupfs']
cgroup=pathlib.Path(pathlib.Path('/proc/self/cgroup').read_text().split('0::',1)[1].strip())
assert cgroup.name=='supervisor',str(cgroup)
parent=str(cgroup.parent/'payload')
log=(job/'build.log').open('wb')
def execute(argv):
 p=subprocess.Popen(argv,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 while p.poll() is None:
  if shutil.disk_usage(root).free<80*1024**3:
   save(phase='failed',error='Reserva de disco abaixo de 80 GiB')
   subprocess.run(['systemctl','--user','stop','sasocq-build-'+name+'.service'],env=env)
   raise RuntimeError('disk reserve')
  time.sleep(.5)
 if p.returncode:raise RuntimeError('Comando terminou com código '+str(p.returncode)+'; consulte build.log')
try:
 if req['op']=='build':
  source=pathlib.Path(req['context']).resolve(strict=True);ctx=job/'context'
  if ctx.exists():
   # A fresh generation preserves previous contexts instead of deleting them.
   ctx=job/('context-'+str(time.time_ns()))
  ctx.mkdir(mode=0o700)
  save(phase='copying-context')
  rsync=['rsync','-a','--safe-links']
  for exclude in ['.git','.ssh','.codex','.config','.env*','secrets','credentials','*.pem','*.key','*.p12','*.pfx','auth.json','node_modules','.next','.venv','__pycache__','backups','Backup Conversas Codex']:
   rsync+=['--exclude='+exclude]
  execute(rsync+[str(source)+'/',str(ctx)+'/'])
  save(phase='building',context=str(ctx))
  cmd=podman+['build','--format=docker','--network=slirp4netns:allow_host_loopback=false','--dns=1.1.1.1','--dns=9.9.9.9','--cgroup-parent='+parent,'--security-opt=no-new-privileges','--memory=6g','--memory-swap=6g','--cpu-period=100000','--cpu-quota=800000','--file',str(ctx/req['file']),'--tag',req['tag']]
  if req.get('target'):cmd+=['--target',req['target']]
  execute(cmd+[str(ctx)])
  save(phase='exporting')
  artifact=root/'artifacts'/(name+'.tar')
  if artifact.exists():artifact=root/'artifacts'/(name+'-'+str(time.time_ns())+'.tar')
  execute(podman+['save','--format=docker-archive','--output',str(artifact),req['tag']])
  save(phase='complete',image=req['tag'],artifact=str(artifact),completed=time.time())
 else:
  save(phase='running-test')
  execute(podman+['run','--pull=never','--rm','--cgroups=disabled','--network=slirp4netns:allow_host_loopback=false','--dns=1.1.1.1','--dns=9.9.9.9','--security-opt=no-new-privileges','--cap-drop=ALL',req['image'],*req['argv']])
  save(phase='complete',completed=time.time())
except Exception as e:save(phase='failed',error=str(e));raise
