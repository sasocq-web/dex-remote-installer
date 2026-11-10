"""ONE bounded official navigation alternative, inside the original cursor/lease.

No export, new runtime/authority, persistent certificate or public JSON option.
Search snippets propose a click only. Full primary records + original canonical
uniqueness constrain a private, single-use date observation. Raw frames retained.
"""
import asyncio
import math
import datetime as dt
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass,replace
from .whatsapp_collector import WA,GROUPS,ExportError,OfficialUI,nodes,center,search_value
from .whatsapp_author import _read,_by_id,_target,_empty_composer
from .whatsapp_cursor import bounds
from .collector_evidence import canonical_tokens,CanonicalGap,key,compatible_window,Observation,reconcile,token_stamp

_SEAL=object()


def eligible_tail(code,frame):
    return (code=='IPS' and not frame.opaque_prefixes and len(frame.rows)>=2
            and all(r.key is not None and r.key[0]=='text' and r.event[0] in {'text','clipped'}
                    for r in frame.rows)
            and frame.rows[-1].event[0]=='text')


def _plan(baseline):
    old=canonical_tokens(baseline)
    if any(token_stamp(a)>token_stamp(b) for a,b in zip(old,old[1:])):
        raise ExportError('lookup_canonical_unordered')
    if len(old)<2 or any(isinstance(t,CanonicalGap) for t in old[-2:]):
        raise ExportError('lookup_canonical_tail_gap')
    pair=old[-2:]
    # Original canonical sequence, including ALL gaps/occurrences, decides uniqueness.
    if sum(compatible_window(old[i:i+2],pair) for i in range(len(old)-1))!=1:
        raise ExportError('lookup_canonical_pair_ambiguous')
    for record,token in zip(baseline[-2:],pair):
        if key(record)!=token or old.count(token)!=1:
            raise ExportError('lookup_canonical_text_ambiguous')
    if pair[0][0][:10]!=pair[1][0][:10] or pair[0][0]>pair[1][0]:
        raise ExportError('lookup_canonical_pair_day_or_order')
    # Canonical key normalization remains ORIGINAL for uniqueness only. The
    # actual navigation/primary-body comparison uses literal source bytes-text.
    pair=tuple((token[0],token[1],record['body']) for token,record in zip(pair,baseline[-2:]))
    bodies=[r['body'] for r in baseline]
    if any(bodies.count(t[2])!=1 for t in pair):
        raise ExportError('lookup_canonical_body_ambiguous')
    words=re.findall(r'(?<!\w)[A-Za-z]{4,32}(?!\w)',pair[0][2])
    for word in words:
        pattern=re.compile(r'(?<!\w)'+re.escape(word)+r'(?!\w)')
        if sum(bool(pattern.search(b)) for b in bodies)==1 and len(pattern.findall(pair[0][2]))==1:
            return tuple(pair),word
    raise ExportError('lookup_no_complete_unique_ASCII_word')


def plan(baseline):
    try:return _plan(baseline)
    except (ValueError,KeyError,TypeError):
        raise ExportError('lookup_canonical_unsupported') from None


def eligible_source(baseline):
    # Invalid proposals do not veto any existing cursor proof/navigation.
    try:plan(baseline)
    except ExportError:return False
    return True


def _plain(tree):
    _empty_composer(tree)
    forbidden=('search_fragment','search_view_edit_text','search_input',
               'quoted_message_preview_container','action_mode_bar')
    if any(_by_id(tree,rid) for rid in forbidden):
        raise ExportError('lookup_not_plain_conversation')


def _field(tree):
    fields=_by_id(tree,'search_input')
    if (len(fields)!=1 or fields[0].get('focused')!='true' or fields[0].get('enabled')!='true'
            or fields[0].get('class')!='android.widget.EditText' or fields[0].get('package')!=WA):
        raise ExportError('lookup_search_not_focused')
    if len(_by_id(tree,'search_fragment'))!=1:
        raise ExportError('lookup_search_scope_ambiguous')
    if fields[0] not in list(_by_id(tree,'search_fragment')[0].iter('node')):
        raise ExportError('lookup_search_field_outside_scope')
    return fields[0]


@dataclass(frozen=True)
class _Result:
    fingerprint:bytes
    day:dt.date
    point:tuple
    query:str


