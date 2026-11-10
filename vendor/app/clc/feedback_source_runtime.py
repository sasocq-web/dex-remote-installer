"""Private protocol for the fixed reviewed processing image, not an attestor.

The transport is an internal callback bound by System, never a tool argument.
It must launch the immutable image under the audited rootless budget, private
stdio and fixed readonly database route. No production transport is wired yet.
"""
import hashlib,json,math,re,time
from .feedback_source_receipts import hex64

IMAGE='sha256:43e3c5577ecb6dd92c79770f2b22bf52bf866ed98460e70bfa93ef79fdd3e0ee'
TRUST='processing_only_requires_System_provenance_verification'
CAPSULE_FIELDS={'pdc','equipment','equipment_legacy','materials','planning_dates','manifest'}
REASONS={'verified','source_capture_stale','specific_source_conditions_unresolved'}

def wire(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
def digest(raw):return hashlib.sha256(raw).hexdigest()
def unique(pairs):
    out={}
    for k,v in pairs:
        if k in out:raise ValueError('duplicate runtime field')
        out[k]=v
    return out
def decode(raw,limit):
    if not isinstance(raw,bytes) or not 0<len(raw)<=limit:raise ValueError('private runtime output bound')
    try:return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite runtime field')))
    except (UnicodeError,ValueError,RecursionError):raise ValueError('private runtime JSON') from None

class SourceRuntimeProtocol:
    def __init__(self,transport,clock=time.time):self.transport=transport;self.clock=clock
    async def acknowledge(self,reference):
        # Called only after SourceReceiptStore fsync+journal commit succeeds.
        await self.transport.acknowledge(reference)
    def envelope(self,request,field,payload,reference,coverage):
        if (not isinstance(request,dict) or set(request)!={'event_id','dependency_id'} or not hex64(request['event_id'])
            or not isinstance(request['dependency_id'],str) or re.fullmatch('[a-z][a-z0-9_]{0,100}',request['dependency_id']) is None
            or not isinstance(reference,str) or re.fullmatch('urn:sasocq:[a-z0-9][a-z0-9:._-]{15,190}',reference) is None
            or not isinstance(coverage,dict)):raise ValueError('private runtime bound request')
        raw=wire(payload)
        if len(raw)>(16000000 if field=='capture' else 4000000):raise ValueError('private runtime payload bound')
        sha=digest(raw)
        body=wire(dict(**request,**{field:payload,field+'_sha256':sha},source_provenance=dict(
            schema='system-private-source-reference-v1',receipt_ref=reference,payload_sha256=sha,coverage=coverage)))
        if len(body)>16065536:raise ValueError('private runtime wire bound')
        return body,sha
    async def normalize(self,request,capture,coverage,pins,verified):
        raw=wire(capture)
        if (pins.get('runtime')!=IMAGE.removeprefix('sha256:') or not isinstance(verified,dict)
            or verified.get('capture_sha256')!=digest(raw) or not hex64(verified.get('nonce'))
            or verified.get('evidence_ref')!='urn:sasocq:private-source-receipt:'+verified['nonce']):
            raise ValueError('private runtime verified capture binding')
        body,_=self.envelope(request,'capture',capture,verified['evidence_ref'],coverage)
        out=decode(await self.transport('normalize',body,IMAGE),4065536)
        if (not isinstance(out,dict) or set(out)!={'ok','stage','trust_state','capsule_sha256','capsule'}
            or out['ok'] is not True or out['stage']!='normalize' or out['trust_state']!=TRUST
            or not isinstance(out['capsule'],dict) or set(out['capsule'])!=CAPSULE_FIELDS):
            raise ValueError('private normalizer response')
        capsule=wire(out['capsule'])
        if len(capsule)>4000000 or digest(capsule)!=out['capsule_sha256']:
            raise ValueError('private normalizer digest')
        # SourceReceiptStore separately checks exact signed manifest and bytes.
        return capsule
    async def evaluate(self,request,raw,reference,binding):
        capsule=decode(raw,4000000)
        if not isinstance(capsule,dict) or set(capsule)!=CAPSULE_FIELDS:raise ValueError('private capsule schema')
        manifest=capsule['manifest']
        if (not isinstance(manifest,list) or not manifest or any(not isinstance(r,dict)
            or type(r.get('observed_at')) not in {int,float} or not math.isfinite(r['observed_at']) for r in manifest)):
            raise ValueError('private source observation timestamps required')
        source_at=min(r['observed_at'] for r in manifest)
        body,sha=self.envelope(request,'capsule',capsule,reference,{})
        before=self.clock();out=decode(await self.transport('evaluate',body,IMAGE),65536);now=self.clock()
        if (not isinstance(out,dict) or set(out)!={'ok','stage','trust_state','capsule_sha256','result_sha256','result'}
            or out['ok'] is not True or out['stage']!='evaluate' or out['trust_state']!=TRUST
            or out['capsule_sha256']!=sha or not isinstance(out['result'],dict)
            or digest(wire(out['result']))!=out['result_sha256']):raise ValueError('private evaluator response')
        result=out['result']
        if (set(result)!={'event_id','dependency_id','source_report_id','contract_revision','observed_at','semantic_reason','diagnostic'}
            or any(result.get(k)!=v for k,v in request.items())
            or type(result['source_report_id']) is not int or result['source_report_id']!=binding['source_report_id']
            or result['contract_revision']!=binding['contract_revision']
            or type(result['observed_at']) not in {int,float} or result['observed_at']!=source_at
            or not all(0<=now-r['observed_at']<=300 for r in manifest)
            or not 0<=now-before<=30 or result['semantic_reason'] not in REASONS
            or not isinstance(result['diagnostic'],dict)):
            raise ValueError('private evaluator current contract/time/enum')
        # No positive observation here. System must still recheck CAS and all
        # source receipt/cache freshness, then map this fixed enum in its reader.
        return dict(result)
