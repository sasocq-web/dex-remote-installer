"""Read-only broker availability inside the EXISTING Authority EX transaction."""
import json,socket,struct,time,uuid
from pathlib import Path

def broker_acquisition_available():
    deadline=time.monotonic()+1.
    request_id=uuid.uuid4().hex
    boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    def budget(sock):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError("broker_admission_deadline")
        sock.settimeout(remaining)
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            budget(sock);sock.connect("/run/sasocq-control/broker.sock")
            pid,uid,gid=struct.unpack("3i",sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=0 or pid<=0:raise ValueError("broker_peer_not_root")
            budget(sock)
            sock.sendall(json.dumps({"action":"broker.runtime_availability","request_id":request_id}).encode()+b"\n")
            data=bytearray()
            while b"\n" not in data:
                budget(sock);part=sock.recv(4097-len(data))
                if not part:raise ValueError("broker_admission_eof")
                data.extend(part)
                if len(data)>4096:raise ValueError("broker_admission_size")
            budget(sock)
            raw,trailing=bytes(data).split(b"\n",1)
            if trailing:raise ValueError("broker_admission_extra")
            value=json.loads(raw)
            if type(value) is not dict or set(value)!={"ok","result"} or value["ok"] is not True:raise ValueError()
            result=value["result"]
            if type(result) is not dict or set(result)!={"schema","request_id","boot_id","available"}:raise ValueError()
            if type(result["schema"]) is not int or result["schema"]!=1 or result["request_id"]!=request_id or result["boot_id"]!=boot or type(result["available"]) is not bool:raise ValueError()
            return result["available"]
    except (OSError,ValueError,TypeError,KeyError):
        raise ValueError("broker_acquisition_unconfirmed") from None
