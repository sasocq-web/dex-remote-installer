"""Reinstallable SASOCQ placement policy. Never executes a development workload."""
from __future__ import annotations
import argparse,base64,hashlib,json,os,pathlib,pwd,shutil,subprocess,time
BUNDLE=pathlib.Path('/usr/share/sasocq-placement')
START='<!-- SASOCQ-PLACEMENT-BEGIN -->';END='<!-- SASOCQ-PLACEMENT-END -->'

def put(root,path,data,mode=0o644):
 p=root/path.lstrip('/')
 if any(x.is_symlink() for x in [p,*p.parents] if x!=root):raise RuntimeError('Refusing symlink target: '+path)
 missing=[x for x in [p.parent,*p.parent.parents] if x!=root and not x.exists()]
 p.parent.mkdir(parents=True,exist_ok=True)
 for x in missing:x.chmod(0o700 if '/.codex' in str(x) else 0o755)
 if p.exists() and p.read_bytes()==data and p.stat().st_mode&0o777==mode:return
 if p.exists():
  old=p.read_bytes();b=root/'var/backups/sasocq-placement'/hashlib.sha256(old).hexdigest()/path.lstrip('/')
  b.parent.mkdir(parents=True,exist_ok=True);b.write_bytes(old);b.chmod(0o600)
 uidgid=(p.stat().st_uid,p.stat().st_gid) if p.exists() else None
 tmp=p.with_name(p.name+'.placement-tmp');tmp.write_bytes(data);tmp.chmod(mode)
 if uidgid and os.geteuid()==0:os.chown(tmp,*uidgid)
 tmp.replace(p)

def rule_file(root,path,rule):
 p=root/path.lstrip('/');old=p.read_text() if p.exists() and not p.is_symlink() else ''
 if START in old:
  a,tail=old.split(START,1)
  if END not in tail:raise RuntimeError('Incomplete policy block '+path)
  old=a+tail.split(END,1)[1]
 put(root,path,(old.rstrip()+'\n\n'+START+'\n'+rule.rstrip()+'\n'+END+'\n').encode())

def host(root=pathlib.Path('/'),bundle=BUNDLE):
 manifest=json.loads((bundle/'manifest.json').read_text());rule=(bundle/'RULES.md').read_text()
 if root==pathlib.Path('/'):
  if os.uname().machine!=manifest['architecture']:raise RuntimeError('Unsupported lab architecture; never fall back to the VM')
  if pwd.getpwnam('codex-worker').pw_uid!=1001:raise RuntimeError('Managed codex-worker must use UID 1001; preserve existing accounts and repair through the System')
 for name,info in manifest['files'].items():
  if '..' in pathlib.PurePosixPath(name).parts or not name.startswith(('/usr/local/', '/etc/systemd/user/')):raise RuntimeError('Invalid policy destination')
  if info['mode'] not in [0o644,0o755]:raise RuntimeError('Invalid policy mode')
  data=(bundle/name.lstrip('/')).read_bytes()
  if hashlib.sha256(data).hexdigest()!=info['sha256']:raise RuntimeError('Policy payload mismatch: '+name)
  put(root,name,data,info['mode'])
 for name in ['/home/codex/.codex/AGENTS.md','/home/codex-worker/.codex/AGENTS.md','/home/codex/SystemWorkspace/AGENTS.md','/srv/sasocq/projects/AGENTS.md']:
  rule_file(root,name,rule)
 for name in ['/home/codex/.codex/skills/clc-system-admin/SKILL.md','/home/codex-worker/.codex/skills/clc-system-admin/SKILL.md']:
  if (root/name.lstrip('/')).is_file():rule_file(root,name,rule)
 if root==pathlib.Path('/'):
  worker=pwd.getpwnam('codex-worker')
  if worker.pw_uid!=1001:raise RuntimeError('Host lab requires managed codex-worker UID 1001; never fall back to the VM')
  for name in ['/srv/sasocq/lab','/srv/sasocq/development']:
   p=pathlib.Path(name);p.mkdir(mode=0o700,parents=True,exist_ok=True);os.chown(p,worker.pw_uid,worker.pw_gid);p.chmod(0o700)
  for user in ['codex','codex-worker']:
   who=pwd.getpwnam(user);p=pathlib.Path(who.pw_dir)/'.codex/AGENTS.md';os.chown(p.parent,who.pw_uid,who.pw_gid);p.parent.chmod(0o700);os.chown(p,who.pw_uid,who.pw_gid)
  subprocess.run(['loginctl','enable-linger','codex-worker'],check=True)
  subprocess.run(['systemctl','start','user@1001.service'],check=True)
  subprocess.run(['runuser','-u','codex-worker','--','env','XDG_RUNTIME_DIR=/run/user/1001','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus','systemctl','--user','daemon-reload'],check=True)
 return {'policy':'host-development-v1','host_files':len(manifest['files']),'rules_installed':True}

