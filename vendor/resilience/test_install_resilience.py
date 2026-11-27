from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

spec = spec_from_file_location("installer", Path(__file__).with_name("restore-native-resilience.py"))
installer = module_from_spec(spec)
spec.loader.exec_module(installer)


class PackagedResilience(TestCase):
    def test_portable_modes_leave_native_files_empty(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(installer.apply(root, lambda: self.fail("unexpected service query")), "portable")
            self.assertEqual(list(root.iterdir()), [])

    def test_native_install_uses_verified_packaged_broker_and_gateway(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (installer.NATIVE_MARKER, installer.NATIVE_BACKEND):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).touch()
            broker = root / installer.BROKER
            broker.mkdir(parents=True)
            pins = json.loads((installer.ASSETS / "broker-pins.json").read_bytes())
            for name, values in pins.items():
                raw = (installer.ASSETS / "broker-candidate" / name).read_bytes()
                self.assertEqual(installer.sha(raw), values["new"])
                (broker / name).write_bytes(raw)
            protocol_spec = spec_from_file_location("packaged_protocol", broker / "bounded_protocol.py")
            protocol = module_from_spec(protocol_spec)
            protocol_spec.loader.exec_module(protocol)
            status = {"ok": True, "result": {"returncode": 0, "output": {
                "name": "sasocq-server", "available": True, "current": {}, "recommended": {}, "state": "running"}}}
            request = {"action": "vm", "params": {"operation": "resource-status"}}
            self.assertTrue(protocol.completion(status, request=request))
            self.assertFalse(protocol.completion(status))
            self.assertEqual(installer.apply(root, lambda: self.fail("already qualified")), "native")
            gateway = root / installer.ROOT_LIB / "recovery_gateway.py"
            self.assertEqual(gateway.read_bytes(), (installer.ASSETS / gateway.name).read_bytes())
            self.assertTrue((root / "usr/lib/systemd/user/dex-rescue-gateway.service").is_file())
