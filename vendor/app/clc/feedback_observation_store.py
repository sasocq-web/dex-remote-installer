"""Atomic private observation cache for trusted System producers only.

No public endpoint calls this function with caller-supplied observations. A native
producer must verify its receipt and current contract before publishing here.
"""
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path
from .feedback_native_receipts import fresh, protected

FIELDS={'id','kind','reader_id','subject','revision','observed_at','evidence_ref','satisfied','reason'}
LIMIT=250000


def validate(binding, observation, now):
    if not isinstance(observation,dict) or set(observation)!=FIELDS:
        raise ValueError('observation envelope')
    session=(observation['id']==binding['dependency_id'] and observation['kind']=='authenticated_session'
             and observation['reader_id']=='nc-fixed-observation-v1.native_session')
    capture=(observation['id']=='android_capture' and observation['kind']=='capability'
             and observation['reader_id']=='system.android.native_capture.v1')
    if not (session or capture) or type(observation['satisfied']) is not bool:
        raise ValueError('observation producer')
    subject=dict(event_id=binding['event_id'],source_report_id=binding['source_report_id'],
                 contract_revision=binding['contract_revision'],origin='https://nc.sasocq.com',
                 module=binding['module'],device='android')
    if observation['subject']!=subject:
        raise ValueError('observation binding')
    for value in (binding['event_id'],binding['contract_revision'],observation['revision']):
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):
            raise ValueError('observation revision')
    if (not fresh(observation['observed_at'],now)
            or not isinstance(observation['evidence_ref'],str)
            or not re.fullmatch(('urn:sasocq:native-capture:' if capture else 'urn:sasocq:native-receipt:')+'[a-f0-9]{64}',observation['evidence_ref'])
            or not isinstance(observation['reason'],str)
            or not re.fullmatch('[a-z][a-z0-9_]{0,119}',observation['reason'])):
        raise ValueError('observation evidence')


def publish(directory,binding,observation,now):
    """Merge a verified producer result without refreshing any stored timestamp.

    Caller must own its native lease and have re-read contract CAS immediately
    before this call. This function never changes source item/batch/report state.
    """
    validate(binding,observation,now)
    directory=Path(directory);protected(directory)
    lock=directory/'observations.lock'
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    temporary=None
    try:
        protected(lock);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=directory/'observations.json'
        value={'contract':'feedback-observations-v1','events':{}}
        if path.exists() or path.is_symlink():
            protected(path)
            if path.stat().st_size>LIMIT:raise ValueError('observation capacity')
            value=json.loads(path.read_text())
            if (not isinstance(value,dict) or set(value)!={'contract','events'}
                    or value['contract']!='feedback-observations-v1'
                    or not isinstance(value['events'],dict)):
                raise ValueError('observation cache contract')
        # Remove only expired proofs; timestamps on retained proofs are untouched.
        events={}
        for event,record in value['events'].items():
            if not isinstance(record,dict) or not isinstance(record.get('observations'),list):
                raise ValueError('observation cache record')
            rows=[r for r in record['observations'] if isinstance(r,dict) and fresh(r.get('observed_at'),now)]
            if rows:events[event]=dict(record,observations=rows)
        event=binding['event_id'];revision=binding['contract_revision']
        previous=events.get(event,{})
        rows=previous.get('observations',[]) if previous.get('contract_revision')==revision else []
        old=next((r for r in rows if r.get('id')==observation['id']),None)
        # Concurrent delayed completion must not overwrite newer actual evidence.
        if old and old['observed_at']>observation['observed_at']:
            raise ValueError('observation older than stored proof')
        events[event]={'contract_revision':revision,'observations':[
            *[r for r in rows if r.get('id')!=observation['id']],observation]}
        raw=json.dumps({'contract':'feedback-observations-v1','events':events},
                       sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if len(raw)>LIMIT:raise ValueError('observation capacity')
        temp_fd,temporary=tempfile.mkstemp(prefix='.observations-',dir=directory)
        with os.fdopen(temp_fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path);temporary=None
        parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        if temporary is not None:Path(temporary).unlink(missing_ok=True)
        os.close(fd)
