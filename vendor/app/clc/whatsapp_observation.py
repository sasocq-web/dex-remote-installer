"""Review-only complementary official-UI observation envelope.

Pure signing/verification functions: no writer, transport, runtime, UI call or DB.
The resident must own the HMAC key and obtain LeaseContext from its authenticated
private authority/peer, NOT request bodies, booleans or untrusted tool output.
A signature proves provenance/integrity under that trust boundary, not UI truth.
"""
from dataclasses import dataclass
import datetime as dt
import hashlib,hmac,json,re
from zoneinfo import ZoneInfo

ZERO='0'*64
HEX=re.compile(r'[a-f0-9]{64}')


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def seal(payload,key):
    if not isinstance(key,bytes) or len(key)<32:raise ValueError('private_signing_key_required')
    raw=canonical(payload)
    if len(raw)>32768:raise ValueError('observation_size_limit')
    return {'payload':payload,'mac':hmac.new(key,b'SASOCQ-OFFICIAL-UI-OBSERVATION-v1\0'+raw,hashlib.sha256).hexdigest()}


@dataclass(frozen=True)
class LeaseContext:
    lease_id:str
    runtime_generation:str
    group:str
    now_ms:int
    lease_expires_ms:int
    next_sequence:int
    previous_digest:str
    source_id:str
    source_sha256:str
    seen_capture_ids:frozenset=frozenset()


@dataclass(frozen=True)
class VerifiedObservation:
    payload_bytes:bytes
    envelope_digest:str
    next_sequence:int
    # Always complementary, never an export participant/canonical authorization.
    binding:str='ambiguous'
    may_export:bool=False


def _verify(envelope,key,context):
    """Caller must atomically CAS next_sequence/previous_digest under lease lock.

    Without durable CAS and private runtime/lease verification, do not persist or
    transport. This candidate deliberately has no operational activation hook.
    """
    if not isinstance(context,LeaseContext):raise ValueError('private_lease_context_required')
    if not isinstance(envelope,dict) or set(envelope)!= {'payload','mac'}:raise ValueError('envelope_shape')
    payload=envelope['payload'];signed=seal(payload,key)
    if not isinstance(envelope['mac'],str) or not hmac.compare_digest(envelope['mac'],signed['mac']):raise ValueError('observation_authentication_failed')
    fields={'schema','acquisition','capture_id','lease_id','runtime_generation','group','sequence','previous_digest','captured_at_ms','expires_at_ms','device_day','timezone','notice_day','frames','notice','source'}
    if not isinstance(payload,dict) or set(payload)!=fields:raise ValueError('observation_shape')
    if payload['schema']!=1 or type(payload['schema']) is not int or payload['acquisition']!='android_native_official_ui':raise ValueError('observation_version')
    if payload['capture_id'] in context.seen_capture_ids:raise ValueError('capture_id_already_recorded')
    if not re.fullmatch(r'[a-f0-9]{32}',payload['capture_id']):raise ValueError('capture_id_invalid')
    if (payload['lease_id'],payload['runtime_generation'],payload['group'])!=(context.lease_id,context.runtime_generation,context.group):raise ValueError('lease_runtime_group_mismatch')
    if not HEX.fullmatch(payload['runtime_generation']):raise ValueError('runtime_identity_invalid')
    if type(payload['sequence']) is not int or payload['sequence']!=context.next_sequence or payload['sequence']<1:raise ValueError('observation_replay_or_sequence_gap')
    if payload['previous_digest']!=context.previous_digest or not HEX.fullmatch(payload['previous_digest']):raise ValueError('observation_chain_mismatch')
    start,end=payload['captured_at_ms'],payload['expires_at_ms']
    if type(start) is not int or type(end) is not int or not(start<=context.now_ms<=end<=context.lease_expires_ms) or not(0<end-start<=120000):raise ValueError('capture_lease_expired_or_unbounded')
    if payload['timezone']!='America/Sao_Paulo':raise ValueError('device_timezone_unproved')
    today=dt.datetime.fromtimestamp(start/1000,ZoneInfo(payload['timezone'])).date()
    if dt.date.fromisoformat(payload['device_day'])!=today or dt.date.fromisoformat(payload['notice_day'])>today:raise ValueError('device_day_inconsistent')
    frames=payload['frames']
    if not isinstance(frames,list) or len(frames)!=3:raise ValueError('capture_return_sequence_required')
    for order,item in enumerate(frames):
        if not isinstance(item,dict) or set(item)!= {'role','sha256','sequence'} or item['role']!=('before','after','return')[order] or item['sequence']!=order or type(item['sequence']) is not int or not HEX.fullmatch(item['sha256']):raise ValueError('frame_provenance_invalid')
    # before and after must be distinct captures. Return equality is allowed.
    if frames[0]['sha256']==frames[1]['sha256']:raise ValueError('stationary_capture_not_certified')
    n=payload['notice']
    from .whatsapp_notice import AdminDeletedNotice
    expected=set(AdminDeletedNotice.__dataclass_fields__)
    if not isinstance(n,dict) or set(n)!=expected:raise ValueError('notice_shape')
    if n['kind']!='deleted_by_admin' or n['visibility']!='full' or n['clock_role']!='DISPLAYED_MESSAGE_CLOCK' or n['source_binding']!='unresolved':raise ValueError('notice_role_or_visibility_unproved')
    for k in ('original_display','original_phone','actor_display','displayed_clock'):
        if not isinstance(n[k],str) or not n[k] or len(n[k])>512 or any(ord(c)<32 for c in n[k]):raise ValueError('notice_value_invalid')
    if not re.fullmatch(r'\+[\d ()-]+',n['original_phone']) or not 10<=len(re.sub(r'\D','',n['original_phone']))<=15:raise ValueError('original_sender_phone_unproved')
    if not isinstance(n['meaningful_box'],(tuple,list)) or len(n['meaningful_box'])!=4 or any(type(x) is not int for x in n['meaningful_box']):raise ValueError('notice_geometry_missing')
    src=payload['source']
    if not isinstance(src,dict) or set(src)!= {'id','zip_sha256','candidate_ordinals_zero','binding','exact_body_matches'}:raise ValueError('source_link_shape')
    if (src['id'],src['zip_sha256'])!=(context.source_id,context.source_sha256) or not HEX.fullmatch(src['zip_sha256']):raise ValueError('verified_export_reference_mismatch')
    ordinals=src['candidate_ordinals_zero']
    if not isinstance(ordinals,list) or len(ordinals)<2 or len(ordinals)>32 or any(type(i) is not int or i<0 for i in ordinals) or ordinals!=sorted(set(ordinals)):raise ValueError('ambiguous_source_range_required')
    if src['binding']!='ambiguous' or src['exact_body_matches']!=0 or type(src['exact_body_matches']) is not int:raise ValueError('unproved_exact_source_binding')
    raw=canonical(payload)
    digest=hashlib.sha256(canonical(envelope)).hexdigest()
    return VerifiedObservation(raw,digest,context.next_sequence+1)


