"""Read-only Android Settings date evidence, never a host-clock fallback."""
import datetime as dt
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
from .whatsapp_collector import ExportError, nodes, select, center

SETTINGS = 'com.android.settings'

def settings_day(xml, now):
    nodes(xml)  # Same bounded XML validation as the app cursor.
    tree = ET.fromstring(xml)
    values = {}
    for row in tree.iter('node'):
        children = list(row)
        titles = [n.get('text') for n in children if n.get('resource-id') == 'android:id/title' and n.get('package') == SETTINGS]
        summaries = [n.get('text') for n in children if n.get('resource-id') == 'android:id/summary' and n.get('package') == SETTINGS]
        if len(titles) == len(summaries) == 1:
            if titles[0] in values:
                raise ExportError('device_date_ambiguous')
            values[titles[0]] = summaries[0]
    if values.get('Time zone') != 'GMT-03:00 Brasilia Standard Time':
        raise ExportError('device_timezone_unverified')
    try:
        day = dt.datetime.strptime(values['Date'], '%B %d, %Y').date()
        clock = values['Time']
        parsed = dt.datetime.strptime(clock, '%I:%M %p' if clock.endswith(('AM','PM')) else '%H:%M').time()
    except (KeyError, ValueError):
        raise ExportError('device_date_unverified') from None
    local = now.astimezone(ZoneInfo('America/Sao_Paulo'))
    device = dt.datetime.combine(day, parsed, local.tzinfo)
    if abs((device-local).total_seconds()) > 180 or device.hour == 0 and device.minute < 3 or device.hour == 23 and device.minute >= 57:
        raise ExportError('device_clock_or_rollover_unverified')
    return day

def visible_select(rows, **selector):
    # Settings retains off-screen preference rows with reversed/empty bounds.
    # They are not actionable, even when Android reports enabled=true.
    visible = []
    for row in rows:
        try:
            center(row)
        except ExportError:
            continue
        visible.append(row)
    return select(visible, **selector)

async def read_device_day(ui):
    await ui.call({'operation':'open', 'package':SETTINGS})
    for _ in range(8):
        rows = await ui.read()
        if visible_select(rows, text='Date & time', rid='android:id/title', package=SETTINGS):
            x,y = center(visible_select(rows, text='Date & time', rid='android:id/title', package=SETTINGS))
            await ui.call({'operation':'tap','x':x,'y':y})
            break
        if visible_select(rows, text='System', rid='android:id/title', package=SETTINGS):
            x,y = center(visible_select(rows, text='System', rid='android:id/title', package=SETTINGS))
            await ui.call({'operation':'tap','x':x,'y':y})
            continue
        # Already on the date page after a previous check.
        if any(n.get('text') == 'Date' and n.get('package') == SETTINGS for n in rows):
            break
        scroll = visible_select(rows, rid=SETTINGS+':id/main_content_scrollable_container')
        if not scroll:
            await ui.call({'operation':'back'})
            continue
        x, y = center(scroll)
        await ui.call({'operation':'swipe','x':x,'y':y+250,'x2':x,'y2':y-250,'duration_ms':450})
    else:
        raise ExportError('device_settings_unavailable')
    result = await ui.call({'operation':'ui'})
    return settings_day(result['output'], dt.datetime.now(dt.timezone.utc))
