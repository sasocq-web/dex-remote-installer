"""Small read-only sample for the PC overview, independent of the administrative queue."""
import json
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path


def cpu_times():
    values = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    return sum(values), sum(values[3:5])


def sample():
    first, idle = cpu_times()
    memory = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in
              Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:', 'MemAvailable:'))}
    total, available = memory['MemTotal'], memory['MemAvailable']
    root = shutil.disk_usage('/')
    volumes, disks = [], []
    def visit(node, disk=None):
        if node['type'] == 'disk':
            disk = dict(model=node.get('model') or node['name'], serial=node.get('serial'),
                        transport=node.get('tran'), rotational=bool(node['rota']), size=node['size'])
            disks.append(disk)
        if disk:
            mounts = [m for m in node.get('mountpoints') or [] if m and not m.startswith(
                ('/var/lib/sasocq-control/', '/srv/sasocq/projects/'))]
            if mounts:
                path = min(mounts, key=lambda m: (m.count('/'), len(m)))
                try:
                    usage = shutil.disk_usage(path)
                except OSError:
                    pass
                else:
                    volumes.append(dict(path=path, model=disk['model'], rotational=disk['rotational'],
                        external=disk['transport']=='usb', total=usage.total, used=usage.used,
                        free=usage.free, percent=round(100*usage.used/max(1,usage.total),1)))
        for child in node.get('children', []):
            visit(child, disk)
    try:
        raw = subprocess.check_output(['/usr/bin/lsblk','-J','-b','-o',
            'NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,MOUNTPOINTS'],timeout=2)
        for node in json.loads(raw)['blockdevices']:
            visit(node)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    time.sleep(.08)
    last, idle_end = cpu_times()
    model = next((line.partition(':')[2].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                  if line.startswith('model name')),platform.machine())
    temperatures = []
    for sensor in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
        try:
            temperatures.append({'celsius':float(sensor.read_text())/1000})
        except (OSError,ValueError):
            continue
    return dict(checked_at=time.time(),hostname=socket.gethostname(),kernel=platform.release(),
        platform=platform.machine(),uptime_seconds=float(Path('/proc/uptime').read_text().split()[0]),
        cpu=dict(logical=os.cpu_count(),percent=round(100*(1-(idle_end-idle)/max(1,last-first)),1)),
        memory=dict(total=total,available=available,used=total-available),volumes=volumes,
        filesystems=[dict(path='/',total=root.total,free=root.free,used=root.used,
            percent=round(100*root.used/root.total,1))],temperatures=temperatures,
        hardware=dict(cpu=dict(model=model),disks=disks))
