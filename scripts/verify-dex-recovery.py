"""Compare restored recovery content, not just successful service exit status."""
import hashlib
import json
import sys
from pathlib import Path


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"arquivo ausente ou link não permitido: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(local, restored, snapshot):
    bundle = Path("recovery/dex-current")
    required = {Path(".git/refs/heads/main"), bundle / "manifest.json"}
    # Include scripts and timer configuration, even if only those changed.
    for directory in ("scripts", "systemd", str(bundle)):
        required.update(p.relative_to(local) for p in (local / directory).rglob("*")
                        if p.is_file() and "__pycache__" not in p.parts)
    for relative in sorted(required):
        if digest(local / relative) != digest(restored / relative):
            raise ValueError(f"snapshot divergente: {relative}")
    manifest = json.loads((restored / bundle / "manifest.json").read_text())
    for name, checksum in (("package", "sha256"), ("control_plane_package", "control_plane_sha256")):
        filename = manifest[name]
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("nome de pacote inválido no manifesto")
        if digest(restored / bundle / filename) != manifest[checksum]:
            raise ValueError(f"pacote inválido no snapshot: {filename}")
    return {"verified": True, "snapshot": snapshot, "files_checked": len(required),
            "package_version": manifest["package_version"],
            "git_commit": (restored / ".git/refs/heads/main").read_text().strip(),
            "manifest_sha256": digest(restored / bundle / "manifest.json")}


if __name__ == "__main__":
    try:
        print(json.dumps(verify(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])))
    except (OSError, ValueError, KeyError, IndexError) as exc:
        print(json.dumps({"verified": False, "error": str(exc)}))
        raise SystemExit(1)
