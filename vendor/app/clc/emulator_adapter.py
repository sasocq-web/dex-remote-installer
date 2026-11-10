"""Unprivileged adapter to the fixed root-owned emulator helper."""
import asyncio, base64, io, json, pathlib, shutil, subprocess

HELPER='/usr/local/lib/dex-emulator/host-coordinated-v1.py'

class Host:
    async def call(self,operation,lease,arguments=None):
        request=dict(operation=operation,slot=lease.slot,profile=lease.purpose,token=lease.token)
        if arguments is not None:request['arguments']=arguments
        def invoke():
            # The pool drains this call on cancellation. No UI operation retries.
            p=subprocess.run(['sudo','-n','/usr/bin/python3',HELPER],input=json.dumps(request),
                             text=True,capture_output=True,timeout=370)
            try:value=json.loads(p.stdout)
            except ValueError:raise RuntimeError('emulator_helper_response_invalid') from None
            if p.returncode or 'error' in value:raise RuntimeError(value.get('error','emulator_helper_failed'))
            return value
        return await asyncio.to_thread(invoke)

    async def start(self,lease):
        if (await self.call('start',lease)).get('boot_completed') is not True:raise RuntimeError('emulator_boot_unconfirmed')

    async def stop(self,lease):await self.call('stop',lease)
    async def stopped(self,lease):return (await self.call('stopped',lease)).get('stopped') is True

    async def command(self,lease,args):
        result=await self.call('command',lease,args)
        if args['operation']=='screenshot':
            from PIL import Image
            data=base64.b64decode(result['png'],validate=True)
            with Image.open(io.BytesIO(data)) as frame:
                if frame.width>4096 or frame.height>4096:raise ValueError('emulator_capture_too_large')
                frame.thumbnail((1280,1280));out=io.BytesIO();frame.convert('RGB').save(out,format='JPEG',quality=85)
            return {'image_url':'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()}
        return result

    async def metrics(self):
        def read():
            mem=pathlib.Path('/proc/meminfo').read_text()
            available=int(mem.split('MemAvailable:')[1].split()[0])/1024
            full=next(line for line in pathlib.Path('/proc/pressure/memory').read_text().splitlines() if line.startswith('full '))
            psi=dict(item.split('=') for item in full.split()[1:])
            def busy(name):
                p=subprocess.run(['systemctl','show',name,'-p','ActiveState','--value'],text=True,capture_output=True,timeout=5)
                return p.returncode!=0 or p.stdout.strip() not in {'inactive','failed'}
            return dict(mem_available_mib=available,memory_full_avg10=float(psi['avg10']),memory_full_avg60=float(psi['avg60']),
                        disk_available_mib=shutil.disk_usage('/var/lib').free/1024**2,
                        backup_running=busy('sasocq-backup.service'),legacy_running=any(busy(n) for n in ('sasocq-android-waydroid.target','waydroid-container.service')))
        return await asyncio.to_thread(read)