def result_proposal(xml,query,code,reference_day):
    nodes(xml);tree=ET.fromstring(xml);field=_field(tree)
    if search_value(field.get('text',''))!=query:
        raise ExportError('lookup_query_echo_mismatch')
    scope=_by_id(tree,'search_fragment')[0];listing=_by_id(scope,'result_list')
    if len(listing)!=1:raise ExportError('lookup_results_missing')
    found=[]
    # Native result's common container has header+content as DIRECT children.
    # Never associate a date from a neighbouring row or from the whole overlay.
    for container in listing[0].iter('node'):
        header=[n for n in container if n.get('resource-id')==WA+':id/search_message_container_header']
        content=[n for n in container if n.get('resource-id')==WA+':id/search_message_container_content']
        if len(header)!=1 or len(content)!=1:continue
        names=_by_id(header[0],'conversations_row_contact_name');dates=_by_id(header[0],'conversations_row_date');snippets=_by_id(content[0],'search_message_text_content')
        if len(names)!=1 or names[0].get('text')!=GROUPS[code][1]:continue
        # A result without date is NOT a date candidate (still a visible result).
        if len(dates)!=1 or len(snippets)!=1:continue
        if not re.search(r'(?<!\w)'+re.escape(query)+r'(?!\w)',snippets[0].get('text','')):continue
        value=dates[0].get('text','')
        try:
            if not re.fullmatch(r'\d{1,2}/\d{1,2}/\d{2}(?:\d{2})?',value):raise ValueError()
            day=dt.datetime.strptime(value,'%m/%d/%Y' if len(value.split('/')[-1])==4 else '%m/%d/%y').date()
        except ValueError:continue
        if reference_day is None or day>reference_day or abs(day.year-reference_day.year)>1:continue
        box,b= bounds(container),bounds(listing[0])
        if not (b[0]<=box[0]<box[2]<=b[2] and b[1]<=box[1]<box[3]<=b[3]):continue
        found.append(_Result(ET.tostring(container),day,center(snippets[0].attrib),query))
    if len(found)!=1:raise ExportError('lookup_visible_date_candidate_ambiguous')
    return found[0] # NOT global/exhaustive uniqueness, only one proposed clickable row.


@dataclass(frozen=True)
class _DayCertificate:
    seal:object
    scope:object
    ui:object
    frame:object
    row_index:int
    day:dt.date
    result:_Result
    binding:tuple
    used:bool=False


def _primary_layout(row):
    slots=_by_id(row,'conversation_text_row')
    if len(slots)!=1:raise ExportError('lookup_primary_layout_unproved')
    if (len(_by_id(slots[0],'message_text'))!=1 or len(_by_id(slots[0],'date'))!=1
            or _by_id(row,'message_text')!=_by_id(slots[0],'message_text')
            or _by_id(row,'date')!=_by_id(slots[0],'date')):
        raise ExportError('lookup_primary_field_scope_unproved')
    for n in row.iter('node'):
        if 'quoted' in n.get('resource-id','').split('/')[-1] and _by_id(n,'name_in_group_tv'):
            raise ExportError('lookup_primary_sender_in_quote')


def bind_day(scope,ui,raw,xml,result,pair):
    tree,viewport=_read(xml,'IPS');_plain(tree)
    body,stamp,sender=pair[0][2],pair[0][0],pair[0][1]
    clock=dt.datetime.fromisoformat(stamp).strftime('%H:%M')
    # _target uses native clock format; compare semantically EXACT minute, no offset.
    candidates=[]
    for i,row in enumerate(raw.rows):
        if row.event[0]!='text' or row.event[3]!=body:continue
        _,author,display,_=row.event
        fmt='%I:%M %p' if re.fullmatch(r'\d{1,2}:\d{2} [AP]M',' '.join(display.split()),re.I) else '%H:%M'
        try:actual=dt.datetime.strptime(' '.join(display.split()).upper(),fmt).time()
        except ValueError:continue
        if actual!=dt.datetime.fromisoformat(stamp).time() or author!=sender:continue
        target,_=_target(tree,body,display,viewport)
        _primary_layout(target)
        candidates.append(i)
    # Full PRIMARY body/query once in opened list. Quoted_text never qualifies.
    primary=_by_id(next(n for n in tree.iter('node') if n.get('resource-id')=='android:id/list'),'message_text')
    if any(re.search(r'(?<!\w)'+re.escape(result.query)+r'(?!\w)',n.get('text',''))
           for n in _by_id(tree,'quoted_text')):
        raise ExportError('lookup_query_in_quoted_child_ambiguous')
    if len(candidates)!=1 or sum(bool(re.search(r'(?<!\w)'+re.escape(result.query)+r'(?!\w)',n.get('text',''))) for n in primary)!=1:
        raise ExportError('lookup_opened_primary_identity_ambiguous')
    index=candidates[0]
    if result.day.isoformat()!=stamp[:10]:raise ExportError('lookup_observed_day_source_conflict')
    # The next complete primary occurrence must be the canonical partner. Its
    # missing author is acquired fresh by Reply; never filled from this pair.
    if index+1>=len(raw.rows):raise ExportError('lookup_opened_pair_missing')
    partner=raw.rows[index+1]
    if partner.event[0]!='text' or partner.event[3]!=pair[1][2]:raise ExportError('lookup_opened_pair_missing')
    _,_,display,_=partner.event
    target,_=_target(tree,pair[1][2],display,viewport)
    _primary_layout(target)
    fmt='%I:%M %p' if re.fullmatch(r'\d{1,2}:\d{2} [AP]M',' '.join(display.split()),re.I) else '%H:%M'
    try:partner_clock=dt.datetime.strptime(' '.join(display.split()).upper(),fmt).time()
    except ValueError:raise ExportError('lookup_opened_clock_invalid') from None
    if partner_clock!=dt.datetime.fromisoformat(pair[1][0]).time():raise ExportError('lookup_opened_clock_conflict')
    return _DayCertificate(_SEAL,scope,ui,raw,index,result.day,result,
                           (id(scope),id(ui),raw,index,result.day,result))


