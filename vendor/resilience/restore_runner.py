#!/usr/bin/python3
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

BASE=Path('/home/codex/.local/share/dex-rescue')
def save(value):
    temp=BASE/'result.tmp'
    temp.write_text(json.dumps(value));os.replace(temp,BASE/'result.json')
def main():
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (BASE/'job.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        save({'result':'running','started_at':time.time()})
        prompt=Path('/usr/local/lib/dex-rescue/repair-prompt.txt').read_text()
        with (BASE/'cli.log').open('w') as log:
            try:
                p=subprocess.run(['/home/codex/.local/bin/codex','exec','--dangerously-bypass-approvals-and-sandbox','--skip-git-repo-check','--cd','/home/codex/SystemWorkspace','--output-last-message',str(BASE/'last-message.txt'),prompt],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
                code=p.returncode
            except subprocess.TimeoutExpired: code=124
        import asyncio
        from recovery_gateway import ready as service_ready
        ready = asyncio.run(service_ready())
        save({'result':'restored' if ready and code==0 else 'failed','cli_exit':code,'ready':ready,'finished_at':time.time()})
        return 0 if ready and code==0 else 1
if __name__=='__main__': raise SystemExit(main())
