"""Reversible VM -> host import, executed only by codex-worker."""
import fcntl, hashlib, json, os, pathlib, re, shutil, subprocess, time

def import_vm(name,project,prefix,place,local_only=False):
    if not re.fullmatch(r'[A-Za-z0-9_. -]{1,80}',project) or project in ('.','..'):
        raise ValueError('Nome do projeto inválido')
    if not re.fullmatch(r'wine-[a-z0-9-]{1,60}',prefix): raise ValueError('Prefixo Wine inválido')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}\.rbxlx?',place): raise ValueError('Arquivo de teste inválido')
    work=pathlib.Path('/srv/sasocq/lab')/name
    if work.is_symlink(): raise ValueError('Área não pode ser symlink')
    work.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (work/'.import.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if subprocess.run(['systemctl','--user','is-active','--quiet','sasocq-lab-'+name+'.service']).returncode==0:
            raise ValueError('Encerre a área antes de importar arquivos')
        final=work/'vm-import'; staging=work/'.vm-import-staging'
        if final.exists(): raise ValueError('Importação existente preservada; use outro nome para uma nova cópia')
        if staging.is_symlink(): raise ValueError('Staging inválido')
        staging.mkdir(mode=0o700,exist_ok=True)
        tree=staging/'test-client'; tree.mkdir(mode=0o700,exist_ok=True)
        source='/srv/sasocq/projects/'+project+'/test-client'
        local_source=pathlib.Path('/srv/sasocq/development/vm-import/sasocq-server')/source.lstrip('/')
        status_path=pathlib.Path('/srv/sasocq/development/migration-state.json')
        verified=False
        if status_path.exists():
            verified=any(e.get('source')=='/srv/sasocq/projects/'+project and e.get('verify_code')==0 and e.get('differences')==0 for e in json.loads(status_path.read_text()).get('entries',[]))
        use_local=verified and local_source.is_dir()
        if local_only and not use_local: raise ValueError('Cópia local verificada ainda indisponível para este projeto')
        rsync=['/usr/bin/rsync','-a','--no-owner','--no-group','--safe-links','--protect-args',
            '--partial','--bwlimit=40000','--rsync-path=sudo -n rsync',
            '-e','/usr/bin/ssh -F /home/codex-worker/.ssh/config -o BatchMode=yes -o ConnectTimeout=10']
        # Only the executable bundle, chosen private Wine profile and chosen place
        # are copied. No login URL files, SSH/Codex credentials or unrelated homes.
        for entry in ('studio-visual-bin',prefix,place):
            if shutil.disk_usage(work).free<50*1024**3: raise ValueError('Reserva de disco insuficiente')
            if use_local:
                args=['rsync','-a','--no-owner','--no-group','--safe-links',str(local_source/entry),str(tree)+'/']
            else:args=rsync+['sasocq-server:'+source+'/'+entry,str(tree)+'/']
            p=subprocess.run(args,capture_output=True,text=True,timeout=900)
            if p.returncode: raise RuntimeError('Falha de importação: '+p.stderr[-1500:])
        executable=tree/'studio-visual-bin/RobloxStudioBeta.exe'
        if not executable.is_file(): raise ValueError('Studio não encontrado na cópia')
        # Preserve source data: the VM is read-only throughout this operation.
        # Hash the main program and place; rsync also checks transferred blocks.
        hashes={}
        for p in (executable,tree/place):
            h=hashlib.sha256()
            with p.open('rb') as stream:
                while block:=stream.read(1024*1024): h.update(block)
            hashes[str(p.relative_to(tree))]=h.hexdigest()
        drives=tree/prefix/'dosdevices'; drives.mkdir(exist_ok=True)
        z=drives/'z:'
        if not z.exists() and not z.is_symlink(): z.symlink_to('/')
        metadata={'source':source,'source_location':str(local_source) if use_local else 'sasocq-server:'+source,'prefix':prefix,'place':place,'imported_at':time.time(),'sha256':hashes,'source_preserved':True}
        (staging/'manifest.json').write_text(json.dumps(metadata,indent=2)+'\n')
        os.rename(staging,final)
        print(json.dumps({'imported':True,'path':str(final),'source_preserved':True,'sha256':hashes}))
