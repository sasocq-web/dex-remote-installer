"""Durable reservation state machine, not an Android runtime or shell.

Expired/crashed leases require verified cleanup. Time alone never releases them.
The future single runtime owns this store; no project-facing filesystem/API.
"""
import contextlib, fcntl, json, os, stat, time, uuid, math, re
from pathlib import Path

class AuthorityBusy(TimeoutError):
    """Existing authority lock is contended; no state was authorized."""

class Authority:
    def __init__(self, directory, clock=time.time, *, admission_check=None):
        self.directory=Path(directory)
        self.clock=clock
        self.admission_check=admission_check
        info=self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('authority_directory_not_private')

    @contextlib.contextmanager
    def transaction(self):
        fd=os.open(self.directory/'authority.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('authority_lock_not_private')
            deadline=time.monotonic()+.25
            while True:
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise AuthorityBusy('authority_transaction_busy')
                    time.sleep(.01)
            yield
        finally:os.close(fd)

    def read(self):
        path=self.directory/'lease.json'
        try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        except FileNotFoundError:return None
        with os.fdopen(fd) as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size>4096:
                raise ValueError('authority_state_not_private')
            value=json.load(stream)
        keys={'schema','generation','owner','created','expires','phase','boot_id'}
        if (type(value) is not dict or set(value)!=keys or type(value['schema']) is not int or value['schema']!=1
            or value['phase'] not in {'held','cleanup_required'}
            or type(value['owner']) is not list or len(value['owner'])!=2
            or not all(type(v) is str and 1<=len(v)<=200 for v in value['owner'])
            or type(value['expires']) not in (int,float)
            or not math.isfinite(value['expires'])
            or type(value['created']) not in (int,float) or not math.isfinite(value['created'])
            or not 1<=value['expires']-value['created']<=1500
            or value['boot_id'] is None or type(value['boot_id']) is not str
            or type(value['generation']) is not str or not re.fullmatch(r'[0-9a-f]{32}',value['generation'])):
            raise ValueError('authority_state_invalid')
        return value

    def write(self,value):
        tmp=self.directory/('lease.'+uuid.uuid4().hex+'.tmp')
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,self.directory/'lease.json')
        self.sync()

    def sync(self):
        fd=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)

    def acquire(self,owner,ttl):
        if (type(owner) is not tuple or len(owner)!=2
            or not all(type(v) is str and 1<=len(v)<=200 for v in owner)
            or type(ttl) not in (int,float) or not 1<=ttl<=1500):
            raise ValueError('authority_request_invalid')
        with self.transaction():
            if self.read() is not None:raise ValueError('authority_busy_or_cleanup_required')
            if self.admission_check is not None:
                if self.admission_check() is not True:raise ValueError('broker_lane_unavailable')
            now=self.clock()
            if not math.isfinite(now):raise ValueError('authority_clock_invalid')
            value=dict(schema=1,generation=uuid.uuid4().hex,owner=list(owner),created=now,expires=now+ttl,phase='held',boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
            self.write(value)
            return value

    def inspect(self):
        with self.transaction():
            value=self.read()
            if value and value['phase']=='held' and (not value['created']<=self.clock()<value['expires'] or value['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip()):
                value=dict(value,phase='cleanup_required');self.write(value)
            return value

    def require(self,generation):
        value=self.read()
        if not value or value['generation']!=generation:raise ValueError('authority_generation_changed')
        return value

    def begin_cleanup(self,generation):
        with self.transaction():
            value=dict(self.require(generation),phase='cleanup_required')
            self.write(value)
            return value

    def cleanup_verified(self,generation,verify_stopped):
        # Only a trusted, fixed runtime callback may supply the actual stop check.
        # No wire API accepts a caller-provided stopped flag or receipt.
        with self.transaction():
            value=self.require(generation)
            if value['phase']!='cleanup_required' or verify_stopped() is not True:
                raise ValueError('authority_cleanup_not_verified')
            (self.directory/'lease.json').unlink();self.sync()
