"""Finite frame admission and conservative durable request receipts.

SOURCE REVIEW ONLY. This does not bound the existing serial queue or handlers.
Receipts never store request bodies or command output. Missing final result after
a process restart is a reconciliation condition, never permission to execute again.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import stat
import tempfile
import time
from pathlib import Path

REQUEST_ID = re.compile(r"^[a-f0-9]{32}$|^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
GENERATION = re.compile(r"^[a-f0-9]{32}$")
READ_SECONDS = 5.
MAX_ADMISSION_SECONDS = 120.
MAX_RECEIPTS = 20000

class AdmissionError(ValueError):
    pass

def frame(conn, limit, *, clock=time.monotonic, read_seconds=READ_SECONDS):
    """One absolute read budget; trickling bytes cannot renew it."""
    end = clock() + read_seconds
    data = bytearray()
    while True:
        left = end - clock()
        if left <= 0:
            raise AdmissionError("request_read_deadline")
        conn.settimeout(left)
        try:
            chunk = conn.recv(min(65536, limit + 1 - len(data)))
        except socket.timeout:
            raise AdmissionError("request_read_deadline") from None
        if not chunk:
            raise AdmissionError("request_frame_incomplete")
        data.extend(chunk)
        if len(data) > limit:
            raise AdmissionError("request_too_large")
        if b"\n" in data:
            first, trailing = bytes(data).split(b"\n", 1)
            if trailing.strip():
                raise AdmissionError("multiple_request_frames")
            value = json.loads(first.decode("utf-8"))
            if not isinstance(value, dict):
                raise AdmissionError("request_must_be_object")
            return value

def completion(response, *, request=None):
    """Only consistent ordinary exits, with no nested uncertainty markers."""
    if type(response) is not dict or type(response.get("ok")) is not bool:return False
    result=response.get("result")
    if type(result) is not dict:return False
    output=result.get("output")
    # The VM resource report describes the VM's state, independently of
    # whether the broker request itself has finished.
    params = request.get("params") if type(request) is dict else None
    vm_resource_report = (
        type(params) is dict and request.get("action") == "vm"
        and params.get("operation") == "resource-status"
        and params.get("name", "sasocq-server") == "sasocq-server"
        and type(output) is dict and output.get("name") == "sasocq-server"
        and type(output.get("available")) is bool
        and type(output.get("current")) is dict and type(output.get("recommended")) is dict
    )
    parts=[response,result]+([output] if type(output) is dict else [])
    for part in parts:
        if (part.get("pending") is True or part.get("timed_out") is True
            or part.get("execution_uncertain") is True
            or (isinstance(part.get("state"), str)
                and part.get("state") in {"queued","pending","running","timeout","timed_out","interrupted"}
                and not (part is output and vm_resource_report and part["state"] == "running"))):
            return False
    codes=[v["returncode"] for v in [result,output] if type(v) is dict and "returncode" in v]
    return (bool(codes) and "returncode" in result
        and all(type(rc) is int and 0<=rc<124 for rc in codes)
        and len(set(codes))==1 and response["ok"] is (codes[0]==0))

def atomic(path, value, *, no_replace=False):
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if no_replace:
            os.link(name, path)
        else:
            os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)

class Receipts:
    """Single serial broker, no new locks or authority. Owner UID binds access."""
    def __init__(self, directory, *, boot, clock=time.monotonic, wall=time.time):
        self.root = Path(directory)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not self.root.is_dir() or self.root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AdmissionError("receipt_directory_not_private")
        key = self.root / "fingerprint.key"
        if not key.exists():
            try:
                atomic(key, {"key": secrets.token_hex(32)}, no_replace=True)
            except FileExistsError:
                pass
        info = key.lstat()
        if key.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AdmissionError("receipt_key_not_private")
        self.key = bytes.fromhex(json.loads(key.read_text())["key"])
        if len(self.key) != 32:
            raise AdmissionError("receipt_key_invalid")
        self.boot, self.clock, self.wall = boot, clock, wall
        self.initial_wall, self.initial_mono = wall(), clock()
        self.cache = {}

    def clock_valid(self):
        # No authority or lease is created/renewed by this clock check.
        return abs((self.wall() - self.initial_wall) - (self.clock() - self.initial_mono)) <= 1.

    def path(self, uid, rid):
        if type(uid) is not int or uid < 0 or not REQUEST_ID.fullmatch(rid):
            raise AdmissionError("request_identity_invalid")
        name = str(uid) + "-" + rid + ".json"
        primary = self.root / name
        archive = self.root / "terminal-archive" / hashlib.sha256(name.encode()).hexdigest()[:2]
        if primary.exists() or primary.is_symlink():
            return primary
        for directory in (self.root / "terminal-archive", archive):
            if directory.exists() or directory.is_symlink():
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise AdmissionError("receipt_archive_not_private")
        historical = archive / name
        if historical.exists() or historical.is_symlink():
            return historical
        return primary

    def digest(self, uid, request):
        raw = json.dumps({"uid":uid, "request":request}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return hmac.new(self.key, raw, hashlib.sha256).hexdigest()

    def validate(self, request):
        scope = request.get("admission")
        if type(scope) is not dict or set(scope) != {"schema","boot_id","generation","deadline_mono"}:
            raise AdmissionError("admission_schema_invalid")
        if type(scope["schema"]) is not int or scope["schema"] != 1:
            raise AdmissionError("admission_schema_invalid")
        if scope["boot_id"] != self.boot or not GENERATION.fullmatch(str(scope["generation"])):
            raise AdmissionError("admission_scope_invalid")
        end = scope["deadline_mono"]
        if type(end) not in (float,int) or not math.isfinite(end):
            raise AdmissionError("admission_deadline_invalid")
        if not self.clock_valid():
            raise AdmissionError("admission_clock_discontinuity")
        remaining = end - self.clock()
        if not 0 < remaining <= MAX_ADMISSION_SECONDS:
            raise AdmissionError("admission_expired_or_out_of_range")
        return scope

    def lookup(self, uid, rid, generation):
        path = self.path(uid, rid)
        if not path.exists():
            return {"ok": False, "error": "receipt_absent"}
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AdmissionError("receipt_not_private")
        record = json.loads(path.read_text())
        if record["uid"] != uid or record["generation"] != generation:
            raise AdmissionError("receipt_scope_mismatch")
        return {"ok": True, "receipt": record}

    def require_writable_capacity(self):
        # Historical identities are not active work. Preserve their lookup and
        # retention; refuse only actual lack of room for durable new records.
        info=os.statvfs(self.root)
        if info.f_bavail*info.f_frsize < 4*1024*1024 or info.f_favail < 32:
            raise AdmissionError("receipt_storage_capacity")

    def execute(self, uid, request, dispatch):
        # Admission is not authorization. Existing peer checks and dispatch
        # policies remain mandatory; generation is an idempotency scope only.
        rid = request.get("request_id")
        if type(rid) is not str:
            raise AdmissionError("request_identity_invalid")
        path = self.path(uid, rid)
        scope = request.get("admission")
        if type(scope) is not dict:
            raise AdmissionError("admission_schema_invalid")
        fingerprint = self.digest(uid, request)
        if path.exists():
            result = self.lookup(uid, rid, scope.get("generation"))
            record = result["receipt"]
            if not hmac.compare_digest(record["fingerprint"], fingerprint):
                raise AdmissionError("request_id_content_conflict")
            cached = self.cache.get((uid,rid))
            if cached is not None:
                return cached
            # Never re-dispatch a running/uncertain/terminal record, even after
            # its original deadline, boot change, or broker process restart.
            return {"ok": False, "error":"request_reconciliation_required", "receipt":record}
        self.require_writable_capacity()
        try:
            self.validate(request)
        except AdmissionError as exc:
            # Record expired, otherwise well-formed envelopes before refusal.
            # There was no dispatch; a later retry still cannot renew this ID.
            end = scope.get("deadline_mono")
            well_formed = (set(scope) == {"schema","boot_id","generation","deadline_mono"}
                           and type(scope["schema"]) is int and scope["schema"] == 1
                           and scope["boot_id"] == self.boot
                           and GENERATION.fullmatch(str(scope["generation"]))
                           and type(end) in (int,float) and math.isfinite(end))
            if well_formed and end <= self.clock():
                refused = {"schema":1,"uid":uid,"request_id":rid,
                           "generation":scope["generation"],"boot_id":self.boot,
                           "fingerprint":fingerprint,"deadline_mono":end,
                           "state":"refused","reason":"expired_before_dispatch"}
                atomic(path, refused, no_replace=True)
                return {"ok":False,"error":"expired_before_dispatch","receipt":refused}
            raise
        record = {
            "schema":1, "uid":uid, "request_id":rid,
            "generation":scope["generation"], "boot_id":self.boot,
            "fingerprint":fingerprint, "deadline_mono":scope["deadline_mono"],
            "state":"admitted", "admitted_mono":self.clock(),
        }
        atomic(path, record, no_replace=True)
        # Refuse queue/fsync overruns before running the command.
        try:
            self.validate(request)
        except AdmissionError:
            record.update(state="refused", reason="admission_lost_before_dispatch")
            atomic(path, record)
            return {"ok":False, "error":"admission_lost_before_dispatch", "receipt":record}
        record.update(state="running", started_mono=self.clock())
        atomic(path, record)
        # Recheck after this second fsync too. No timeout cancels a handler.
        try:
            self.validate(request)
        except AdmissionError:
            record.update(state="refused", reason="admission_lost_before_dispatch")
            atomic(path, record)
            return {"ok":False, "error":"admission_lost_before_dispatch", "receipt":record}
        try:
            response = dispatch(request)
        except BaseException:
            record.update(state="uncertain", reason="handler_without_final_completion", finished_mono=self.clock())
            atomic(path, record)
            raise
        completed = completion(response, request=request)
        record.update(state="completed" if completed else "uncertain", finished_mono=self.clock())
        if completed:
            record.update(ok=response["ok"], returncode=response["result"]["returncode"])
        atomic(path, record) # persistence failure must not yield successful ack
        answer = dict(response, receipt=record)
        if record["state"] != "completed":
            return {"ok":False, "error":"request_reconciliation_required", "receipt":record}
        self.cache[(uid,rid)] = answer
        while len(self.cache) > 8:
            del self.cache[next(iter(self.cache))]
        return answer

    def compact_terminal(self, *, excluded=(), target=10000, threshold=None, max_moves=32, seconds=0.1):
        """Single-writer maintenance; only exact durable terminal proofs qualify.

        Original request identity and receipt bytes survive link/fsync/unlink.
        No active/checkpoint/uncertain state is modified or renewed.
        """
        excluded = {Path(p).name for p in excluded}
        files = sorted(self.root.glob("*.json"))
        threshold = MAX_RECEIPTS if threshold is None else threshold
        if len(files) < threshold:
            return 0
        if not 1 <= max_moves <= 128 or not 0 < seconds <= 0.5:
            raise AdmissionError("receipt_maintenance_budget_invalid")
        removed = 0
        deadline = self.clock() + seconds
        for source in files:
            if removed >= max_moves or self.clock() >= deadline:
                break
            if len(files) - removed <= target:
                break
            if source.name in excluded or not REQUEST_ID.fullmatch(source.stem.split("-", 1)[-1]):
                continue
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AdmissionError("receipt_not_private")
            raw = source.read_bytes()
            record = json.loads(raw)
            if (record.get("schema") != 2 or record.get("state") != "completed"
                or any(k in record for k in ("cleanup_checkpoint", "cleanup_of", "rollout_pointer_target"))):
                continue
            fingerprint = record.get("fingerprint", "")
            if not re.fullmatch("[a-f0-9]{64}", fingerprint):
                continue
            proofpath = self.root / "terminal-proofs" / (fingerprint + ".json")
            try:
                proofinfo = proofpath.lstat()
                if not stat.S_ISREG(proofinfo.st_mode) or proofinfo.st_uid != os.getuid() or proofinfo.st_mode & 0o077:
                    raise AdmissionError("receipt_terminal_proof_not_private")
                proof = json.loads(proofpath.read_bytes())
            except FileNotFoundError:
                continue
            fields = ("uid", "request_id", "generation", "fingerprint", "boot_id", "unit")
            state = proof.get("unit_state", {})
            if (proof.get("state") != "terminal_observed"
                or any(proof.get(k) != record.get(k) for k in fields)
                or state.get("InvocationID") != record.get("invocation_id")
                or state.get("Id") != record.get("unit")
                or state.get("ActiveState") not in ("inactive", "failed")
                or state.get("SubState") not in ("dead", "failed")
                or state.get("MainPID") != "0" or state.get("ControlPID") != "0"
                or state.get("Job") != ""):
                continue
            expected = str(record["uid"]) + "-" + record["request_id"] + ".json"
            if source.name != expected:
                raise AdmissionError("receipt_archive_identity_mismatch")
            base = self.root / "terminal-archive"
            shard = base / hashlib.sha256(source.name.encode()).hexdigest()[:2]
            for directory in (base, shard):
                directory.mkdir(mode=0o700, exist_ok=True)
                st = directory.lstat()
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                    raise AdmissionError("receipt_archive_not_private")
                self._sync_directory(directory.parent)
            destination = shard / source.name
            try:
                os.link(source, destination, follow_symlinks=False)
            except FileExistsError:
                ds = destination.lstat()
                if not stat.S_ISREG(ds.st_mode) or (ds.st_dev, ds.st_ino) != (info.st_dev, info.st_ino):
                    raise AdmissionError("receipt_archive_conflict")
            self._sync_directory(shard)
            current = source.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or source.read_bytes() != raw:
                raise AdmissionError("receipt_changed_during_archive")
            source.unlink()
            self._sync_directory(self.root)
            removed += 1
        return removed

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

"""Shared pure operation catalogue. No handler exception is assumed effect-free."""
CATALOGUE = {
 'host-admin': ('status', 'status exec read-file write-file projects-storage-review projects-storage-transaction'),
 'service': ('status', 'status start stop restart reload enable disable'),
 'vm': ('status', 'status start shutdown reboot destroy resource-status reconcile-resources'),
 'session': ('status', 'status terminate switch-next-boot'),
 'gaming': ('status', 'status start restart request stop release sync-library open-store'),
 'workers': ('status', 'status register unregister freeze pause thaw resume terminate stop priority limits apply'),
 'packages': ('install', 'install remove'),
 'resources': ('status', 'status apply set-mode'),
 'backup': ('status', 'status configure run disable snapshots restore-status restore validate-restore'),
 'recovery': ('status', 'status prepare repair reapply factory-reset export'),
 'game-storage': ('status', 'status ensure adopt prepare'),
 'emulation': ('status', 'status scan install'),
 'physical': ('status', 'status start activate stop'),
 'server': ('status', 'status exec read-file write-file service deploy'),
 'authd': ('status', 'status prepare configure'),
 'publication': ('status', 'status install configure'),
 'watchdog': ('status', 'status install check self-test maintenance safe-mode clear-safe-mode clear-quarantine reboot-test'),
 'system': ('overview', 'overview hardware power-policy install-power-policy update reboot poweroff'),
}
CATALOGUE['steam']=CATALOGUE['gaming']
SPECIAL=frozenset({'broker.reconcile_readonly','ping','provision.status','provision.run','provision.complete',
 'broker.incident','broker.receipt','broker.capacity','broker.diagnostic','broker.runtime_availability'})
HOST_OPERATIONS=frozenset(CATALOGUE['host-admin'][1].split())

def rejection(request):
    code=None; supported=[]
    if type(request) is not dict:code='request_invalid'
    else:
        action=request.get('action');params=request.get('params',{})
        if type(action) is not str:code='action_invalid'
        elif type(params) is not dict:code='params_invalid'
        elif action in SPECIAL:return None
        elif action not in CATALOGUE:code='action_unsupported'
        else:
            default,options=CATALOGUE[action];supported=options.split()
            op=params.get('operation',default)
            if type(op) is not str or op not in supported:code='operation_unsupported'
            elif 'confirm' in params and type(params['confirm']) is not bool:code='confirm_invalid'
            elif action=='host-admin' and op=='projects-storage-transaction':
                if (set(params)!={'operation','transaction_id','plan_sha256'}
                    or type(params.get('transaction_id')) is not str
                    or re.fullmatch('[a-f0-9]{32}',params['transaction_id']) is None
                    or type(params.get('plan_sha256')) is not str
                    or re.fullmatch('[a-f0-9]{64}',params['plan_sha256']) is None):
                    code='storage_long_scope_invalid'
            elif action=='host-admin' and op=='projects-storage-review':
                if (set(params)!={'operation','transaction_id'}
                    or type(params.get('transaction_id')) is not str
                    or re.fullmatch('[a-f0-9]{32}',params['transaction_id']) is None):
                    code='storage_review_scope_invalid'
            elif action=='host-admin' and op=='exec':
                a=params.get('argv')
                if type(a) is not list or not 1<=len(a)<=128 or any(type(v) is not str or not v or len(v)>8192 or '\x00' in v or '\n' in v for v in a):code='argv_invalid'
                elif 'timeout' in params and (type(params['timeout']) is not int or not 1<=params['timeout']<=7200):code='timeout_invalid'
                elif 'environment' in params and params['environment'] is not None and type(params['environment']) is not dict:code='environment_invalid'
                elif 'input_text' in params and params['input_text'] is not None and type(params['input_text']) is not str:code='input_invalid'
    if code is None:return None
    return {'ok':False,'result':{'returncode':2,'output':{'code':code,'effects_started':False,'supported_operations':supported}}}