def vm_files(bundle=BUNDLE):
 return {'/usr/local/bin/docker':((bundle/'docker-guard').read_text(),0o755),'/etc/apparmor.d/sasocq-hosting-wine':((bundle/'wine-profile').read_text(),0o644),'/etc/sasocq-publication-only.md':((bundle/'RULES.md').read_text(),0o644)}

def vm_script(bundle=BUNDLE):
 # Exact, inspectable policy operation; no generic remote shell API.
 payload={k:[base64.b64encode(v[0].encode()).decode(),v[1]] for k,v in vm_files(bundle).items()}
 return '''import pathlib,base64,json,subprocess,hashlib,os
files=PAYLOAD
units=['sasocq-android-adb.service','sasocq-android-appium.service']
for u in units:
 r=subprocess.run(['systemctl','is-active',u],capture_output=True,text=True)
 if r.stdout.strip() in ['active','activating']:raise RuntimeError('Legacy Android active; preserve session and migrate through the System')
for name,(encoded,mode) in files.items():
 p=pathlib.Path(name);data=base64.b64decode(encoded)
 if p.is_symlink():raise RuntimeError('Unexpected symlink '+name)
 if p.exists() and p.read_bytes()!=data:
  old=p.read_bytes();b=pathlib.Path('/var/backups/sasocq-placement')/hashlib.sha256(old).hexdigest()/name.lstrip('/');b.parent.mkdir(parents=True,exist_ok=True);b.write_bytes(old);b.chmod(0o600)
 p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);p.chmod(mode)
for u in units:
 p=pathlib.Path('/etc/systemd/system')/u
 if p.is_symlink() and os.readlink(p)=='/dev/null':continue
 if p.exists() or p.is_symlink():
  b=pathlib.Path('/var/backups/sasocq-placement/android-units')/u;b.parent.mkdir(parents=True,exist_ok=True)
  if b.exists():raise RuntimeError('Existing rollback unit; inspect before replacing '+u)
  p.rename(b)
 p.symlink_to('/dev/null')
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['apparmor_parser','-r','/etc/apparmor.d/sasocq-hosting-wine'],check=True)
r=subprocess.run(['/usr/local/bin/docker','build','.'],capture_output=True,text=True);assert r.returncode==64
print(json.dumps({'vm_role':'production-only','docker_build_refused':True,'android':'masked','wine_profile':'loaded'}))
'''.replace('PAYLOAD',repr(payload))

def apply_vm(bundle=BUNDLE):
 from .server_admin import _ssh_base,server_address
 address=server_address()
 if not address:raise RuntimeError('VM not reachable; placement not verified')
 p=subprocess.run(_ssh_base(address)+['sudo','-n','python3','-'],input=vm_script(bundle),text=True,capture_output=True,timeout=60)
 if p.returncode:raise RuntimeError(p.stderr[-1500:] or p.stdout[-1500:])
 return json.loads(p.stdout)