def verify(envelope,key,context):
    try:
        return _verify(envelope,key,context)
    except (TypeError,KeyError,AttributeError,OverflowError,OSError):
        raise ValueError('invalid_observation') from None


def native_triplet(xmls,group,reference_day):
    """Derive payload evidence from three official-channel XML responses in memory.

    Transport authenticity must be established by the resident private runtime
    before calling this. This function cannot authenticate a supplied XML string.
    No raw XML or display values are written/logged; no source linkage is inferred.
    """
    from .whatsapp_frames import capture
    from .whatsapp_cursor import page
    from .whatsapp_notice import physical_notice_pair, AdminDeletedNotice
    from dataclasses import asdict
    if not isinstance(xmls,(tuple,list)) or len(xmls)!=3:raise ValueError('three_native_frames_required')
    frames=[capture(xml,group,reference_day,page) for xml in xmls]
    notices=[];days=[]
    for frame in frames:
        found=[(i,row) for i,row in enumerate(frame.rows) if row.event[0]=='notice' and isinstance(row.event[1],AdminDeletedNotice)]
        if len(found)!=1 or found[0][0]!=len(frame.rows)-1:raise ValueError('unique_terminal_notice_required')
        row=found[0][1];notices.append(row)
        same_row_days=[e[1] for e in row.prefix if e[0]=='day']
        if len(same_row_days)!=1:raise ValueError('same_occurrence_day_required')
        days.append(same_row_days[0])
    if len(set(days))!=1 or len({f.viewport for f in frames})!=1:raise ValueError('notice_day_or_viewport_changed')
    a,b,r=[row.event[1] for row in notices]
    if a.visibility!='full' or r!=a or notices[0].box!=notices[2].box:raise ValueError('notice_return_not_restored')
    physical_notice_pair(a,b,notices[0].box,notices[1].box,frames[0].viewport)
    # Still not a global occurrence ID or source match; keys remain None.
    return {'notice':asdict(a),'notice_day':days[0],
            'frames':[{'role':role,'sequence':i,'sha256':hashlib.sha256(xml.encode()).hexdigest()}
                      for i,(role,xml) in enumerate(zip(('before','after','return'),xmls))]}
