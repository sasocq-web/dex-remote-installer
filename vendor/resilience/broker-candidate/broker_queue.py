"""Finite admission queue and one identified dispatch unit; no Android authority."""
from __future__ import annotations
import contextlib,json,os,secrets,time,uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from .bounded_protocol import AdmissionError,atomic,completion
from .identified_units import TERMINAL,terminal_proven,cleanup_predecessor_terminal
from .native_budget import require_request_budget
MAX_QUEUED=16
MAX_PAYLOAD_BYTES=16_000_000

def current_boot():return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

@dataclass
class Work:
    uid:int
    request:dict
    record:dict
    path:Path
    size:int
    response:dict|None=None
    channel_done:bool=False
    payload_allowed:bool=False

class Queue:
    def __init__(self,store,units,*,clock=time.monotonic,admission_guard=None):
        self.store,self.units,self.clock=store,units,clock
        self.admission_guard=admission_guard
        self.waiting=deque();self.active=None;self.hold=False;self.held_reason=""
        self.payload_bytes=0;self.answers={}
        self.checkpoint=store.root/"active-dispatch.json"
        # An interrupted broker never resumes admitted/running/uncertain work.
        # No age/boot/timeout permits clearing the global uncertainty fence.
        from .activation_gate import settled_administrative_paths
        self.admin_recheck=0.
        settled=settled_administrative_paths(store.root,units,store.boot)
        from .outcome_recovery import known_paths
        settled |= known_paths(store.root,units,store.boot)
        for path in store.root.glob("*.json"):
            if path.name in settled:continue
            r=json.loads(path.read_text())
            if r.get("state") not in {"completed","refused"}:
                self.hold=True;self.held_reason="startup_reconciliation_required"
            predecessor=r.get("cleanup_checkpoint")
            before_payload=prepayload_abort(r,self.checkpoint,self.units)
            if (r.get("state") in {"queued","starting"} and type(predecessor) is dict
                and self.checkpoint.exists()
                and (json.loads(self.checkpoint.read_text())==predecessor or before_payload)):
                # Launch cannot precede atomic checkpoint replacement.
                # Append a disposition; never rewrite or resume old receipt.
                decisions=store.root/"receipt-decisions"
                decisions.mkdir(mode=0o700,exist_ok=True)
                fd=os.open(store.root,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
                decision={"state":"not_dispatched","request_id":r["request_id"],
                    "uid":r["uid"],"generation":r["generation"],"fingerprint":r["fingerprint"],
                    "predecessor_checkpoint":predecessor,"reason":("broker_restart_before_cleanup_payload" if before_payload else "broker_restart_before_cleanup_handoff")}
                target=decisions/(r["fingerprint"]+".json")
                if target.exists():
                    if json.loads(target.read_text())!=decision:
                        raise AdmissionError("cleanup_restart_disposition_conflict")
                else:atomic(target,decision,no_replace=True)


    def validate(self,request):
        self.store.validate(request)
        require_request_budget(request,self.clock())
        if self.admission_guard is not None:self.admission_guard(request)

    @staticmethod
    def refusal_reason(exc,fallback):
        reason=str(exc)
        return reason if reason.startswith(("android_authority_","maintenance_excluded_","native_")) else fallback

    def incident_status(self,uid):
        records=[]
        cp=self.store.root/'active-dispatch.json'
        if cp.exists():
            if cp.is_symlink() or cp.stat().st_size>65536:raise ValueError('incident_checkpoint_invalid')
            head=json.loads(cp.read_bytes())
            if head.get('uid')==uid:
                path=self.store.path(uid,head['request_id'])
                if path.is_symlink() or path.stat().st_size>65536:raise ValueError('incident_receipt_invalid')
                row=json.loads(path.read_bytes())
                if any(row.get(k)!=v for k,v in head.items()):raise ValueError('incident_scope_changed')
                keys=('request_id','generation','unit','state','first_reason','reason','last_reason')
                summary={k:row[k] for k in keys if k in row}
                proofpath=self.store.root/'worker-outcomes'/(row['fingerprint']+'.json')
                summary['durable_outcome_present']=proofpath.is_file()
                records.append(summary)
        return {'reconciliation_required':self.hold,'records':records,
            'bounded':True,'read_only':True,'automatic_replay':False}

    def diagnostic_status(self,uid):
        """Read-only, bounded incident evidence; no command or caller path."""
        import stat
        from .outcome_journal import FIELDS
        def private(path):
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                before=os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid()
                    or before.st_nlink!=1 or before.st_mode&0o077 or before.st_size>65536):
                    raise ValueError('diagnostic_evidence_untrusted')
                raw=os.read(fd,65537);after=os.fstat(fd);linked=path.lstat()
                if len(raw)>65536 or any(getattr(before,k)!=getattr(after,k) or getattr(before,k)!=getattr(linked,k)
                    for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):
                    raise ValueError('diagnostic_evidence_changed')
                return json.loads(raw)
            finally:os.close(fd)
        result={'schema':1,'read_only':True,'automatic_replay':False,
            'reconciliation_required':self.hold,'waiting':len(self.waiting),
            'active':self.active is not None,'records':[]}
        try:
            cp=private(self.checkpoint)
            if cp.get('uid')!=uid:return result
            row=private(self.store.path(uid,cp['request_id']))
            if any(row.get(k)!=v for k,v in cp.items()):raise ValueError('diagnostic_scope_changed')
            import re
            fp=row.get('fingerprint')
            if not isinstance(fp,str) or not re.fullmatch('[a-f0-9]{64}',fp):raise ValueError('diagnostic_scope_invalid')
            record={k:row.get(k) for k in ('request_id','generation','unit','state','first_reason','last_reason')}
            for kind in ('worker-outcomes','worker-children'):
                directory=self.store.root/kind
                try:
                    st=directory.lstat()
                    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077:
                        raise ValueError('diagnostic_directory_untrusted')
                    proof=private(directory/(fp+'.json'))
                except FileNotFoundError:record[kind]={'present':False};continue
                if any(proof.get(k)!=row.get(k) for k in FIELDS):raise ValueError('diagnostic_scope_changed')
                allowed=('schema','validated_completion','reason','exception_type','ok','returncode',
                         'command_returncode','command_lifecycle','command_unit','child_unit')
                record[kind]=dict(present=True,**{k:proof[k] for k in allowed if k in proof})
            result['records']=[record]
        except FileNotFoundError:result['evidence_status']='absent'
        except (OSError,ValueError,KeyError,TypeError):result['evidence_status']='unavailable_or_changed'
        return result


    def readonly_closure(self, uid, rid):
        """Verify the exact receipt and independent closure without dispatching."""
        import hashlib
        from .activation_gate import private_read
        self.store.path(uid, rid)  # validate the caller-scoped identity
        name = f"{uid}-{rid}.json"
        root = self.store.root
        primary = root / name
        archive = root / "terminal-archive" / hashlib.sha256(name.encode()).hexdigest()[:2] / name
        fingerprint = self.store.digest(uid, {"action":"provision.status","params":{},"request_id":rid})
        raw = private_read(archive)
        row = json.loads(raw)
        decision = json.loads(private_read(root / "previous-boot-decisions" / (fingerprint + ".json")))
        expected = {"reason":"readonly_interrupted_no_replay","automatic_replay":False,
                    "request_id":rid,"boot_id":row["boot_id"],"receipt_sha256":hashlib.sha256(raw).hexdigest()}
        if (decision != expected or decision["automatic_replay"] is not False
            or row["schema"] != 2 or row["state"] != "uncertain" or row["boot_id"] == self.store.boot
            or row["uid"] != uid or row["request_id"] != rid or row["generation"] != uuid.UUID(rid).hex
            or row["fingerprint"] != fingerprint):
            raise AdmissionError("readonly_closure_mismatch")
        try:
            duplicate = private_read(primary)
        except FileNotFoundError:
            duplicate = None
        if duplicate is not None and duplicate != raw:
            raise AdmissionError("readonly_primary_conflict")
        return primary, duplicate is not None, decision

    def diagnostic_with_closures(self, uid):
        from .activation_gate import private_read
        result = self.diagnostic_status(uid)
        if not self.hold:
            return result
        directory = self.store.root / "previous-boot-decisions"
        try:
            with os.scandir(directory) as entries:
                for count, entry in enumerate(entries):
                    if count >= 256:
                        result["closure_scan_truncated"] = True
                        break
                    if not entry.name.endswith(".json"):
                        continue
                    try:
                        decision = json.loads(private_read(Path(entry.path)))
                        if decision.get("reason") != "readonly_interrupted_no_replay":
                            continue
                        rid = decision["request_id"]
                        primary, present, proof = self.readonly_closure(uid, rid)
                        if present:
                            result["records"].append({"request_id":rid,"operation":"provision.status",
                                "state":"uncertain","closure_decision":proof,"archive_matches_primary":True,
                                "available_operation":{"action":"broker.reconcile_readonly","params":{"request_id":rid}}})
                            result["evidence_status"] = "verified"
                    except (OSError, ValueError, TypeError, KeyError, AdmissionError):
                        continue
        except FileNotFoundError:
            pass
        return result

    def reconcile_readonly(self, uid, rid):
        if self.active is not None or self.waiting or self.checkpoint.exists() or self.checkpoint.is_symlink():
            raise AdmissionError("dispatch_not_idle")
        primary, present, decision = self.readonly_closure(uid, rid)
        if present:
            primary.unlink()
        fd = os.open(self.store.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if self.hold and self.held_reason in {"startup_reconciliation_required","administrative_successor_other_reconciliation_required"}:
            # Only clear a startup scan hold after checking every other primary.
            for other in self.store.root.glob("*.json"):
                if json.loads(other.read_bytes()).get("state") not in {"completed","refused"}:
                    break
            else:
                self.hold = False
                self.held_reason = ""
        return {"decision":decision,"archived":True,"reconciliation_required":self.hold}

    def answer(self,w):
        return self.answers.get((w.uid,w.record["request_id"]))

    def persist(self,w,**fields):
        r=dict(w.record,**fields)
        atomic(w.path,r);w.record=r

    def refuse(self,w,reason,launch_invocation=None):
        import re
        evidence={}
        if ("invocation_id" not in w.record and isinstance(launch_invocation,str)
            and re.fullmatch("[a-f0-9]{32}",launch_invocation)):
            evidence["invocation_id"]=launch_invocation
        self.persist(w,state="refused",reason=reason,finished_mono=self.clock(),**evidence)
        self.answers[(w.uid,w.record["request_id"])]={
            "ok":False,"error":reason,"receipt":w.record}

    def uncertain(self,w,reason):
        self.hold=True;self.held_reason=reason
        first=w.record.get("first_reason",w.record.get("reason",reason))
        self.persist(w,state="uncertain",reason=first,first_reason=first,last_reason=reason)
        self.answers[(w.uid,w.record["request_id"])]={
            "ok":False,"error":"request_reconciliation_required","receipt":w.record}

    def submit(self,uid,original):
        rid=original.get("request_id")
        if not isinstance(rid,str):raise AdmissionError("request_identity_invalid")
        path=self.store.path(uid,rid)
        digest=self.store.digest(uid,original)
        if path.exists():
            r=json.loads(path.read_text())
            if r["uid"]!=uid or r["fingerprint"]!=digest:
                raise AdmissionError("request_id_content_conflict")
            answer=self.answers.get((uid,rid))
            return None,answer or {"ok":False,"error":"request_reconciliation_required","receipt":r}
        from .bounded_protocol import rejection
        rejected=rejection(original)
        if rejected is not None:return None,rejected
        request=dict(original)
        if "admission" not in request:
            # Legacy caller keeps final response semantics. A private rollback
            # has an additional immutable process-age/deadline binding.
            request["admission"]={"schema":1,"boot_id":self.store.boot,
                "generation":uuid.UUID(rid).hex,"deadline_mono":self.clock()+120.}
        cleanup_of=None;rollout_predecessor=None
        if self.hold:
            if "_rollout_binding" in request:
                self.validate(request)
                rollout_predecessor=self.rollout_administrative_admission(uid,request)
            else:cleanup_of=self.cleanup_admission(uid,request)
        if self.active is None and not self.waiting and not self.hold:
            self.validate(request)
            self.store.compact_terminal(threshold=12000, max_moves=32, seconds=0.1)
        if len(self.waiting)>=MAX_QUEUED:
            raise AdmissionError("queue_capacity")
        self.store.require_writable_capacity()
        scope=request["admission"]
        self.validate(request)
        size=len(json.dumps(original).encode())
        if self.payload_bytes+size>MAX_PAYLOAD_BYTES:raise AdmissionError("queue_payload_capacity")
        r={"schema":2,"uid":uid,"request_id":rid,"fingerprint":digest,
           "generation":scope["generation"],"boot_id":scope["boot_id"],
           "deadline_mono":scope["deadline_mono"],"state":"queued",
           "unit":"sasocq-broker-job-"+digest[:32]+".service"}
        if "_rollout_binding" in request:
            r["rollout_pointer_target"]=request["params"]["argv"][2]
            r["rollout_gate_sha256"]=request["_rollout_binding"]["gate_sha256"]
        if rollout_predecessor is not None:r["rollout_administrative_checkpoint"]=rollout_predecessor
        if cleanup_of is not None:
            r["cleanup_of"]=cleanup_of["request_id"]
            r["cleanup_checkpoint"]=cleanup_of
        atomic(path,r,no_replace=True)
        if cleanup_of is not None:self.active=None
        w=Work(uid,request,r,path,size)
        self.waiting.append(w);self.payload_bytes+=size
        return w,None


    def cleanup_admission(self,uid,request):
        from .native_cleanup import selected
        # No caller flag or ordinary UID can bypass authority validation.
        if not selected(request) or self.admission_guard is None:
            raise AdmissionError("global_reconciliation_required")
        self.validate(request)
        if self.waiting:raise AdmissionError("native_cleanup_waiters_require_reconciliation")
        try:
            checkpoint=json.loads(self.checkpoint.read_text())
            oldpath=self.store.path(checkpoint["uid"],checkpoint["request_id"])
            old=json.loads(oldpath.read_text())
            keys=("uid","request_id","unit","fingerprint","generation","boot_id")
            if any(old[k]!=checkpoint[k] for k in keys):raise ValueError()
            scope=request["admission"]
            if (uid!=old["uid"] or scope["generation"]!=old["generation"]
                or scope["boot_id"]!=old["boot_id"] or request["request_id"]==old["request_id"]):
                raise ValueError()
            state=self.units.state(old["unit"])
            if not self.predecessor_terminal(old,state):raise ValueError()
            if self.active is not None and self.active.record["request_id"]!=old["request_id"]:
                raise ValueError()
            # Preserve old receipt and checkpoint bytes in immutable history.
            history=self.store.root/"cleanup-history"
            history.mkdir(mode=0o700,exist_ok=True)
            parent=os.open(self.store.root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent)
            finally:os.close(parent)
            archived=dict(checkpoint,unit_state=state,state="cleanup_predecessor_terminal")
            archivepath=history/(old["fingerprint"]+".json")
            if archivepath.exists():
                prior=json.loads(archivepath.read_text())
                # The immutable observation may be loaded while the current
                # same-invocation service was garbage-collected after Unref.
                # Preserve it; independently prove both scope and termination.
                if (type(prior) is not dict or set(prior)!=set(archived)
                    or any(prior[k]!=checkpoint[k] for k in keys)
                    or prior["state"]!="cleanup_predecessor_terminal"
                    or not self.predecessor_terminal(old,prior["unit_state"])):
                    raise ValueError()
            else:atomic(archivepath,archived,no_replace=True)
            self.validate(request)
            if json.loads(self.checkpoint.read_text())!=checkpoint:raise ValueError()
            # Keep predecessor checkpoint until successor receipt is durable.
            return checkpoint
        except (OSError,ValueError,KeyError,TypeError,RuntimeError):
            raise AdmissionError("native_cleanup_predecessor_not_proven") from None

    def predecessor_terminal(self,record,state):
        if cleanup_predecessor_terminal(record,state):return True
        # Only a NEW durable refusal emitted before payload release can
        # recover a no-invocation predispatch failure. No old receipt backfill.
        from .identified_units import terminal_proven,fixed_cgroup_absent,UNIT_RE
        import re
        try:
            proof=json.loads((self.store.root/"pre-payload-refusals"/(record["fingerprint"]+".json")).read_text())
            keys=("uid","request_id","unit","fingerprint","generation","boot_id")
            if (set(proof)!=set(keys)|{"state","launch_invocation_id"}
                or any(proof[k]!=record[k] for k in keys)
                or proof["state"]!="payload_not_released"
                or record["boot_id"]!=current_boot()):
                return False
            invocation=proof["launch_invocation_id"]
            if terminal_proven(state,record["unit"]):
                return isinstance(invocation,str) and re.fullmatch("[a-f0-9]{32}",invocation) is not None and state["InvocationID"]==invocation
            expected={"Id":record["unit"],"LoadState":"not-found","ActiveState":"inactive",
                "SubState":"dead","MainPID":"0","ControlPID":"0","Job":"",
                "ControlGroup":"","Result":"success","InvocationID":""}
            return UNIT_RE.fullmatch(record["unit"]) is not None and state==expected and fixed_cgroup_absent(record["unit"])
        except (OSError,ValueError,KeyError,TypeError,RuntimeError):return False

    def pre_payload_refused(self,w):
        if w.payload_allowed:raise AdmissionError("payload_already_released")
        proofdir=self.store.root/"pre-payload-refusals"
        proofdir.mkdir(mode=0o700,exist_ok=True)
        fd=os.open(self.store.root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        keys=("uid","request_id","unit","fingerprint","generation","boot_id")
        proof={k:w.record[k] for k in keys}
        proof.update(state="payload_not_released",
            launch_invocation_id=getattr(self.units,"invocations",{}).get(w.record["unit"]))
        if w.request.get("action")=="host-admin" and w.request.get("params",{}).get("operation")=="projects-storage-transaction":
            import hashlib
            scope={k:w.request[k] for k in ("request_id","params","admission")}
            proof["storage_long_scope_sha256"]=hashlib.sha256(json.dumps(scope,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        target=proofdir/(w.record["fingerprint"]+".json")
        if target.exists():
            if json.loads(target.read_text())!=proof:raise AdmissionError("payload_refusal_proof_conflict")
        else:atomic(target,proof,no_replace=True)
        return proof["launch_invocation_id"]

    def expire_waiters(self):
        remaining=deque()
        while self.waiting:
            w=self.waiting.popleft()
            try:self.validate(w.request)
            except AdmissionError as exc:
                self.refuse(w,self.refusal_reason(exc,"queue_admission_expired"));self.payload_bytes-=w.size
            else:remaining.append(w)
        self.waiting=remaining

    def administrative_paths(self,request=None):
        from .activation_gate import settled_administrative_paths
        if request is not None and "_rollout_binding" in request:
            # Only the private kernel-bound rollback may see a final admin
            # settlement through the retained package gate. No other hold is
            # exempted, no final action callback or receipt is fabricated.
            if self.admission_guard is None or not hasattr(self.admission_guard,"check_rollout"):
                raise AdmissionError("rollout_private_guard_required")
            self.admission_guard.check_rollout(request)
            return settled_administrative_paths(self.store.root,self.units,self.store.boot,gate_check=lambda:False)
        from .outcome_recovery import known_paths
        return settled_administrative_paths(self.store.root,self.units,self.store.boot) | known_paths(self.store.root,self.units,self.store.boot)

    def rollout_administrative_admission(self,uid,request,ignore=None):
        if type(uid) is not int or uid!=1000 or self.active is not None or self.waiting:
            raise AdmissionError("rollout_queue_not_quiescent")
        settled=self.administrative_paths(request)
        if "active-dispatch.json" not in settled:
            raise AdmissionError("rollout_administrative_predecessor_unsettled")
        for path in self.store.root.glob("*.json"):
            if path.name in settled or path==ignore:continue
            value=json.loads(path.read_text())
            if value.get("state") not in {"completed","refused"}:
                raise AdmissionError("rollout_other_reconciliation_required")
        return json.loads(self.checkpoint.read_text())

    def refresh_administrative_hold(self):
        from .activation_gate import settled_administrative_paths
        settled=self.administrative_paths()
        if settled and self.active is None and not self.waiting:
            pending=False
            for path in self.store.root.glob("*.json"):
                if path.name in settled:continue
                value=json.loads(path.read_text())
                if value.get("state") not in {"completed","refused"}:pending=True;break
            if not pending:self.hold=False;self.held_reason=""
            else:self.hold=True;self.held_reason="administrative_successor_other_reconciliation_required"
        return settled

    def archive_administrative_predecessor(self,request=None):
        import base64,hashlib
        settled=self.administrative_paths(request)
        if "active-dispatch.json" not in settled:
            raise AdmissionError("administrative_predecessor_not_settled")
        cpraw=self.checkpoint.read_bytes();cp=json.loads(cpraw)
        path=self.store.path(cp["uid"],cp["request_id"]);raw=path.read_bytes()
        if path.name not in settled:raise AdmissionError("administrative_predecessor_receipt_changed")
        history=self.store.root/"administrative-history";history.mkdir(mode=0o700,exist_ok=True)
        fd=os.open(self.store.root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        entry={"schema":1,"state":"administrative_predecessor_archived","checkpoint":cp,
            "checkpoint_raw":base64.b64encode(cpraw).decode(),"receipt_raw":base64.b64encode(raw).decode(),
            "receipt_sha256":hashlib.sha256(raw).hexdigest()}
        target=history/(cp["fingerprint"]+".json")
        if target.exists():
            if json.loads(target.read_text())!=entry:raise AdmissionError("administrative_history_conflict")
        else:atomic(target,entry,no_replace=True)
        if (self.checkpoint.read_bytes()!=cpraw or path.read_bytes()!=raw
            or "active-dispatch.json" not in self.administrative_paths(request)):
            raise AdmissionError("administrative_predecessor_changed_after_archive")
        return cp

    def next(self):
        if (self.hold and self.active is None and not self.waiting
            and self.clock()>=self.admin_recheck):
            self.admin_recheck=self.clock()+1.
            self.refresh_administrative_hold()
        self.expire_waiters()
        if self.active is not None or not self.waiting:return None
        if (self.hold and "cleanup_of" not in self.waiting[0].record
            and not ("rollout_administrative_checkpoint" in self.waiting[0].record
                and "_rollout_binding" in self.waiting[0].request)):return None
        w=self.waiting.popleft()
        try:
            self.validate(w.request)
            self.persist(w,state="starting",started_mono=self.clock())
            self.validate(w.request)
        except AdmissionError as exc:
            self.refuse(w,self.refusal_reason(exc,"admission_lost_before_unit"))
            self.payload_bytes-=w.size;return None
        checkpoint={k:w.record[k] for k in ("uid","request_id","unit","fingerprint","generation","boot_id")}
        guard=self.admission_guard
        fence=guard.dispatch_fence(w.request) if hasattr(guard,'dispatch_fence') else contextlib.nullcontext()
        try:
            with fence:
                self.store.validate(w.request)
                require_request_budget(w.request,self.clock())
                predecessor=w.record.get("cleanup_checkpoint")
                if predecessor is None:
                    if self.checkpoint.exists():
                        if "_rollout_binding" in w.request:
                            expected=self.rollout_administrative_admission(w.uid,w.request,ignore=w.path)
                            if w.record.get("rollout_administrative_checkpoint",expected)!=expected:
                                raise AdmissionError("rollout_administrative_checkpoint_changed")
                        admin_cp=self.archive_administrative_predecessor(w.request)
                        self.persist(w,administrative_checkpoint=admin_cp)
                        self.validate(w.request)
                        # Archive and successor metadata are durable BEFORE the
                        # active pointer changes. Original receipt stays exact.
                        if json.loads(self.checkpoint.read_text())!=admin_cp:
                            raise AdmissionError("administrative_checkpoint_changed_before_replace")
                        self.archive_administrative_predecessor(w.request) # recheck after successor fsync
                        atomic(self.checkpoint,checkpoint)
                    else:atomic(self.checkpoint,checkpoint,no_replace=True)
                else:
                    if json.loads(self.checkpoint.read_text())!=predecessor:
                        raise AdmissionError("native_cleanup_checkpoint_changed")
                    oldrecord=json.loads(self.store.path(predecessor["uid"],predecessor["request_id"]).read_text())
                    oldstate=self.units.state(predecessor["unit"])
                    if not self.predecessor_terminal(oldrecord,oldstate):
                        raise AdmissionError("native_cleanup_predecessor_changed")
                    # Old proof archive and successor receipt are durable.
                    # Release only a reference owned by this broker connection.
                    self.units.release_retained(predecessor["unit"])
                    # Atomic replacement; old immutable archive remains.
                    atomic(self.checkpoint,checkpoint)
                self.active=w
                self.store.validate(w.request)
        except (AdmissionError,OSError,RuntimeError,ValueError,KeyError,TypeError) as exc:
            # A lost query, Unref or checkpoint write cannot crash the
            # frontend or authorize launch. Keep the predecessor/checkpoint.
            # A KNOWN guard refusal before ANY checkpoint has no dispatch
            # effect. Do not poison the existing owner's native lane. All I/O,
            # unknown errors and any durable/current checkpoint remain held.
            known_refusal=False
            if isinstance(exc,AdmissionError) and self.active is not w:
                try:known_refusal=not self.checkpoint.exists()
                except OSError:pass
            if not known_refusal:
                self.hold=True
                self.held_reason="dispatch_checkpoint_reconciliation_required"
            if self.active is w:
                # Checkpoint is durable: conservative reconciliation, not replay
                # or a claim that launch/handler has completed.
                self.uncertain(w,'checkpoint_admission_uncertain')
            else:
                self.refuse(w,self.refusal_reason(exc,'admission_lost_before_checkpoint'))
                self.payload_bytes-=w.size
            return None
        return w

    def reconcile(self,uid,request_id,generation):
        # Scope check BEFORE looking in RAM. Never dispatch or renew admission.
        answer=self.store.lookup(uid,request_id,generation)
        if answer.get("ok") is not True:return answer
        record=answer["receipt"]
        decision_path=self.store.root/"receipt-decisions"/(record["fingerprint"]+".json")
        if decision_path.exists():
            decision=json.loads(decision_path.read_text())
            if any(decision.get(k)!=record.get(k) for k in ("request_id","uid","generation","fingerprint")):
                raise AdmissionError("cleanup_restart_disposition_conflict")
            return {"ok":False,"error":"cleanup_admission_not_dispatched",
                    "receipt":record,"disposition":decision}
        if record.get("state")=="refused":
            original=self.original_pre_payload_proof(record)
            if original is not None:
                return {"ok":False,"error":"original_payload_not_released","receipt":record,"pre_payload_proof":original}
        cached=self.answers.get((uid,request_id))
        if record["state"]=="completed" and cached is not None:
            if cached.get("receipt")==record:return cached
        try:
            from .outcome_recovery import recover
            evidence=recover(self.store.root,record,self.units,self.store.boot)
            return {"ok":False,"error":"original_receipt_preserved","receipt":record,"recovered_result":evidence}
        except (OSError,ValueError,KeyError,RuntimeError):pass
        return {"ok":False,"error":"request_reconciliation_required","receipt":record}

    def acquisition_available(self):
        # Called only by authenticated private runtime while it holds the
        # existing authority.lock EX. No authorization/ticket/TTL is granted.
        if hasattr(self.admission_guard,"activation_clear"):
            try:self.admission_guard.activation_clear()
            except AdmissionError:return False
        settled=self.refresh_administrative_hold()
        # A queued request owns no dispatch authority. Existing EX vs SH
        # fences decide the race: if the lease wins, queued maintenance is
        # revalidated/refused before a checkpoint or unit launch. Preserve this
        # original contract; active/inflight/uncertain work still blocks lease.
        return (self.active is None and not self.hold
            and (not self.checkpoint.exists() or "active-dispatch.json" in settled))

    def before_payload(self,w):
        if self.active is not w:raise AdmissionError("worker_scope_changed")
        if w.payload_allowed or w.record["state"]!="starting":
            raise AdmissionError("worker_payload_admission_already_decided")
        # The worker cannot execute before this durable record and recheck.
        try:
            self.validate(w.request)
            unit_state=self.units.state(w.record["unit"])
            invocation=unit_state.get("InvocationID")
            import re
            if not isinstance(invocation,str) or not re.fullmatch("[a-f0-9]{32}",invocation):
                raise AdmissionError("native_dispatch_invocation_uncertain")
            group=unit_state.get("ControlGroup")
            if group!="/system.slice/"+w.record["unit"]:
                raise AdmissionError("native_dispatch_cgroup_uncertain")
            from .outcome_journal import read_only_device_contract
            effect=read_only_device_contract(w.request)
            self.persist(w,state="running",invocation_id=invocation,worker_cgroup=group,effect_contract=effect)
            self.validate(w.request)
        except (AdmissionError,OSError,RuntimeError,ValueError,KeyError,TypeError) as exc:
            invocation=self.pre_payload_refused(w)
            # Bind the final refusal to the retained launch. A changed unit
            # or missing identity must still block the next dispatch.
            self.refuse(w,self.refusal_reason(exc,"admission_lost_before_dispatch"),invocation)
            return None
        w.payload_allowed=True
        return w.request

    def completed(self,w,response):
        if self.active is not w:raise AdmissionError("worker_scope_changed")
        if not completion(response, request=w.request):
            self.uncertain(w,"handler_without_final_completion")
            return
        self.persist(w,state="completed",ok=response["ok"],
                     returncode=response["result"]["returncode"],finished_mono=self.clock())
        w.response=response
        self.answers[(w.uid,w.record["request_id"])]=dict(response,receipt=w.record)
        while len(self.answers)>32:del self.answers[next(iter(self.answers))]

    def poll_terminal(self):
        if self.active is None:return
        w=self.active
        try:state=self.units.state(w.record["unit"])
        except Exception:
            # A lost query is not a final result, OFF proof or permission to
            # start another handler. Keep the same unit and request ID.
            return
        if not terminal_proven(state,w.record["unit"]) or not w.channel_done:return
        if state["InvocationID"]!=w.record.get("invocation_id"):
            self.uncertain(w,"unit_terminal_invocation_changed")
            return
        if w.record["state"] not in {"completed","refused"}:
            self.uncertain(w,"unit_terminal_without_final_completion")
        if w.record["state"] in {"completed","refused"}:
            expected={k:w.record[k] for k in ("uid","request_id","unit","fingerprint","generation","boot_id")}
            if json.loads(self.checkpoint.read_text())!=expected:
                self.uncertain(w,"active_checkpoint_scope_changed")
            else:
                proofdir=self.store.root/"terminal-proofs"
                proofdir.mkdir(mode=0o700,exist_ok=True)
                # The directory entry must survive a crash before UnrefUnit.
                parentfd=os.open(self.store.root,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(parentfd)
                finally:os.close(parentfd)
                proof=dict(expected,state="terminal_observed",unit_state=state,
                           observed_mono=self.clock())
                atomic(proofdir/(w.record["fingerprint"]+".json"),proof)
                # Persist proof BEFORE releasing the systemd reference.
                try:self.units.release(w.record["unit"])
                except Exception:
                    self.uncertain(w,"unit_reference_release_uncertain")
                    return
                self.checkpoint.unlink()
                fd=os.open(self.store.root,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
        self.payload_bytes-=w.size;self.active=None

    def original_pre_payload_proof(self,record):
        """Read-only original evidence. Does not settle, replay or change a receipt."""
        import stat,re,hashlib,os,json
        if record.get("state")!="refused":return None
        fingerprint=record.get("fingerprint")
        if type(fingerprint) is not str or not re.fullmatch("[a-f0-9]{64}",fingerprint):
            raise AdmissionError("original_pre_payload_fingerprint")
        directory=self.store.root/"pre-payload-refusals"
        if not os.path.lexists(directory):return None
        for p in (self.store.root,directory):
            st=p.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700:
                raise AdmissionError("original_pre_payload_directory")
        parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            directory_identity=os.fstat(parent)
            if (directory_identity.st_dev,directory_identity.st_ino)!=(directory.lstat().st_dev,directory.lstat().st_ino):
                raise AdmissionError("original_pre_payload_parent_changed")
            try:fd=os.open(fingerprint+".json",os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
            except FileNotFoundError:return None
            try:
                a=os.fstat(fd)
                if not stat.S_ISREG(a.st_mode) or a.st_uid!=os.getuid() or stat.S_IMODE(a.st_mode)!=0o600 or a.st_nlink!=1:
                    raise AdmissionError("original_pre_payload_metadata")
                raw=os.read(fd,16385);b=os.fstat(fd);c=os.stat(fingerprint+".json",dir_fd=parent,follow_symlinks=False)
                linked_parent=directory.lstat()
                if (linked_parent.st_dev,linked_parent.st_ino)!=(directory_identity.st_dev,directory_identity.st_ino):
                    raise AdmissionError("original_pre_payload_parent_changed")
                ident=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_uid,x.st_gid,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
                if len(raw)>16384 or ident(a)!=ident(b) or ident(a)!=ident(c):
                    raise AdmissionError("original_pre_payload_changed")
            finally:os.close(fd)
        finally:os.close(parent)
        proof=json.loads(raw)
        keys={"uid","request_id","unit","fingerprint","generation","boot_id","state","launch_invocation_id"}
        if (type(proof) is not dict or set(proof) not in (keys,keys|{"storage_long_scope_sha256"}) or proof["state"]!="payload_not_released"
            or any(proof[k]!=record.get(k) for k in ("uid","request_id","unit","fingerprint","generation","boot_id"))
            or type(proof["uid"]) is not int
            or (proof["launch_invocation_id"] is not None and
                (type(proof["launch_invocation_id"]) is not str or not re.fullmatch("[a-f0-9]{32}",proof["launch_invocation_id"])))
            or record.get("invocation_id")!=proof["launch_invocation_id"]):
            raise AdmissionError("original_pre_payload_scope")
        if "storage_long_scope_sha256" in proof and (type(proof["storage_long_scope_sha256"]) is not str or not re.fullmatch("[a-f0-9]{64}",proof["storage_long_scope_sha256"])):
            raise AdmissionError("original_pre_payload_long_scope")
        return {"kind":"original_payload_not_released_v1","proof":proof,"proof_sha256":hashlib.sha256(raw).hexdigest()}


def prepayload_abort(record,checkpoint_path,units,boot=None,absent=None):
    """No payload is sent before the durable running record; hold still stays."""
    from .identified_units import fixed_cgroup_absent
    if absent is None:absent=fixed_cgroup_absent
    if (record.get("state")!="starting" or "invocation_id" in record
        or type(record.get("cleanup_checkpoint")) is not dict):return False
    try:
        if boot is None:boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if record["boot_id"]!=boot:return False
        keys=("uid","request_id","unit","fingerprint","generation","boot_id")
        if json.loads(checkpoint_path.read_text())!={k:record[k] for k in keys}:return False
        state=units.state(record["unit"])
        expected={"Id":record["unit"],"LoadState":"not-found","ActiveState":"inactive",
            "SubState":"dead","MainPID":"0","ControlPID":"0","Job":"",
            "ControlGroup":"","Result":"success","InvocationID":""}
        return state==expected and absent(record["unit"])
    except (OSError,ValueError,KeyError,TypeError,RuntimeError):return False
