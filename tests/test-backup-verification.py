import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup_verification", ROOT / "scripts/verify-dex-recovery.py")
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name) / "local"
        self.remote = Path(self.temp.name) / "restored"
        self.bundle = self.local / "recovery/dex-current"
        self.bundle.mkdir(parents=True)
        (self.local / ".git/refs/heads").mkdir(parents=True)
        (self.local / ".git/refs/heads/main").write_text("current-commit\n")
        (self.local / "scripts").mkdir()
        (self.local / "scripts/publish-after-backup").write_text("new publisher")
        (self.local / "systemd").mkdir()
        manifest = {"package_version": "1.0.0", "package": "dex.deb", "control_plane_package": "control.deb"}
        for name, checksum in (("package", "sha256"), ("control_plane_package", "control_plane_sha256")):
            (self.bundle / manifest[name]).write_bytes(b"fixture")
            manifest[checksum] = hashlib.sha256(b"fixture").hexdigest()
        (self.bundle / "manifest.json").write_text(json.dumps(manifest))
        shutil.copytree(self.local, self.remote)

    def test_matching_snapshot_is_verified(self):
        self.assertTrue(verification.verify(self.local, self.remote, "snapshot-id")["verified"])

    def test_old_git_reference_is_not_confirmed(self):
        (self.remote / ".git/refs/heads/main").write_text("old-commit\n")
        with self.assertRaisesRegex(ValueError, "divergente"):
            verification.verify(self.local, self.remote, "old")

    def test_script_change_requires_new_backup_even_with_same_manifest(self):
        (self.remote / "scripts/publish-after-backup").write_text("old publisher")
        with self.assertRaisesRegex(ValueError, "divergente"):
            verification.verify(self.local, self.remote, "old")

    def test_corrupt_package_is_not_confirmed(self):
        (self.remote / "recovery/dex-current/dex.deb").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            verification.verify(self.local, self.remote, "broken")

    def test_missing_file_is_not_confirmed(self):
        (self.remote / "recovery/dex-current/dex.deb").unlink()
        with self.assertRaises(ValueError):
            verification.verify(self.local, self.remote, "broken")

    def test_post_backup_failure_preserves_pending_marker(self):
        self.exercise_cleanup(False)

    def test_only_verified_snapshot_clears_pending_marker(self):
        self.exercise_cleanup(True)

    def exercise_cleanup(self, verified):
        source = (ROOT / "scripts/publish-after-backup").read_text()
        cleanup = source[source.index("cleanup() {"):source.index("trap cleanup EXIT")]
        state = Path(self.temp.name) / "state"
        state.mkdir()
        (state / "backup-required").write_text("pending")
        code = '''set -eu
bundle_digest() { printf current; }
sasocq-brokerctl() { printf '{"ok":%s}' "$VERIFY_RESULT"; return "$VERIFY_RC"; }
''' + cleanup + '\ncleanup\n'
        env = {**os.environ, "STATE_DIR": str(state), "BACKUP_MARKER": str(state / "backup-required"),
               "WORK_ROOT": "", "SYNC_PERFORMED": "yes", "TRIGGER_BACKUP": "yes",
               "CONFIRM_BACKUP": "yes", "VERIFY_RESULT": "true" if verified else "false",
               "VERIFY_RC": "0" if verified else "1"}
        result = subprocess.run(["bash", "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if verified else 1, result.stderr)
        self.assertEqual((state / "backup-required").exists(), not verified)
        self.assertEqual((state / "backup-confirmed.json").exists(), verified)


if __name__ == "__main__":
    unittest.main()
