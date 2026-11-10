"""Legacy-side rollback fence for the planned single Android service.

Executed before importing the Dex app. Only the fixed rootless companion unit
can be stopped. No device start, shell, lease deletion or host elevation.
"""
import subprocess
from pathlib import Path

UNIT='codex-android-runtime.service'

def run(argv,timeout):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
    return result.returncode,result.stdout

def _legacy_reconcile(app_root,config_dir,runner=run):
    app_root,config_dir=Path(app_root),Path(config_dir)
    if (app_root/'system/android-runtime-contract.json').exists():
        raise RuntimeError('external_runtime_activation_not_supported_by_legacy_guard')
    code,raw=runner(['systemctl','--user','show',UNIT,'--property=LoadState,ActiveState'],10)
    fields=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    if fields.get('LoadState')=='not-found':
        pass
    elif code or fields.get('LoadState')!='loaded':
        raise RuntimeError('runtime_unit_state_unavailable')
    elif fields.get('ActiveState') in {'active','activating','deactivating','reloading'}:
        code,_=runner(['systemctl','--user','stop',UNIT],150)
        if code:raise RuntimeError('runtime_stop_failed')
        code,raw=runner(['systemctl','--user','show',UNIT,'--property=LoadState,ActiveState'],10)
        fields=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
        if code or fields.get('ActiveState') not in {'inactive','failed'}:
            raise RuntimeError('runtime_still_active')
    elif fields.get('ActiveState') not in {'inactive','failed'}:
        raise RuntimeError('runtime_unit_state_unknown')
    # A stopped unit is not proof of a completed Android cleanup.
    lease=config_dir/'android-runtime'/'lease.json'
    if lease.exists() or lease.is_symlink():
        raise RuntimeError('runtime_lease_requires_reconciliation')
    return {'mode':'legacy','companion_stopped':True,'lease_clear':True}


def unit_definition(app_root):
    import re
    root=Path(app_root).resolve()
    if not re.fullmatch(r'/opt/codex-linux-control/releases/[A-Za-z0-9._-]{1,95}/app',str(root)):
        raise RuntimeError('runtime_source_path_invalid')
    return f"""[Unit]
Description=SASOCQ single Android authority
After=network-online.target sasocq-control-broker.socket
StartLimitIntervalSec=300
StartLimitBurst=3
[Service]
Type=exec
WorkingDirectory={root}
Environment=PYTHONPATH={root}
Environment=PYTHONUNBUFFERED=1
Environment=CLC_PACKAGE_MODE=true
Environment=XDG_RUNTIME_DIR=%t
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
Slice=codex-control.slice
ExecStart=/usr/bin/python3 -m clc.android_runtime_daemon
Restart=on-failure
RestartSec=5
TimeoutStopSec=150
KillMode=mixed
UMask=0077
MemoryHigh=256M
MemoryMax=384M
MemorySwapMax=0
TasksMax=128
[Install]
WantedBy=default.target
"""

def _read_contract(root):
    import json
    path=Path(root)/'system/android-runtime-contract.json'
    return json.loads(path.read_text()) if path.exists() else None

def _probe(config_dir):
    import asyncio
    from .android_runtime_ipc import call
    return asyncio.run(call(Path(config_dir)/'android-runtime/android-runtime.sock',{'operation':'state'}))

def reconcile(app_root,config_dir,runner=run,probe=_probe,home=None):
    import os,time
    app_root,config_dir=Path(app_root).resolve(),Path(config_dir)
    contract=_read_contract(app_root)
    if contract is None:return _legacy_reconcile(app_root,config_dir,runner)
    if contract!={'schema':1,'protocol':1}:raise RuntimeError('runtime_contract_invalid')
    definition=unit_definition(app_root)
    release=app_root.parent.name
    code,raw=runner(['systemctl','--user','show',UNIT,'--property=LoadState,ActiveState'],10)
    fields=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    loaded=fields.get('LoadState')
    if loaded not in {'loaded','not-found'} or (code and loaded!='not-found'):
        raise RuntimeError('runtime_unit_state_unavailable')
    active=fields.get('ActiveState')
    if loaded=='loaded' and active in {'active','activating','deactivating','reloading'}:
        try:state=probe(config_dir)
        except Exception:state={}
        if state.get('release_id')==release and state.get('ready') is True and state.get('protocol')==1:
            return {'mode':'external','release_id':release,'reused':True}
        code,_=runner(['systemctl','--user','stop',UNIT],160)
        if code:raise RuntimeError('runtime_stop_failed')
        code,raw=runner(['systemctl','--user','show',UNIT,'--property=LoadState,ActiveState'],10)
        fields=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
        if code or fields.get('ActiveState') not in {'inactive','failed'}:raise RuntimeError('runtime_still_active')
    elif active not in {'inactive','failed'}:raise RuntimeError('runtime_unit_state_unknown')
    lease=config_dir/'android-runtime/lease.json'
    if lease.exists() or lease.is_symlink():raise RuntimeError('runtime_lease_requires_reconciliation')
    folder=(Path.home() if home is None else Path(home))/'.config/systemd/user'
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/UNIT
    if path.is_symlink():raise RuntimeError('runtime_unit_symlink_rejected')
    if path.exists():
        backup=path.with_suffix('.service.previous')
        if backup.is_symlink():raise RuntimeError('runtime_backup_symlink_rejected')
        backup.write_bytes(path.read_bytes());backup.chmod(0o600)
    tmp=path.with_suffix('.service.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:f.write(definition);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    d=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(d)
    finally:os.close(d)
    for args in (['daemon-reload'],['enable',UNIT],['reset-failed',UNIT],['start','--no-block',UNIT]):
        code,_=runner(['systemctl','--user',*args],15)
        if code:raise RuntimeError('runtime_service_prepare_failed')
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        try:
            state=probe(config_dir)
            if state.get('protocol')==1 and state.get('release_id')==release and state.get('ready') is True and state.get('cleanup_pending') is False:
                return {'mode':'external','release_id':release,'reused':False}
        except Exception:pass
        time.sleep(.2)
    raise RuntimeError('runtime_service_not_ready')