def consume_day(certificate,scope,ui,raw,enriched):
    if (not isinstance(certificate,_DayCertificate) or certificate.seal is not _SEAL
            or certificate.used or certificate.scope is not scope or certificate.ui is not ui
            or certificate.binding!=(id(scope),id(ui),certificate.frame,certificate.row_index,certificate.day,certificate.result)
            or certificate.frame!=raw or enriched.viewport!=raw.viewport
            or len(enriched.rows)!=len(raw.rows) or enriched.button!=raw.button
            or enriched.opaque_prefixes!=raw.opaque_prefixes
            or enriched._opaque_join is not raw._opaque_join):
        raise ExportError('lookup_day_certificate_invalid')
    for a,b in zip(raw.rows,enriched.rows):
        if (a.key,a.box,a.prefix,a.visible,a.edge_text_sliver)!=(b.key,b.box,b.prefix,b.visible,b.edge_text_sliver):raise ExportError('lookup_day_frame_changed')
        if a.event!=b.event and not (a.event[0]==b.event[0]=='text' and a.event[1] is None and a.event[2:]==b.event[2:] and b.event[1]):
            raise ExportError('lookup_day_frame_changed')
    object.__setattr__(certificate,'used',True)
    i=certificate.row_index;row=enriched.rows[i];day=('day',certificate.day.isoformat())
    if any(e!=day for e in row.prefix):raise ExportError('lookup_day_prefix_conflict')
    return replace(enriched,rows=enriched.rows[:i]+(replace(row,prefix=(day,)),)+enriched.rows[i+1:])


def _forward_shape(frame,day,first=False):
    if frame.opaque_prefixes:raise ExportError('lookup_forward_opaque')
    seen_text=False
    for i,row in enumerate(frame.rows):
        if any(e!=('day',day) for e in row.prefix):raise ExportError('lookup_forward_day_changed')
        if row.key is not None and row.key[0]=='text' and row.event[0] in {'text','clipped'}:
            seen_text=True;continue
        # Certified OUTER text slivers remain raw and go through ACTIVE seam
        # normalization. No trimming/skipping by this lookup adapter.
        if row.edge_text_sliver and i in {0,len(frame.rows)-1}:continue
        if first and not seen_text and row.event==('clipped',) and row.key and row.key[0]=='barrier':continue
        raise ExportError('lookup_forward_internal_barrier')


async def _drain(task):
    cancelled=False
    while True:
        try:value=await asyncio.shield(task);break
        except asyncio.CancelledError:
            if task.cancelled():raise
            cancelled=True
    if cancelled:raise asyncio.CancelledError()
    return value


