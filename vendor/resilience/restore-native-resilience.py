#!/usr/bin/python3
"""Restore the SASOCQ native recovery files after the matching base is restored."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


ASSETS = Path(__file__).resolve().parent
BROKER = Path("usr/lib/python3/dist-packages/sasocq_control")
ROOT_LIB = Path("usr/local/lib/dex-rescue")
NATIVE_MARKER = Path("opt/codex-linux-control/current/system/native-root-bundle/manifest.json")
NATIVE_BACKEND = Path("usr/lib/systemd/user/codex-linux-control-backend.service")
NAMES = {"bounded_protocol.py", "broker_queue.py", "identified_units.py", "outcome_journal.py"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic_file(path, raw, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".resilience-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply(root, broker_active):
    if not (root / NATIVE_MARKER).is_file() or not (root / NATIVE_BACKEND).is_file():
        return "portable"
    pins = json.loads((ASSETS / "broker-pins.json").read_bytes())
    if set(pins) != NAMES:
        raise ValueError("Invalid native broker payload scope")
    changes = {}
    for name, pin in pins.items():
        destination = root / BROKER / name
        current = destination.read_bytes()
        new = (ASSETS / "broker-candidate" / name).read_bytes()
        if (not stat.S_ISREG(destination.lstat().st_mode)
                or sha(new) != pin["new"] or sha(current) not in {pin["old"], pin["new"]}):
            raise ValueError(f"Broker version does not match this maintenance release: {name}")
        if current != new:
            changes[destination] = (current, new)
    if changes and broker_active():
        raise RuntimeError("Install the matched native recovery files with the broker stopped")
    for name in ("recovery_auth.py", "restore_runner.py", "repair-prompt.txt"):
        target = root / ROOT_LIB / name
        if not target.exists():
            atomic_file(target, (ASSETS / name).read_bytes(), 0o644)
    for name in ("dex-rescue-gateway.service", "dex-restore.service"):
        target = root / "usr/lib/systemd/user" / name
        if not target.exists():
            atomic_file(target, (ASSETS / name).read_bytes(), 0o644)
    applied = []
    try:
        for path, (before, after) in changes.items():
            atomic_file(path, after, 0o644)
            applied.append(path)
        atomic_file(root / ROOT_LIB / "recovery_gateway.py", (ASSETS / "recovery_gateway.py").read_bytes(), 0o644)
    except Exception:
        for path in applied:
            atomic_file(path, changes[path][0], 0o644)
        raise
    return "native"


def broker_active():
    result = subprocess.run(["systemctl", "show", "sasocq-control-broker.service",
                             "--property=ActiveState", "--value"], text=True,
                            capture_output=True, check=True, timeout=5)
    return result.stdout.strip() != "inactive"


if __name__ == "__main__":
    if len(sys.argv) != 1 or os.geteuid() != 0:
        raise SystemExit("Native reinstall helper requires the SASOCQ installer")
    profile = apply(Path("/"), broker_active)
    if profile == "native":
        subprocess.run(["systemctl", "--user", "--machine=codex@.host", "daemon-reload"], check=True, timeout=10)
        subprocess.run(["systemctl", "--user", "--machine=codex@.host",
                        "enable", "--now", "dex-rescue-gateway.service"], check=True, timeout=15)
    print(f"Dex recovery installation profile: {profile}")
