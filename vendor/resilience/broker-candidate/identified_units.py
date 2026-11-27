"""Identified systemd dispatch units. One active unit, no request/output on disk."""
from __future__ import annotations
import json,os,re,socket,struct,subprocess,time
from pathlib import Path

UNIT_RE=re.compile(r"^sasocq-broker-job-[a-f0-9]{32}\.service$")
TERMINAL={"inactive","failed"}
MAX_RESULT=2_000_000

class SystemdUnits:
    def __init__(self,root,broker_pid):
        self.root=Path(root)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=self.root.lstat()
        if self.root.is_symlink() or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError("unit_channel_not_private")
        self.broker_pid=broker_pid
        self.references=None
        self.invocations={}

    def launch(self,unit,path,nonce,limit):
        if not UNIT_RE.fullmatch(unit) or path.parent!=self.root or not re.fullmatch("[a-f0-9]{32}",nonce):
            raise ValueError("unit_identity_invalid")
        # No shell, no --wait/--pipe, no caller argv/environment. Existing
        # dispatch authorization remains inside the fixed package runner.
        argv=["systemd-run","--quiet","--unit="+unit,
              "--property=Type=exec","--property=Slice=system.slice",
              "--property=RuntimeMaxSec="+str(limit),
              "--property=TimeoutStopSec=5","--property=KillMode=control-group",
              "--property=MemoryMax=768M","--property=MemorySwapMax=0",
              "--property=TasksMax=128","--property=CPUWeight=10",
              "--property=StandardOutput=null","--property=StandardError=null",
              "/usr/bin/python3","-m","sasocq_control.broker_job",
              "--socket",str(path),"--nonce",nonce,"--broker-pid",str(self.broker_pid)]
        p=subprocess.run(argv,capture_output=True,timeout=3)
        if p.returncode:raise RuntimeError("unit_submission_uncertain")
        # Fixed worker awaits payload; inability to pin means no dispatch.
        if self.references is None:self.references=UnitReferences()
        self.references.acquire(unit)
        state=self.state(unit)
        self.invocations[unit]=state["InvocationID"]

    def release(self,unit):
        if self.references is None:raise RuntimeError("unit_reference_missing")
        self.references.release(unit)

    def release_retained(self,unit):
        # After broker restart, no reference belongs to this connection.
        if self.references is None or unit not in self.references.held:return False
        state=self.state(unit)
        if not terminal_proven(state,unit):
            raise RuntimeError("predecessor_reference_unit_not_terminal")
        if state["InvocationID"]!=self.invocations.get(unit):
            raise RuntimeError("predecessor_reference_invocation_changed")
        self.references.release(unit)
        self.invocations.pop(unit,None)
        return True

    def state(self,unit):
        if not UNIT_RE.fullmatch(unit):raise ValueError("unit_identity_invalid")
        p=subprocess.run(["systemctl","show",unit,
                          "--property=Id,LoadState,ActiveState,SubState,MainPID,ControlPID,Job,ControlGroup,Result,InvocationID",
                          "--no-pager"],capture_output=True,text=True,timeout=2)
        if p.returncode:raise RuntimeError("unit_state_uncertain")
        result={}
        for line in p.stdout.splitlines():
            k,sep,v=line.partition("=")
            if not sep or k in result:raise RuntimeError("unit_state_incomplete")
            result[k]=v
        if set(result)!={"Id","LoadState","ActiveState","SubState","MainPID","ControlPID","Job","ControlGroup","Result","InvocationID"} or result["Id"]!=unit:
            raise RuntimeError("unit_state_incomplete")
        if result["LoadState"]=="not-found":return result
        invocation=result["InvocationID"]
        if not re.fullmatch("[a-f0-9]{32}",invocation):
            raise RuntimeError("unit_invocation_missing")
        if unit in self.invocations and self.invocations[unit]!=invocation:
            raise RuntimeError("unit_invocation_changed")
        return result

    def peer(self,conn,unit):
        pid,uid,gid=struct.unpack("3i",conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid!=os.getuid():return False
        # systemd cgroup membership, not a UID-only or caller-token claim.
        try:
            state=self.state(unit)
            cgroup=Path("/proc",str(pid),"cgroup").read_text()
            return (state["ActiveState"] in {"activating","active"}
                    and state["MainPID"]==str(pid)
                    and any(line.rsplit("/",1)[-1]==unit for line in cgroup.splitlines()))
        except (OSError,ValueError,RuntimeError):return False

def worker(socket_path,nonce,broker_pid,dispatch):
    """Same broker dispatch in its cgroup; not another Android executor."""
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(5.)
        conn.connect(socket_path)
        pid,uid,gid=struct.unpack("3i",conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if pid!=broker_pid or uid!=os.getuid():
            raise PermissionError("broker_peer_changed")
        conn.sendall(json.dumps({"nonce":nonce}).encode()+b"\n")
        data=bytearray();end=time.monotonic()+5
        while b"\n" not in data:
            left=end-time.monotonic()
            if left<=0:raise TimeoutError("worker_input_deadline")
            conn.settimeout(left);part=conn.recv(65536)
            if not part:raise EOFError("worker_input_incomplete")
            data.extend(part)
            if len(data)>8_500_000:raise ValueError("worker_input_limit")
        request=json.loads(data.split(b"\n",1)[0])
        # Frontend already fsynced running, checked deadline and authenticated
        # member. No dispatch is invoked for partial input/changed peer.
        admission=request.get("admission",{})
        end=admission.get("deadline_mono")
        boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        import math
        if (type(end) not in (int,float) or not math.isfinite(end)
                or not 0 < end-time.monotonic() <= 120.
                or admission.get("boot_id")!=boot):
            conn.sendall(b'{"ok":false,"error":"worker_admission_expired"}\n')
            return
        try:answer=dispatch(request)
        except Exception as exc:
            kind=type(exc).__name__
            if kind not in {'RuntimeError','TimeoutError','TimeoutExpired','ValueError','TypeError','OSError','PermissionError','FileNotFoundError'}:kind='OtherError'
            answer={"ok":False,"error":"handler_without_final_completion","exception_type":kind}
        from .outcome_journal import compact,record
        answer=compact(answer, request=request)
        # Evidence must survive before socket delivery. Failure stays uncertain.
        record(request,answer)
        payload=json.dumps(answer,ensure_ascii=False).encode()+b"\n"
        conn.settimeout(2.)
        conn.sendall(payload)
        # No retry: loss after dispatch is reconciled by the frontend receipt.

def terminal_proven(state,unit):
    """No next dispatch while an identified unit can still produce effects."""
    from .native_cleanup import empty_cgroup
    keys={"Id","LoadState","ActiveState","SubState","MainPID","ControlPID","Job","ControlGroup","Result","InvocationID"}
    return (type(state) is dict and set(state)==keys
        and state["Id"]==unit and state["LoadState"]=="loaded"
        and state["ActiveState"] in TERMINAL and state["SubState"] in {"dead","failed"}
        and state["MainPID"]=="0" and state["ControlPID"]=="0"
        and state["Job"]=="" and empty_cgroup(state["ControlGroup"]))

class UnitReferences:
    """Broker-private retained system-bus connection; no project authority."""
    def __init__(self,bus=None):
        if bus is None:
            import dbus
            bus=dbus.SystemBus(private=True)
        self.bus=bus;self.held=set()
    def call(self,method,unit):
        if not UNIT_RE.fullmatch(unit):raise ValueError("unit_identity_invalid")
        return self.bus.call_blocking("org.freedesktop.systemd1",
            "/org/freedesktop/systemd1","org.freedesktop.systemd1.Manager",
            method,"s",(unit,),timeout=2.)
    def acquire(self,unit):
        if unit in self.held:raise RuntimeError("unit_reference_already_held")
        self.call("RefUnit",unit);self.held.add(unit)
    def release(self,unit):
        if unit not in self.held:raise RuntimeError("unit_reference_missing")
        self.call("UnrefUnit",unit);self.held.remove(unit)

def fixed_cgroup_absent(unit):
    """Only kernel cgroup absence, no age/PID/socket heuristics."""
    if not UNIT_RE.fullmatch(unit):return False
    try:
        fd=os.open("/sys/fs/cgroup/system.slice",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            try:os.stat(unit,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:return True
            return False
        finally:os.close(fd)
    except OSError:return False

def cleanup_predecessor_terminal(record,state,boot=None,absent=fixed_cgroup_absent):
    if terminal_proven(state,record["unit"]):
        return state["InvocationID"]==record.get("invocation_id")
    # Recovery-only proof. Never interpreted as successful action completion.
    if boot is None:boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    unit=record["unit"]
    if (not UNIT_RE.fullmatch(unit) or record.get("boot_id")!=boot
        or record.get("worker_cgroup")!="/system.slice/"+unit
        or not re.fullmatch("[a-f0-9]{32}",record.get("invocation_id",""))):return False
    expected={"Id":unit,"LoadState":"not-found","ActiveState":"inactive",
        "SubState":"dead","MainPID":"0","ControlPID":"0","Job":"",
        "ControlGroup":"","Result":"success","InvocationID":""}
    return state==expected and absent(unit)
