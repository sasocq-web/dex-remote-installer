"""Deterministic official-app exports; caller must own the shared Android lease.

No private WhatsApp database, account registration, APK, credentials or LLM.
UI writes are never retried blindly. Persist upload intent before tapping Upload.
"""
import asyncio
import datetime as dt
import re
import xml.etree.ElementTree as ET

WA = 'com.whatsapp.w4b'
DRIVE = 'com.google.android.apps.docs'
GROUPS = {
    'REDUC': ('Dessalgadoras', 'Dessalgadoras REDUC- grupo do I'),
    'ADM': ('Dessalgadora', 'Dessalgadora ADM/1°/2⁰ TURNO'),
    'IPS': ('IPS', 'IPS'),
    'GESTAO-IPS': ('Gest', 'Gestão IPS'),
}

class ExportError(RuntimeError):
    pass

def nodes(xml):
    if len(xml) > 2_000_000 or '<!DOCTYPE' in xml or '<!ENTITY' in xml:
        raise ExportError('invalid_ui')
    return [n.attrib for n in ET.fromstring(xml).iter('node')]

def select(rows, *, text=None, rid=None, desc=None, package=None):
    found = [n for n in rows if (text is None or n.get('text') == text)
             and (rid is None or n.get('resource-id') == rid)
             and (desc is None or n.get('content-desc') == desc)
             and (package is None or n.get('package') == package)
             and n.get('enabled') == 'true']
    # Android search overlays may retain the underlying home tree. The overlay
    # is last; never match arbitrary message text or a partial contact name.
    return found[-1] if found else None

def center(node):
    match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
    if not match:
        raise ExportError('invalid_bounds')
    x,y,a,b = map(int, match.groups())
    if a <= x or b <= y or a > 8192 or b > 8192:
        raise ExportError('invalid_bounds')
    return (x+a)//2, (y+b)//2


def search_chat_result(xml, title):
    """Scope identity to the active search, excluding the retained home tree."""
    nodes(xml)
    tree = ET.fromstring(xml)
    fragments = [n for n in tree.iter('node')
                 if n.get('resource-id') == WA+':id/search_fragment']
    if len(fragments) != 1:
        raise ExportError('search_scope_missing_or_ambiguous')
    scope = fragments[0]
    center(scope.attrib)
    scope_box = tuple(map(int, re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',
                                          scope.get('bounds', '')).groups()))
    # Distinct DOM labels, not arbitrary equal titles or coordinate dedup.
    matches = {}
    for container in scope.iter('node'):
        if container.get('resource-id') != WA+':id/contact_row_container':
            continue
        for label in container.iter('node'):
            if (label.get('resource-id') == WA+':id/conversations_row_contact_name'
                    and label.get('text') == title and label.get('package') == WA
                    and label.get('enabled') == 'true'):
                try:
                    center(label.attrib)
                    box = tuple(map(int, re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',
                                                     label.get('bounds', '')).groups()))
                except (ExportError, AttributeError):
                    continue
                if not (scope_box[0] <= box[0] < box[2] <= scope_box[2]
                        and scope_box[1] <= box[1] < box[3] <= scope_box[3]):
                    continue
                matches[id(label)] = label.attrib
    if len(matches) > 1:
        raise ExportError('ambiguous_chat_result')
    return next(iter(matches.values()), None)


def search_value(value):
    # Native search exposes an invisible leading U+200B in its EditText.
    # Normalize only input-edge markers, never chat titles or message bodies.
    return value.strip('\u200b\ufeff')

