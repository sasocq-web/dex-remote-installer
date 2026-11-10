"""Read-only registered disk jobs; never starts or resumes an operation."""
import json
import re
import time
import threading
import codecs
from collections import deque
from pathlib import Path

REGISTRY = Path("/etc/sasocq-disk-activities")
UNIT = re.compile(r"^sasocq-[a-zA-Z0-9_.-]+\.service$")
def parse_progress(raw):
    matches = list(re.finditer(r"(\d+(?:[.,]\d+)?)%\s+(?:feito|done),\s*([0-9:]+)", raw))
    if not matches:
        return {"percent": None, "elapsed": ""}
    last = matches[-1]
    return {"percent": min(100, max(0, float(last[1].replace(",", ".")))), "elapsed": last[2]}

def registrations():
    result = []
    for path in sorted(REGISTRY.glob("*.json"))[:32]:
        try:
            data = json.loads(path.read_text())
            if not UNIT.fullmatch(data.get("unit", "")) or not data.get("serial"):
                continue
            log = Path(data.get("log", ""))
            if log.parent != Path("/var/log") or not log.name.startswith("sasocq-") or log.is_symlink():
                continue
            result.append(data)
        except (OSError, ValueError, TypeError):
            continue
    return result

def read_activity(data, unit_raw):
    fields = dict(line.split("=", 1) for line in unit_raw.splitlines() if "=" in line)
    raw = ""
    try:
        with open(data["log"], "rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 65536))
            raw = stream.read(65536).decode("utf-8", "replace")
    except OSError:
        pass
    progress = parse_progress(raw)
    active = fields.get("ActiveState") == "active"
    interrupted = "Interrupted at block" in raw
    failed = fields.get("Result") not in (None, "", "success") or fields.get("ActiveState") == "failed"
    # No completion inference from a percentage or a vanished transient unit.
    completed = bool(re.search(r"(?:Pass completed|Passagem concluída).*", raw)) and not active and not interrupted and not failed
    status = "running" if active else "interrupted" if interrupted else "failed" if failed else "completed" if completed else "unknown"
    return {"serial": data["serial"], "title": str(data.get("title", "Atividade no disco"))[:120],
            "status": status, "scope": "Progresso da etapa atual", **progress, **estimate(data, status)}

# Incremental, bounded log reader: retain only recent samples, never reread a
# multi-hour log on each refresh. Lock protects concurrent dashboard requests.
_CACHE = {}
_LOCK = threading.Lock()
_TOKEN = re.compile(r"(?P<phase>(?:Testando com padrão|Testing with pattern)\s+0x[0-9a-fA-F]+:|(?:Lendo e comparando|Reading and comparing):)|(?P<pct>\d+(?:[.,]\d+)?)%\s+(?:feito|done),\s*(?P<elapsed>[0-9:]+)\s")
def elapsed_seconds(value):
    result = 0
    for part in value.split(":"):
        result = result * 60 + int(part)
    return result

def estimate(data, status, now=None):
    empty = {"eta_seconds": None, "stage_eta_seconds": None, "eta_at": None}
    if status != "running":
        return empty
    now = time.time() if now is None else now
    try:
        path = data["log"]
        with _LOCK, open(path, "rb") as stream:
            st = __import__("os").fstat(stream.fileno())
            identity = (st.st_dev, st.st_ino)
            state = _CACHE.get(path)
            if not state or state["identity"] != identity or st.st_size < state["offset"]:
                state = {"identity": identity, "offset": 0, "buffer": "", "decoder": codecs.getincrementaldecoder("utf-8")("replace"), "phase": 0, "samples": deque(maxlen=180)}
                if len(_CACHE) >= 32:
                    _CACHE.clear()
                _CACHE[path] = state
            stream.seek(state["offset"])
            # At most 32 MiB per request; catch up later if an unexpected log is huge.
            budget = 32 * 1024 * 1024
            while budget > 0:
                chunk = stream.read(min(65536, budget))
                if not chunk:
                    break
                budget -= len(chunk)
                state["offset"] += len(chunk)
                raw = state["buffer"] + state["decoder"].decode(chunk)
                end = 0
                for token in _TOKEN.finditer(raw):
                    end = token.end()
                    if token["phase"]:
                        state["phase"] += 1
                        state["samples"].clear()
                    else:
                        state["samples"].append((float(token["pct"].replace(",", ".")), elapsed_seconds(token["elapsed"])))
                state["buffer"] = raw[end:][-4096:]
            phase = state["phase"]
            total = data.get("total_phases")
            result = {**empty, "phase": phase or None, "total_phases": total}
            samples = list(state["samples"])
            if st.st_size != state["offset"] or now - st.st_mtime > 30 or len(samples) < 2:
                return result
            pct, elapsed = samples[-1]
            candidates = [x for x in samples[:-1] if 30 <= elapsed - x[1] <= 120]
            if not candidates or not 0 < pct < 100:
                return result
            first = candidates[0]
            # No optimistic countdown during a stalled disk or at a phase boundary.
            recent = [x for x in samples[:-1] if 15 <= elapsed - x[1] <= 30]
            if not recent or pct <= recent[0][0] or pct <= first[0]:
                return result
            rate = (pct - first[0]) / (elapsed - first[1])
            result["stage_eta_seconds"] = round((100 - pct) / rate)
            if isinstance(total, int) and 1 <= phase <= total <= 64:
                remaining = ((total - phase) * 100 + 100 - pct) / rate
                result.update(eta_seconds=round(remaining), eta_at=st.st_mtime + remaining)
            return result
    except (OSError, ValueError, KeyError):
        return empty
