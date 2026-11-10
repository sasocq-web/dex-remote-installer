"""Candidate scoped source pipeline. Native lifecycle/runtime wiring still required.

All callbacks are trusted in-process adapters, never deserialized tool arguments.
Only IDs are public. This produces a private source capsule, not eligibility.
"""
import asyncio,hashlib,json,time
from pathlib import Path
from .feedback_source_mcp import SourceMcpCollector
from .feedback_source_receipts import SourceReceiptStore,hex64

PROJECT='project-96921b3806e0'

class SourcePipeline:
    def __init__(self,bundle,pins,directory,clock=time.time):
        self.bundle=Path(bundle);self.pins=dict(pins);self.directory=Path(directory);self.clock=clock
        path=self.bundle/'registry.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1000000:
            raise ValueError('source pipeline registry file')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=self.pins.get('registry.json'):
            raise ValueError('source pipeline registry pin')
        self.registry=json.loads(raw);self.locks={}
    def request(self,request):
        if (not isinstance(request,dict) or set(request)!={'event_id','dependency_id'}
            or not hex64(request['event_id']) or not isinstance(request['dependency_id'],str)
            or not self.registry['bindings'].get(request['event_id'],{}).get(request['dependency_id'])):
            raise ValueError('scoped fixed source IDs required')
    def binding(self,worker,request,workspace,thread):
        self.request(request)
        rows=[r for r in worker.call('resumption_inventory') if r.get('event_id')==request['event_id']]
        if len(rows)!=1:raise ValueError('source event outside current scope')
        row=rows[0];target=worker.routes.get(row.get('site'));fixed=self.registry['contracts'][request['event_id']]
        if (row.get('site')!='controles.sasocq.com' or row.get('state') not in {'blocked','validation_pending'}
            or not target or target.get('project_id')!=PROJECT):raise ValueError('source owner route')
        if workspace!='system' and (workspace!='project:'+PROJECT or thread!=target.get('thread_id')):
            raise ValueError('source canonical resident required')
        doc=json.loads(row.get('document') or '{}')
        if (row.get('revision')!=fixed['contract_revision'] or doc.get('source_report_id')!=fixed['source_report_id']
            or doc.get('owner_project')!=PROJECT or doc.get('resident')!=target.get('thread_id')
            or not isinstance(doc.get('required_dependency_ids'),list)
            or request['dependency_id'] not in doc['required_dependency_ids']):
            raise ValueError('source current contract changed')
        return dict(site=row['site'],project_id=PROJECT,resident=target['thread_id'],
                    source_report_id=fixed['source_report_id'],contract_revision=fixed['contract_revision'],
                    required_dependency_ids=doc['required_dependency_ids'])
    async def collect(self,worker,request,workspace,thread,turn,context_get,mcp_call,normalize,acknowledge=None):
        self.request(request)
        lock=self.locks.setdefault((workspace,thread),asyncio.Lock())
        if lock.locked():raise ValueError('source collection already active')
        async with lock:
            context=context_get()
            if (not isinstance(context,dict) or context.get('workspace')!=workspace
                or context.get('thread')!=thread or not turn or context.get('turn')!=turn):
                raise ValueError('source requires own active native context/turn')
            current=lambda candidate=None: context_get()==context and (candidate is None or candidate==context)
            binding=await asyncio.to_thread(self.binding,worker,request,workspace,thread)
            # Recheck reviewed collector/registry files before nonce or any browser call.
            collector=SourceMcpCollector(self.bundle,self.pins,mcp_call,current)
            collector.checked('normalize.py');collector.checked('semantic_engine.py')
            store=SourceReceiptStore(self.directory,self.registry,self.pins,self.clock)
            try:
                nonce=store.issue(request,context,current)
                capture=await collector.capture(request,nonce)
                if await asyncio.to_thread(self.binding,worker,request,workspace,thread)!=binding:
                    raise ValueError('source contract changed during capture')
                receipt=store.seal(nonce,capture,current)
                verified=store.consume(receipt,request,context,capture,current)
                # Coverage comes from the exact capture signed by System, never
                # from a second caller-supplied summary or a normalizer boolean.
                coverage={i:p['planning_coverage'] for i,p in capture['payloads'].items()
                          if isinstance(p,dict) and 'planning_coverage' in p}
                # Future adapter must verify actual immutable runtime/normalizer
                # hashes before running. They are independently bound in receipt.
                raw=await normalize(request,capture,coverage,dict(self.pins),dict(verified))
                if await asyncio.to_thread(self.binding,worker,request,workspace,thread)!=binding:
                    raise ValueError('source contract changed during normalization')
                reference=store.commit_capsule(verified,raw,current)
                if acknowledge is not None:
                    # Acknowledgment may release only this processing spool,
                    # after the private source capsule is durable and bound.
                    if current() is not True:raise ValueError('source context changed before acknowledgment')
                    await acknowledge(reference)
                return dict(schema='canonical-source-manifest-v1',**request,
                    source_report_id=binding['source_report_id'],contract_revision=binding['contract_revision'],
                    observed_at=verified['observed_at'],private_capsule_ref=reference,parser_pins=dict(self.pins),
                    manifest=json.loads(receipt['payload'])['manifest'])
            finally:store.close()
