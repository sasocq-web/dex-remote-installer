"""Sole runtime broker transport: one send, same-ID read-only reconciliation.

This is not authority: native action authorization is enforced by the broker.
Private command/output bytes stay in RAM. Unknown completion never triggers send.
"""
import json,math,re,socket,struct,time,uuid
from pathlib import Path

class BrokerUncertain(RuntimeError):
    pass

class BrokerNotDispatched(BrokerUncertain):
    """Confirmed admission abort; never implies Android OFF or lease release."""
    pass

def not_dispatched(answer,payload,receipt):
    d=answer.get("disposition")
    if type(d) is not dict:return False
    if (payload["action"]!="service" or payload["params"]!={"unit":"sasocq-android-waydroid.target","operation":"stop","confirm":True}
        or answer.get("error")!="cleanup_admission_not_dispatched"
        or answer.get("ok") is not False or receipt.get("state") not in {"queued","starting"}
        or set(d)!={"state","request_id","uid","generation","fingerprint","predecessor_checkpoint","reason"}
        or d["state"]!="not_dispatched" or d["reason"] not in {"broker_restart_before_cleanup_handoff","broker_restart_before_cleanup_payload"}):
        return False
    if any(d[k]!=receipt.get(k) for k in ("request_id","uid","generation","fingerprint")):return False
    prior=d["predecessor_checkpoint"]
    return (type(prior) is dict
        and set(prior)=={"uid","request_id","unit","fingerprint","generation","boot_id"}
        and prior==receipt.get("cleanup_checkpoint")
        and prior["request_id"]==receipt.get("cleanup_of")
        and prior["request_id"]!=receipt["request_id"]
        and prior["uid"]==receipt["uid"] and prior["generation"]==receipt["generation"]
        and prior["boot_id"]==receipt["boot_id"])

def exchange(payload,deadline):
    def budget(sock):
        left=deadline-time.monotonic()
        if left<=0:raise TimeoutError()
        sock.settimeout(left)
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            budget(sock);sock.connect("/run/sasocq-control/broker.sock")
            pid,uid,gid=struct.unpack("3i",sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=0 or pid<=0:raise ValueError()
            budget(sock);sock.sendall(json.dumps(payload,ensure_ascii=False).encode()+b"\n")
            data=bytearray()
            while b"\n" not in data:
                budget(sock);chunk=sock.recv(min(8192,2_000_001-len(data)))
                if not chunk:raise ValueError()
                data.extend(chunk)
                if len(data)>2_000_000:raise ValueError()
            budget(sock)
            raw,extra=bytes(data).split(b"\n",1)
            if extra:raise ValueError()
            result=json.loads(raw)
            if type(result) is not dict:raise ValueError()
            return result
    except (OSError,ValueError,TypeError):
        raise BrokerUncertain("broker_response_unconfirmed") from None

def completion(response):
    """Only consistent ordinary exits, with no nested uncertainty markers."""
    if type(response) is not dict or type(response.get("ok")) is not bool:return False
    result=response.get("result")
    if type(result) is not dict:return False
    output=result.get("output")
    parts=[response,result]+([output] if type(output) is dict else [])
    for part in parts:
        if (part.get("pending") is True or part.get("timed_out") is True
            or part.get("execution_uncertain") is True
            or part.get("state") in {"queued","pending","running","timeout","timed_out","interrupted"}):
            return False
    codes=[v["returncode"] for v in [result,output] if type(v) is dict and "returncode" in v]
    return (bool(codes) and "returncode" in result
        and all(type(rc) is int and 0<=rc<124 for rc in codes)
        and len(set(codes))==1 and response["ok"] is (codes[0]==0))

class RuntimeBrokerRequests:
    def __init__(self,authority,*,transport=exchange,clock=time.monotonic,sleep=time.sleep):
        self.authority,self.transport,self.clock,self.sleep=authority,transport,clock,sleep

    def __call__(self,action,params=None,*,timeout=120):
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=120:
            raise ValueError("private_request_timeout_invalid")
        started=self.clock()
        if type(started) not in (int,float) or not math.isfinite(started):
            raise BrokerUncertain("broker_clock_invalid")
        end=started+timeout;last=started
        def observe():
            nonlocal last
            now=self.clock()
            if type(now) not in (int,float) or not math.isfinite(now) or not last<=now<end:
                raise BrokerUncertain("broker_original_deadline_or_clock_lost")
            last=now
            return now
        lease=self.authority.read()
        generation=lease["generation"] if lease else uuid.uuid4().hex
        boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        rid=uuid.uuid4().hex
        payload={"request_id":rid,"action":action,"params":params or {},
            "admission":{"schema":1,"boot_id":boot,"generation":generation,"deadline_mono":end}}
        # Serialization freezes caller-owned dictionaries before the only send.
        payload=json.loads(json.dumps(payload))
        try:
            answer=self.transport(payload,end)
        except BrokerUncertain:
            answer=None
        while True:
            now=observe()
            if answer is not None:
                receipt=answer.get("receipt")
                if type(receipt) is not dict:
                    raise BrokerUncertain("broker_receipt_missing")
                if (receipt.get("request_id")!=rid or receipt.get("generation")!=generation
                    or receipt.get("boot_id")!=boot or receipt.get("deadline_mono")!=end):
                    raise BrokerUncertain("broker_receipt_scope_changed")
                if "disposition" in answer:
                    if not_dispatched(answer,payload,receipt):
                        raise BrokerNotDispatched("broker_cleanup_admission_not_dispatched_no_replay")
                    raise BrokerUncertain("broker_cleanup_disposition_unconfirmed")
                state=receipt.get("state")
                if state=="completed":
                    result=answer.get("result")
                    if (completion(answer)
                        and result["returncode"]==receipt.get("returncode")
                        and answer["ok"] is receipt.get("ok")):
                        return answer
                    raise BrokerUncertain("broker_final_output_unavailable")
                if state=="refused":
                    raise BrokerUncertain("broker_request_refused_no_replay")
                if state not in {"queued","admitted","starting","running"}:
                    raise BrokerUncertain("broker_reconciliation_required")
            # Revalidation cannot authorize a second command. Only query receipt.
            query={"action":"broker.receipt","params":{"request_id":rid,"generation":generation}}
            self.sleep(min(.05,max(0,end-observe())))
            observe()
            try:answer=self.transport(query,end)
            except BrokerUncertain:raise BrokerUncertain("broker_receipt_unavailable_no_replay") from None