def cloud_config(user_data,bundle=BUNDLE):
 # Inject policy in the seed; it runs before the server-ready marker.
 import yaml
 d=yaml.safe_load(user_data)
 d.setdefault('packages',[])
 for p in ['apparmor','apparmor-utils','python3']:
  if p not in d['packages']:d['packages'].append(p)
 d.setdefault('write_files',[]).append({'path':'/usr/local/lib/sasocq-placement-bootstrap.py','permissions':'0700','encoding':'b64','content':base64.b64encode(vm_script(bundle).encode()).decode()})
 import shlex
 commands=['python3 /usr/local/lib/sasocq-placement-bootstrap.py']+[shlex.join(c) if isinstance(c,list) else c for c in d.get('runcmd',[])]
 d['runcmd']=[['sh','-ec','\n'.join(commands)]]
 return '#cloud-config\n'+yaml.safe_dump(d,sort_keys=False)

def offline_vm(disk,bundle=BUNDLE):
    """Apply guard files before a restored guest ever starts. Disk must be offline."""
    import tempfile
    if not shutil.which('virt-customize'):raise RuntimeError('Restore requires virt-customize; VM must remain offline')
    with tempfile.TemporaryDirectory(prefix='sasocq-placement-offline-') as temp:
        command=['virt-customize','--no-network','--memsize','768','--format','qcow2','-a',str(disk)]
        for number,(name,(text,mode)) in enumerate(vm_files(bundle).items()):
            source=pathlib.Path(temp)/str(number);source.write_text(text)
            command+=['--run-command','if [ -f '+name+' ] && [ ! -f /var/backups/sasocq-placement/offline'+name+' ]; then mkdir -p /var/backups/sasocq-placement/offline'+str(pathlib.Path(name).parent)+'; cp -p '+name+' /var/backups/sasocq-placement/offline'+name+'; fi']
            command+=['--mkdir',str(pathlib.Path(name).parent),'--upload',str(source)+':'+name,'--chmod',format(mode,'04o')+':'+name]
        # These are fixed paths within the offline guest, never host paths.
        for unit in ['sasocq-android-adb.service','sasocq-android-appium.service']:
            path='/etc/systemd/system/'+unit
            command+=['--run-command','mkdir -p /var/backups/sasocq-placement; if [ -f '+path+' ] && [ ! -L '+path+' ]; then cp -p '+path+' /var/backups/sasocq-placement/'+unit+'; fi; ln -sfn /dev/null '+path]
        result=subprocess.run(command,capture_output=True,text=True,timeout=600,env={**os.environ,'LIBGUESTFS_BACKEND':'direct'})
        if result.returncode:raise RuntimeError('Offline placement failed: '+result.stderr[-1800:])
    return {'disk_policy':'applied-before-boot','android':'masked','vm_role':'production-only'}

def check(bundle=BUNDLE):
 manifest=json.loads((bundle/'manifest.json').read_text());bad=[]
 for name,info in manifest['files'].items():
  p=pathlib.Path(name)
  if not p.is_file() or p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=info['sha256']:bad.append(name)
 names=['/srv/sasocq/projects/AGENTS.md']
 names += ['/home/codex/.codex/AGENTS.md','/home/codex-worker/.codex/AGENTS.md'] if os.geteuid()==0 else [str(pathlib.Path(pwd.getpwuid(os.geteuid()).pw_dir)/'.codex/AGENTS.md')]
 required=START+'\n'+(bundle/'RULES.md').read_text().rstrip()+'\n'+END
 for name in names:
  p=pathlib.Path(name)
  if not p.exists() or required not in p.read_text():bad.append(name)
 if bad:raise RuntimeError('Placement needs repair: '+', '.join(bad))
 return {'host_policy':'verified','files':len(manifest['files'])}

def main():
 p=argparse.ArgumentParser();p.add_argument('operation',choices=['host','vm','check']);p.add_argument('--root',type=pathlib.Path,default=pathlib.Path('/'));p.add_argument('--bundle',type=pathlib.Path,default=BUNDLE);a=p.parse_args()
 if a.root==pathlib.Path('/') and os.geteuid()!=0 and a.operation!='check':raise SystemExit('Use the audited System broker')
 if a.operation=='host':r=host(a.root,a.bundle)
 elif a.operation=='vm':r=apply_vm(a.bundle)
 else:r=check(a.bundle)
 print(json.dumps(r))
if __name__=='__main__':main()
