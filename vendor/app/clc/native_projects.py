"""Native project queue with the existing receipt database; no Dot socket."""
import asyncio
import contextlib
import os
import logging
from .project_relay import ProjectRelay

class NativeProjects:
    def __init__(self, directory, handler, ready):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner_uid = os.getuid()
        self.project_relay = ProjectRelay(directory, handler, self.owner_uid, 1001)
        self.ready = ready
        self.task = None
    async def start(self):
        self.task = asyncio.create_task(self.loop(), name="clc-native-project-dispatch")
    async def loop(self):
        while True:
            try:
                if self.ready():
                    await self.project_relay.dispatch_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("Native project queue failed closed")
            await asyncio.sleep(3)
    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.project_relay.db.close()
