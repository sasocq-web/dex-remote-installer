"""Sole future daemon Lifecycle with crash-safe reservation storage.

Not instantiated by Dex. Migration must replace its existing Lifecycle with a
typed client, never run this candidate next to that legacy authority.
"""
from clc.android_lifecycle import AndroidLifecycle
from .android_authority import Authority

class DurableLifecycle(AndroidLifecycle):
    def __init__(self,marker,authority,**kwargs):
        super().__init__(marker,**kwargs)
        self.authority=authority
        lease=authority.inspect()
        self.durable_generation=lease['generation'] if lease else None
        self.cleanup_failed=self.cleanup_failed or lease is not None

    async def _acquire(self,key,turn_active,turn_id=''):
        if self.owner is not None and self.owner!=key:
            raise ValueError('authority_busy')
        if self.owner is None:
            if self.cleanup_failed:raise ValueError('authority_cleanup_required')
            lease=self.authority.acquire(key,1500)
            self.durable_generation=lease['generation']
        try:
            await super()._acquire(key,turn_active,turn_id)
        except BaseException:
            if self.durable_generation:
                self.authority.begin_cleanup(self.durable_generation)
                self.cleanup_failed=True
            raise

    async def _stop(self):
        generation=self.durable_generation
        if generation:self.authority.begin_cleanup(generation)
        # This inherited routine verifies all units and stops ADB before
        # clearing its marker. An exception keeps the durable reservation.
        await super()._stop()
        if generation:
            try:
                self.authority.cleanup_verified(generation,
                    lambda:self.owner is None and self.cleanup_failed is False)
            except BaseException:
                self.cleanup_failed=True
                raise
            self.durable_generation=None

    async def recover(self):
        async with self.lock:
            if self.marker.exists() or self.durable_generation:
                await self._stop()
