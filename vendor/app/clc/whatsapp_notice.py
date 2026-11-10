"""Evidence-limited official accessibility notice layout, not participant text.

Only the native demonstrated English administrator-deletion layout is admitted.
Values are private observations: never log/reuse as export sender or deletion time.
No UI action, authentication, canonical update or source binding occurs here.
"""
from dataclasses import dataclass, field
import re

WA='com.whatsapp.w4b'
PREFIX=WA+':id/'
ALLOWED={'conversation_row_date_divider','group_profile_pic','main_layout',
 'conversation_row_name_in_group_name_and_role_container','name_in_group',
 'name_in_group_tv','secondary_name_in_group_tv','icon','message_text','date_wrapper','date'}
REQUIRED={'main_layout','conversation_row_name_in_group_name_and_role_container',
 'name_in_group','name_in_group_tv','secondary_name_in_group_tv','icon','message_text','date_wrapper','date'}


def box(node):
    m=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
    if not m:raise ValueError('notice_bounds_invalid')
    result=tuple(map(int,m.groups()))
    if result[0]>=result[2] or result[1]>=result[3]:raise ValueError('notice_bounds_invalid')
    return result


@dataclass(frozen=True)
class AdminDeletedNotice:
    original_display: str
    original_phone: str
    actor_display: str
    displayed_clock: str
    visibility: str
    meaningful_box: tuple
    kind: str='deleted_by_admin'
    clock_role: str='DISPLAYED_MESSAGE_CLOCK'
    source_binding: str='unresolved'

    def identity(self):
        # Private tuple for explicit physical-pair review, never a text anchor.
        return self.kind,self.original_display,self.original_phone,self.actor_display,self.displayed_clock


def parse_notice(row,viewport):
    """None = not a supported layout; ambiguity always preserves outer barrier."""
    try:
        if (row.get('resource-id','')!='' or row.get('package')!=WA
                or row.get('class')!='android.view.ViewGroup'
                or row.get('long-clickable')!='true'):return None
        grouped={}
        for n in row.iter('node'):
            if n.get('visible-to-user')=='false':return None
            rid=n.get('resource-id','')
            if n.get('package')!=WA:return None
            if rid:
                if not rid.startswith(PREFIX) or rid[len(PREFIX):] not in ALLOWED:return None
                grouped.setdefault(rid[len(PREFIX):],[]).append(n)
            elif n.get('text') or n.get('content-desc'):return None
        if not REQUIRED<=grouped.keys() or any(len(v)!=1 for v in grouped.values()):return None
        def text(name):
            n=grouped[name][0]
            if n.get('class')!='android.widget.TextView':raise ValueError('notice_text_class')
            v=n.get('text','')
            if not v or any(ord(c)<32 for c in v):raise ValueError('notice_text_invalid')
            return v
        if grouped['icon'][0].get('class')!='android.widget.ImageView':return None
        display,phone,body,clock=map(text,('name_in_group_tv','secondary_name_in_group_tv','message_text','date'))
        # Secondary field is ORIGINAL sender phone, never deletion actor identity.
        if not re.fullmatch(r'\+[\d ()-]+',phone) or not 10<=len(re.sub(r'\D','',phone))<=15:return None
        match=re.fullmatch(r'This message was deleted by admin (\S(?:[^\r\n]*\S)?)',body)
        if not match:return None
        actor=match[1]
        if (actor!=actor.strip() or 'This message was deleted' in actor
                or any(marker in actor for marker in ('<','>','\u2028','\u2029'))):return None
        clean=' '.join(clock.split())
        import datetime as dt
        fmt='%I:%M %p' if re.fullmatch(r'\d{1,2}:\d{2} [AP]M',clean,re.I) else '%H:%M'
        dt.datetime.strptime(clean.upper(),fmt)
        # All semantic and visible icon descendants; container padding is irrelevant.
        meaningful=[n for n in row.iter('node') if n.get('text') or n.get('content-desc') or n.get('resource-id')==PREFIX+'icon']
        rectangles=[box(n) for n in meaningful]
        rb=box(row)
        if any(not (rb[0]<=b[0]<b[2]<=rb[2] and rb[1]<=b[1]<b[3]<=rb[3]) for b in rectangles):return None
        visible=all(viewport[0]<=b[0]<b[2]<=viewport[2] and viewport[1]<b[1]<b[3]<viewport[3] for b in rectangles)
        merged=(min(b[0] for b in rectangles),min(b[1] for b in rectangles),max(b[2] for b in rectangles),max(b[3] for b in rectangles))
        return AdminDeletedNotice(display,phone,actor,clock,'full' if visible else 'clipped',merged)
    except (ValueError,KeyError,TypeError):return None