async def lookup(cursor,code,baseline,tail,deadline):
    """One navigation attempt, same original UI call/binding and ABS deadline."""
    from .whatsapp_frames import capture,prepend_frames,suffix_records,same_tail
    if (isinstance(deadline,bool) or not isinstance(deadline,(int,float))
            or not math.isfinite(deadline)):
        raise ExportError('lookup_deadline_invalid')
    if not eligible_tail(code,tail):raise ExportError('lookup_initial_tail_ineligible')
    pair,query=plan(baseline);scope=object();limit=deadline-30
    def check():
        if time.monotonic()>=limit:raise ExportError('lookup_context_deadline_exceeded')
    async def call(args):
        check()
        try:value=await cursor.ui.call(args)
        except asyncio.CancelledError:raise
        except Exception:raise ExportError('lookup_transport_uncertain') from None
        check();return value
    nav=OfficialUI(call);started=False;answer=None;raws=[]
    try:
        check();started=True
        await nav.home() # open alone is NEVER a home/focus proof.
        await nav.tap(rid=WA+':id/search_bar_inner_layout',package=WA)
        field=await nav.wait_search_empty()
        if field.get('focused')!='true':
            x,y=center(field);await call({'operation':'tap','x':x,'y':y})
        empty=await call({'operation':'ui'});t=ET.fromstring(empty['output']);f=_field(t)
        if search_value(f.get('text','')) not in ('','Search…'):raise ExportError('lookup_input_not_empty')
        await call({'operation':'type','text':query})
        a=await call({'operation':'ui'});proposal=result_proposal(a['output'],query,code,cursor.reference_day)
        b=await call({'operation':'ui'});again=result_proposal(b['output'],query,code,cursor.reference_day)
        if proposal!=again:raise ExportError('lookup_result_changed_before_click')
        x,y=proposal.point;await call({'operation':'tap','x':x,'y':y})
        opened=await call({'operation':'ui'});raw=capture(opened['output'],code,cursor.reference_day,cursor.parser)
        certificate=bind_day(scope,cursor.ui,raw,opened['output'],proposal,pair)
        cursor.trace('lookup_opened_anchor',raw,visible_date_proposals=1)
        i=certificate.row_index
        # Preserve author acquisition implementation and original ABS limit.
        cursor.author_deadline=limit
        check();enriched=await cursor.enrich(code,raw,indices=(i,i+1));check()
        # enrich acquired only missing authors, then EXACT fresh==raw. Also
        # revalidate the explicit primary sender/clock pair against original source.
        for row,token in zip(enriched.rows[i:i+2],pair):
            if row.event[1]!=token[1]:raise ExportError('lookup_explicit_sender_conflict')
        proof=consume_day(certificate,scope,cursor.ui,raw,enriched)
        day=certificate.day.isoformat();_forward_shape(raw,day,True)
        if len(raw.rows)>200:raise ExportError('lookup_forward_row_limit')
        raws=[raw];proofs=[proof];current=raw
        for stepno in range(65):
            check()
            if same_tail(tail,current):break
            if stepno==64:raise ExportError('lookup_forward_step_limit')
            v=current.viewport;x=(v[0]+v[2])//2;y=(v[1]+v[3])//2
            await call({'operation':'swipe','x':x,'y':y,'x2':x,'y2':y-60,'duration_ms':500})
            output=await call({'operation':'ui'});new=capture(output['output'],code,cursor.reference_day,cursor.parser)
            tree,_=_read(output['output'],code);_plain(tree);_forward_shape(new,day)
            if new==current:raise ExportError('lookup_forward_stationary_without_tail')
            prepend_frames(current,new,max_shift=120) # ACTIVE local physical proof
            raws.append(new);proofs.append(new);current=new
            cursor.trace('lookup_forward_snapshot',new,raw_frame_count=len(raws),requested_step=60)
            if sum(len(f.rows) for f in raws)>200:raise ExportError('lookup_forward_raw_row_limit')
        assembled=proofs[-1]
        for older in reversed(proofs[:-1]):
            check();assembled=prepend_frames(older,assembled,max_shift=120)
            if len(assembled.rows)>200:raise ExportError('lookup_forward_row_limit')
        records,excluded=suffix_records(assembled.events());answer=reconcile(code,baseline,Observation(code,records,True,True,True))
        answer['excluded_prefix_events']=excluded
        check()
    finally:
        if started:
            async def restore():
                # No guessed calibrated gesture. Bound existing native navigation
                # restores original group+bottom; exact Frame is REQUIRED.
                for _ in range(3):
                    out=await cursor.ui.call({'operation':'ui'})
                    try:
                        t,_=_read(out['output'],code);_plain(t);break
                    except ExportError:await cursor.ui.call({'operation':'back'})
                else:raise ExportError('lookup_cleanup_plain_group_unproven')
                final=await cursor.bottom(code)
                if final!=tail:raise ExportError('lookup_tail_frame_not_exact')
            try:await _drain(asyncio.create_task(restore()))
            except asyncio.CancelledError:raise
            except ExportError:raise
            except Exception:raise ExportError('lookup_cleanup_failed') from None
    if time.monotonic()>=limit:raise ExportError('lookup_context_deadline_exceeded')
    return answer,tuple(raws)
