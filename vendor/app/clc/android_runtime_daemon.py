"""One independent Android authority and fixed collector, owned by codex."""
from .android_broker_admission import broker_acquisition_available
from .android_broker_requests import RuntimeBrokerRequests
import asyncio,contextlib,json,os,signal,time
from pathlib import Path
from .android_runtime_ipc import Server
from .android_authority import Authority
from .parallel_runtime import ParallelLifecycle as DurableLifecycle
from .android_runtime_controller import Controller
from .android_jobs import ScheduledWhatsApp
from .config import get_settings
from .automations import AutomationManager
from .feedback_scheduler import FeedbackScheduler

CONTRACT={'schema':1,'protocol':1}

def source_allowed(root,current=Path('/opt/codex-linux-control/current')):
    try:return current.resolve()==root.resolve() and json.loads((current/'system/android-runtime-contract.json').read_text())==CONTRACT
    except (OSError,ValueError):return False

def release_allowed(root,state):
    if not source_allowed(root):return False
    try:
        value=json.loads(state.read_text())
        return value.get('phase') not in {'switching','restarting','health-check','activating'} and value.get('state')!='activating'
    except (OSError,ValueError):return False

async def serve():
    root=Path(__file__).resolve().parents[1]
    if not source_allowed(root):return
    settings=get_settings();directory=settings.resolved_config_dir/'android-runtime'
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Obtain singleton before constructing the only Lifecycle. The empty server
    # remains outside the event loop until the runtime has been assigned.
    server=Server(directory,None,os.getuid(),release_id=root.parent.name)
    await server.start()
    controller=None
    try:
        authority=Authority(directory,admission_check=broker_acquisition_available)
        runtime=DurableLifecycle(settings.resolved_config_dir/'android-owned.marker',authority,broker=RuntimeBrokerRequests(authority))
        server.runtime=runtime
        collector=ScheduledWhatsApp(runtime)
        controller=Controller(runtime,server,collector)
        rollout=Path.home()/'.local/share/codex-linux-control/rollout/state.json'
        controller.release_allowed=lambda:release_allowed(root,rollout)
        feedback=FeedbackScheduler(settings.resolved_config_dir,AutomationManager(settings.resolved_config_dir/'automations.sqlite3'),None,
            lambda:controller.admitted() and time.monotonic()-server.last_pulse<30)
        runtime.feedback_worker=feedback.observation_worker
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,controller.stop_event.set)
        async def watch_source():
            while source_allowed(root):await asyncio.sleep(1)
            controller.stop_event.set()
        watch=asyncio.create_task(watch_source())
        try:await controller.run()
        finally:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):await watch
    finally:await server.close()

if __name__=='__main__':asyncio.run(serve())
