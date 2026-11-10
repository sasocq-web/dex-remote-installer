"""Pinned read-only NC diagnostics. No positive semantic evidence or Android startup."""
import json, math, re
from pathlib import Path
PROJECT='project-96921b3806e0'
VERSION='nc-fixed-observation-v1'
def registry():
    return json.loads(Path(__file__).with_name('nc_reader_registry.json').read_text())

def validate(value,event,dep,contract,spec,now):
    allowed={'id','kind','reader_id','subject','revision','observed_at','evidence_ref','satisfied','reason'}
    if not isinstance(value,dict) or set(value)!=allowed:raise ValueError('reader envelope')
    subject={'event_id':event,'origin':'https://nc.sasocq.com','module':contract['module'],
             'source_report_id':contract['source_report_id'],'contract_revision':contract['contract_revision']}
    if value['subject']!=subject or value['id']!=dep or value['kind']!=spec['kind'] or value['reader_id']!=VERSION+'.'+dep:
        raise ValueError('reader binding')
    stamp=value['observed_at']
    if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<=now-stamp<=300:raise ValueError('reader freshness')
    revision=value['revision']
    if not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{64}',revision):raise ValueError('reader revision')
    if value['evidence_ref']!='urn:sasocq:nc-observation:'+revision:raise ValueError('reader evidence')
    # This pinned delivery provides dataset diagnostics only, never case authorization.
    if value['satisfied'] is not False or not isinstance(value['reason'],str) or not re.fullmatch(('canonical_source_reconciliation_required:' if dep.startswith('controls_') else 'human_case_binding_required:')+'[A-Za-z0-9_]{1,120}',value['reason']):
        raise ValueError('unsupported positive semantics')
    return value

def collect(worker,db,out,inventory):
    pinned=registry()
    db.execute('CREATE TABLE IF NOT EXISTS reader_checks(event TEXT,dependency TEXT,contract_revision TEXT,next_at REAL NOT NULL,result TEXT,error TEXT,PRIMARY KEY(event,dependency))')
    eligible=[];now=worker.clock()
    for record in inventory:
        event=record['event_id'];contract=pinned.get(event)
        if not contract or record.get('state') not in {'blocked','validation_pending'} or record.get('site') not in {'nc.sasocq.com','controles.sasocq.com'} or not record.get('document'):continue
        doc=json.loads(record['document']);target=worker.routes.get(record['site'],{})
        if (record.get('revision')!=contract['contract_revision'] or doc.get('source_report_id')!=contract['source_report_id']
            or doc.get('owner_project')!=PROJECT or target.get('project_id')!=PROJECT or doc.get('resident')!=target.get('thread_id')):continue
        for dep,spec in contract['dependencies'].items():
            if spec['kind']!='data_revision' or dep not in doc.get('required_dependency_ids',[]):continue
            row=db.execute('SELECT contract_revision,next_at FROM reader_checks WHERE event=? AND dependency=?',(event,dep)).fetchone()
            if row and row[0]==record['revision'] and row[1]>now:continue
            eligible.append((row[1] if row else 0,event,dep,contract,spec))
    count=0
    for _,event,dep,contract,spec in sorted(eligible,key=lambda v:(v[0],v[1],v[2]))[:2]:
        try:
            reply=worker.queue({'_transport_mode':'nc_observe','event_id':event,'dependency_id':dep})
            if reply.get('ok') is not True:raise ValueError('reader unavailable')
            result=validate(reply.get('observation'),event,dep,contract,spec,worker.clock())
            # Historical diagnostics stay separate from fresh positive observation cache.
            db.execute('INSERT OR REPLACE INTO reader_checks VALUES(?,?,?,?,?,NULL)',(event,dep,contract['contract_revision'],worker.clock()+86400,json.dumps(result)))
            count+=1
        except Exception as exc:
            db.execute('INSERT OR REPLACE INTO reader_checks VALUES(?,?,?,?,NULL,?)',(event,dep,contract['contract_revision'],worker.clock()+60,type(exc).__name__))
            out.setdefault('errors',[]).append('reader:'+type(exc).__name__)
        db.commit()
    out['reader_diagnostics']=count
    if len(eligible)>2 or out.get('errors'):out['next_check']=min(out['next_check'],worker.clock()+60)