class OfficialUI:
    def __init__(self, call):
        self.call = call
        self.navigation_stage = 'initial'
        self.navigation_trace = []

    def stage(self, value):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', value):
            raise ValueError('invalid_navigation_stage')
        self.navigation_stage = value
        self.navigation_trace.append(value)
        self.navigation_trace = self.navigation_trace[-16:]

    async def read(self):
        result = await self.call({'operation':'ui'})
        return nodes(result['output'])

    async def wait(self, **selector):
        for _ in range(20):
            row = select(await self.read(), **selector)
            if row is not None:
                return row
            await asyncio.sleep(1)
        raise ExportError('expected_control_missing_'+self.navigation_stage)

    async def tap(self, **selector):
        row = await self.wait(**selector)
        x,y = center(row)
        await self.call({'operation':'tap','x':x,'y':y})

    async def wait_search_query(self, query):
        for _ in range(20):
            field = select(await self.read(), rid=WA+':id/search_input', package=WA)
            if field and search_value(field.get('text','')) == query:
                return field
            await asyncio.sleep(1)
        raise ExportError('search_query_not_confirmed')

    async def wait_search_empty(self):
        for _ in range(20):
            rows = await self.read()
            field = select(rows, rid=WA+':id/search_input', package=WA)
            if field:
                value = search_value(field.get('text',''))
                # Accessibility exposes the empty hint as text. A real query
                # has the independent Clear control; never append to it.
                hint = value == 'Search…' and not select(rows, rid=WA+':id/search_clear_btn')
                if field.get('class') != 'android.widget.EditText' or (value and not hint):
                    raise ExportError('search_input_not_empty_editable')
                return field
            await asyncio.sleep(1)
        raise ExportError('expected_control_missing_'+self.navigation_stage)

    async def home(self):
        self.stage('wa_home')
        await self.call({'operation':'open','package':WA})
        for _ in range(8):
            rows = await self.read()
            # Explicit official companion logout, never inferred from chat text.
            # Do not press Continue or attempt login from an unattended collector.
            logged_out = (select(rows, rid=WA+':id/post_logout_title', package=WA)
                          and select(rows, rid=WA+':id/continue_button', package=WA))
            welcome = (select(rows, rid=WA+':id/eula_layout', package=WA)
                       and select(rows, rid=WA+':id/eula_accept', package=WA))
            if logged_out or welcome:
                raise ExportError('whatsapp_link_required')
            if (select(rows, rid=WA+':id/toolbar_logo')
                    and select(rows, rid=WA+':id/search_bar_inner_layout')
                    and not select(rows, rid=WA+':id/search_fragment')):
                return
            await self.call({'operation':'back'})
            await asyncio.sleep(.2)
        raise ExportError('whatsapp_home_unavailable')

    async def tap_chat_result(self, title):
        self.stage('wa_chat_result')
        for _ in range(20):
            result = await self.call({'operation':'ui'})
            row = search_chat_result(result['output'], title)
            if row is not None:
                x,y = center(row)
                await self.call({'operation':'tap','x':x,'y':y})
                return
            await asyncio.sleep(1)
        raise ExportError('chat_result_missing')

    async def inspect_group(self, code):
        query, title = GROUPS[code]
        await self.home()
        self.stage('wa_search_open')
        await self.tap(rid=WA+':id/search_bar_inner_layout', package=WA)
        self.stage('wa_search_input')
        field = await self.wait_search_empty()
        if field.get('focused') != 'true':
            x,y = center(field)
            await self.call({'operation':'tap','x':x,'y':y})
            field = await self.wait_search_empty()
            if field.get('focused') != 'true':
                raise ExportError('search_input_not_focused')
        await self.call({'operation':'type','text':query})
        self.stage('wa_search_query')
        await self.wait_search_query(query)
        await self.tap_chat_result(title)
        self.stage('wa_group_header')
        # IPS search can open its community instead of the identically named chat.
        for _ in range(20):
            rows = await self.read()
            if (select(rows, text=title, rid=WA+':id/conversation_contact_name')
                    and not select(rows, rid=WA+':id/search_fragment')):
                break
            if select(rows, rid=WA+':id/community_navigation_communityName'):
                self.stage('wa_community_chat')
                await self.tap(text=title, rid=WA+':id/conversations_row_contact_name')
                await self.wait(text=title, rid=WA+':id/conversation_contact_name')
                break
            await asyncio.sleep(1)
        else:
            raise ExportError('group_header_not_confirmed')
        # Header confirmation establishes identity, never complete history or
        # attachment coverage. Do not persist message text or hashes of UI text.
        return {'group': code, 'header_confirmed': True,
                'history_coverage': 'insufficient', 'media_coverage': 'unknown'}

    async def export(self, code, day, intent):
        query, title = GROUPS[code]
        if not re.fullmatch(r'\d{8}', day):
            raise ValueError('invalid_date')
        dt.datetime.strptime(day, '%Y%m%d')
        name = f'SASOCQ-{code}-{day}-OFICIAL.zip'
        await self.inspect_group(code)
        self.stage('wa_export_menu')
        await self.tap(desc='More options', package=WA)
        self.stage('wa_more_menu')
        await self.tap(text='More', package=WA)
        self.stage('wa_export_chat')
        await self.tap(text='Export chat', package=WA)
        # Official ZIP exports may skip the media dialog and open sharing directly.
        # Report actual media only after cloud ZIP/content verification.
        self.stage('wa_export_media_choice')
        for _ in range(20):
            rows = await self.read()
            if select(rows, text='Include media', package=WA):
                await self.tap(text='Include media', package=WA)
                break
            if select(rows, text='Drive', package='android'):
                break  # Direct ZIP sharing is valid; cloud CRC/content proof remains mandatory.
            await asyncio.sleep(1)
        else:
            raise ExportError('media_export_dialog_missing')
        self.stage('android_share_drive')
        await self.tap(text='Drive', package='android')
        self.stage('drive_upload_form')
        await self.wait(rid=DRIVE+':id/upload_title_edittext')
        # Only the already authorized destination. A changed/expired Drive UI
        # fails closed; never select another account or consent to login.
        rows = await self.read()
        folder = select(rows, rid=DRIVE+':id/upload_folder_autocomplete')
        if not folder or folder.get('text', '').strip() != 'Conversas WhatsApp , Location':
            raise ExportError('drive_destination_changed')
        self.stage('drive_filename_edit')
        await self.tap(rid=DRIVE+':id/upload_title_edittext')
        # Clear icon is distinct from the folder dropdown, though IDs match.
        await self.tap(desc='Delete file name', package=DRIVE)
        await self.call({'operation':'type','text':name})
        field = await self.wait(rid=DRIVE+':id/upload_title_edittext')
        if field.get('text') != name+', File name':
            raise ExportError('drive_filename_not_confirmed')
        # Durable intent makes a crash at the upload boundary uncertain, never
        # an automatic retry that creates another Drive file.
        await intent(code, name)
        self.stage('drive_submit')
        await self.tap(rid=DRIVE+':id/save_button')
        self.stage('wa_export_return')
        await self.wait(text=title, rid=WA+':id/conversation_contact_name')
        return name
