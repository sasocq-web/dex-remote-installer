import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import recovery_gateway as gateway


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def upstream(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = b"<html><head></head><body>Dex</body></html>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            writer.close()

        origin = await asyncio.start_server(upstream, "127.0.0.1", 0)
        self.server = await asyncio.start_server(gateway.client, "127.0.0.1", 0)
        self.addAsyncCleanup(origin.wait_closed)
        self.addCleanup(origin.close)
        self.addAsyncCleanup(self.server.wait_closed)
        self.addCleanup(self.server.close)
        self.connect = asyncio.open_connection

        async def connect(host, port):
            self.assertEqual(port, 8787)
            return await self.connect(host, origin.sockets[0].getsockname()[1])

        self.enterContext(patch.object(gateway.asyncio, "open_connection", connect))
        self.health = self.enterContext(patch.object(gateway, "health", AsyncMock(return_value=True)))
        self.enterContext(patch.object(gateway, "broker_ready", AsyncMock(return_value=False)))
        self.command = self.enterContext(patch.object(gateway, "command", AsyncMock(return_value=(0, "inactive"))))

    async def request(self, request):
        port = self.server.sockets[0].getsockname()[1]
        reader, writer = await self.connect("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        try:
            return await asyncio.wait_for(reader.read(), 2)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_broker_hold_preserves_dex_but_reports_degraded_recovery(self):
        response = await self.request(b"GET / HTTP/1.1\r\nAccept: text/html\r\n\r\n")
        self.assertIn(b"200 OK", response)
        self.assertIn(b"/__dex_recovery/watch.js", response)
        with patch.object(gateway, "authorize", return_value=""):
            response = await self.request(b"GET /__dex_recovery/status HTTP/1.1\r\n\r\n")
        status = json.loads(response.split(b"\r\n\r\n", 1)[1])
        self.assertTrue(status["application_ready"])
        self.assertFalse(status["ready"])
        self.assertIn("broker", status["reason"])

    async def test_actual_application_outage_redirects(self):
        self.health.side_effect = [False, True]
        response = await self.request(b"GET / HTTP/1.1\r\nAccept: text/html\r\n\r\n")
        self.assertIn(b"302 Found\r\n", response)
        self.assertIn(b"Location: /__dex_recovery/", response)

    async def test_authorized_restore_reports_dispatch_success_or_failure(self):
        request = (b"POST /__dex_recovery/restore HTTP/1.1\r\n"
                   + f"Origin: {gateway.ORIGIN}\r\nX-Dex-Recovery: {gateway.TOKEN}\r\n".encode()
                   + b"\r\n")
        denied = await self.request(request)
        self.assertIn(b"403 Forbidden", denied)
        self.command.assert_not_awaited()
        with patch.object(gateway, "authorize", return_value=""):
            self.assertIn(b"202 Accepted", await self.request(request))
            self.command.assert_awaited_once_with("start", "--no-block", "dex-restore.service")
            self.command.side_effect = TimeoutError
            with self.assertLogs(gateway.LOG, level="ERROR"):
                self.assertIn(b"503 Service Unavailable", await self.request(request))

    async def test_broker_attempt_does_not_suppress_later_application_repair(self):
        class EndSample(Exception):
            pass

        healthy_app = {"backend": True, "front": True, "broker": False}
        down_app = {"backend": False, "front": True, "broker": False}
        with TemporaryDirectory() as folder:
            base = Path(folder)
            broker_attempt = base / "incident-attempt.json"
            broker_attempt.write_text("{}")
            with (patch.object(gateway, "BASE", base),
                  patch.object(gateway, "planned_update", return_value=False),
                  patch.object(gateway, "readiness", AsyncMock(side_effect=[healthy_app] * 3 + [down_app] * 3)),
                  patch.object(gateway.asyncio, "sleep", AsyncMock(side_effect=[None] * 6 + [EndSample()]))):
                with self.assertRaises(EndSample):
                    await gateway.monitor()
            self.command.assert_any_await("start", "--no-block", "dex-restore.service")
            self.assertEqual(self.command.await_count, 2)
            self.assertTrue(broker_attempt.exists())
            self.assertTrue((base / "application-incident-attempt.json").exists())


if __name__ == "__main__":
    unittest.main()