def physical_notice_pair(before,after,before_box,after_box,viewport,max_shift=48):
    """Review aid only. No frame key/canonical/export authorization is produced.

    Caller must separately prove unique occurrence, authenticated frames and day.
    Returning movement does not discharge those independent obligations.
    """
    if before is None or after is None or before.identity()!=after.identity():
        raise ValueError('notice_identity_unproved')
    from .whatsapp_frames import Row,_movement
    # Existing positive/intersecting movement guard; no stationary or overshoot.
    shift=_movement(Row(None,(),after_box),Row(None,(),before_box),viewport,max_shift)
    if shift is None:raise ValueError('notice_physical_movement_unproved')
    return {'topological_movement_only':shift,'occurrence_proven':False,'may_export':False}


@dataclass(frozen=True)
class GenericDeletedNotice:
    original_display: str
    displayed_clock: str
    layout: tuple
    visibility: str
    # Geometric movement does not change the observed layout/role identity.
    meaningful_box: tuple = field(compare=False)
    kind: str='deleted_generic'
    clock_role: str='DISPLAYED_MESSAGE_CLOCK'
    source_binding: str='unresolved'

    def identity(self):
        return self.kind,self.original_display,self.displayed_clock,self.layout


def parse_generic_notice(row,viewport):
    """Exactly the demonstrated generic layout; clock is opaque display identity.

    Fictional fixture clocks do not establish temporal order. No time is parsed
    or assigned to a deletion, no phone/actor is invented and no day is inherited.
    """
    required={'main_layout','conversation_row_name_in_group_name_and_role_container',
              'name_in_group','name_in_group_tv','icon','message_text','date_wrapper','date'}
    allowed=required|{'group_profile_initials','group_profile_pic'}
    try:
        if (row.get('resource-id','')!='' or row.get('package')!=WA or
                row.get('class')!='android.view.ViewGroup' or row.get('long-clickable')!='true'):return None
        grouped={}
        for node in row.iter('node'):
            if node.get('package')!=WA or node.get('visible-to-user')=='false':return None
            rid=node.get('resource-id','')
            if rid:
                if not rid.startswith(PREFIX) or rid[len(PREFIX):] not in allowed:return None
                grouped.setdefault(rid[len(PREFIX):],[]).append(node)
            elif node.get('text') or node.get('content-desc'):return None
        if not required<=grouped.keys() or any(len(v)!=1 for v in grouped.values()):return None
        if grouped['icon'][0].get('class')!='android.widget.ImageView':return None
        if 'group_profile_initials' in grouped and 'group_profile_pic' in grouped:return None
        def text(name):
            n=grouped[name][0];value=n.get('text','')
            if n.get('class')!='android.widget.TextView' or not value or any(ord(c)<32 for c in value):raise ValueError('notice_text_invalid')
            return value
        if text('message_text')!='This message was deleted':return None
        display,clock=text('name_in_group_tv'),text('date')
        meaningful=[n for n in row.iter('node') if n.get('text') or n.get('content-desc') or n.get('resource-id')==PREFIX+'icon']
        rectangles=[box(n) for n in meaningful];outer=box(row)
        if any(not (outer[0]<=b[0]<b[2]<=outer[2] and outer[1]<=b[1]<b[3]<=outer[3]) for b in rectangles):return None
        full=all(viewport[0]<=b[0]<b[2]<=viewport[2] and viewport[1]<b[1]<b[3]<viewport[3] for b in rectangles)
        merged=(min(b[0] for b in rectangles),min(b[1] for b in rectangles),max(b[2] for b in rectangles),max(b[3] for b in rectangles))
        def shape(n):
            return (n.get('resource-id',''),n.get('class',''),n.get('clickable',''),n.get('long-clickable',''),tuple(shape(c) for c in n))
        layout=shape(row)
        import hashlib,json
        if hashlib.sha256(json.dumps(layout,separators=(',',':')).encode()).hexdigest()!= '640ecbfc4acad722541bcd6089065ea2bf07ff1a125d0e9799d1e91b19dd8887':return None
        return GenericDeletedNotice(display,clock,layout,'full' if full else 'clipped',merged)
    except (ValueError,TypeError,KeyError):return None


_parse_admin_notice=parse_notice

def parse_notice(row,viewport):
    return _parse_admin_notice(row,viewport) or parse_generic_notice(row,viewport)
