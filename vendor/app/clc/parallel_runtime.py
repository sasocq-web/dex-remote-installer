"""Preserve legacy authority and route explicit purposes to isolated emulators."""
from .durable_android_runtime import DurableLifecycle
from .android_lifecycle import AndroidLifecycle
from .emulator_pool import Pool, profile_id, admitted
from .emulator_adapter import Host
from .coordinated_pool import CoordinatedPool, MODES

EMULATOR_OPERATIONS={'capabilities','status','start','stop','open','play','ui','apps','is-installed','back','home','tap','type','screenshot','swipe','long_press','orientation'}

def validate_emulator(args):
    if args.get('operation') not in EMULATOR_OPERATIONS:raise ValueError('unsupported_emulator_operation')
    if args.get('operation')=='capabilities':
        if set(args)!={'operation'}:raise ValueError('invalid_capabilities_arguments')
        return None
    return AndroidLifecycle.command(None,args)

def validate_request(args):
    if type(args) is not dict:raise ValueError('invalid_arguments')
    if 'purpose' in args:
        profile_id('system',args['purpose'])
        mode=args.get('mode','exclusive')
        if mode not in MODES:raise ValueError('invalid_sharing_mode')
        clean={k:v for k,v in args.items() if k not in {'purpose','mode'}}
        if mode!='exclusive' and clean.get('operation') in {'start','open','acquire','yield','status','stop'}:
            allowed={'operation','package'} if clean['operation'] in {'start','open','acquire'} else {'operation'}
            if set(clean)-allowed:raise ValueError('invalid_shared_arguments')
            if 'package' in clean:validate_emulator({'operation':'open','package':clean['package']})
            return None
        return validate_emulator(clean)
    return AndroidLifecycle.command(None,args)

class ParallelLifecycle(DurableLifecycle):
    def __init__(self,marker,authority,host=None,**kwargs):
        super().__init__(marker,authority,**kwargs)
        self.pool=CoordinatedPool(marker.parent/'android-runtime/emulator-leases.sqlite3',host or Host(),validate_emulator,lock=self.lock)
        self.pool.external_admission=lambda:not self.cleanup_failed
        self.frontend_alive=True

    def command(self,args):return validate_request(args)

    async def _acquire(self,key,turn_active,turn_id=''):
        # Serialize starts across providers, but allow independent conversations.
        if self.pool.lease(key):raise ValueError('conversation_has_other_android_provider')
        if self.owner is None and self.pool.occupied:
            if not admitted(await self.pool.host.metrics(), self.pool.reservations.list()):
                raise ValueError('android_shared_resources_unavailable')
        return await super()._acquire(key,turn_active,turn_id)

    async def execute(self,workspace,thread,args,turn_active=True,turn_id='',**kwargs):
        self.command(args)
        if 'purpose' in args:
            if self.owner == (workspace,thread):raise ValueError('conversation_has_other_android_provider')
            if kwargs:raise ValueError('scheduled_emulator_migration_not_supported')
            return await self.pool.execute(workspace,thread,args,turn_id,turn_active)
        return await super().execute(workspace,thread,args,turn_active,turn_id,**kwargs)

    async def recover_legacy(self):
        await super().recover()

    async def recover(self):
        await super().recover()
        await self.pool.recover()

    async def viewer(self,key,token,connected):
        if self.pool.lease(key):return await self.pool.viewer(key,token,connected)
        return await super().viewer(key,token,connected)

    async def observe(self,event):
        await super().observe(event)
        await self.pool.observe(event)

    async def sweep(self,active_keys):
        await super().sweep(active_keys)
        await self.pool.sweep(active_keys,self.frontend_alive)
