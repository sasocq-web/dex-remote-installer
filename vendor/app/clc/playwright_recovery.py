"""Coordinate browser replacement with the existing system healthcheck lock."""
from __future__ import annotations
import asyncio,fcntl,json,os,re,tempfile,time,urllib.request
from pathlib import Path

def browser_id():
    with urllib.request.urlopen("http://127.0.0.1:9223/json/version",timeout=3) as response:
        row=json.loads(response.read(65536))
    value=str(row.get("webSocketDebuggerUrl",""))
    match=re.fullmatch(r"ws://127[.]0[.]0[.]1:9223/devtools/browser/([A-Za-z0-9-]+)",value)
    if not match:raise RuntimeError("Identidade CDP indisponível")
    return match[1]

async def renew_bridge(restart, *, state_dir=None, read_id=browser_id, timeout=45):
    """Same lock/id as sasocq-playwright-healthcheck; never restart twice for one id."""
    if state_dir is None:
        state_dir=Path(os.environ.get("XDG_STATE_HOME",str(Path.home()/".local/state")))/"codex-linux-control/playwright-health"
    state_dir=Path(state_dir);state_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=os.open(state_dir/"healthcheck.lock",os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    acquired=False
    try:
        deadline=time.monotonic()+timeout
        while not acquired:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);acquired=True
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError("Recuperação Playwright ocupada")
                await asyncio.sleep(0.05)
        current=await asyncio.to_thread(read_id)
        marker=state_dir/"browser-id"
        try:previous=marker.read_text().strip()
        except FileNotFoundError:previous=""
        if previous==current:return False
        await restart()
        if await asyncio.to_thread(read_id)!=current:
            raise RuntimeError("Chromium mudou durante a recuperação da ponte")
        fd,temporary=tempfile.mkstemp(prefix=".browser-id-",dir=state_dir)
        try:
            with os.fdopen(fd,"w") as stream:
                stream.write(current+"\n");stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,marker)
            directory=os.open(state_dir,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
        return True
    finally:
        if acquired:fcntl.flock(lock,fcntl.LOCK_UN)
        os.close(lock)
