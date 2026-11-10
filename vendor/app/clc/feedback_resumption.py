"""Typed resumption with durable intent and exact, fresh System observations."""
import hashlib,json,os,stat,time
from pathlib import Path

def observations(directory,event,revision,now):
    # This private file is written by audited System readers, never by owners.
    path=Path(directory)/'observations.json'
    if not path.exists():return []
    if path.is_symlink():raise ValueError('observation symlink')
    st=path.stat()
    if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)&0o077 or st.st_size>250000:
        raise ValueError('unprotected observations')
    data=json.loads(path.read_text())
    if data.get('contract')!='feedback-observations-v1':raise ValueError('observation contract')
    record=data.get('events',{}).get(event,{})
    if record.get('contract_revision')!=revision:return []
    result=record.get('observations',[])
    if not isinstance(result,list):raise ValueError('observation list')
    # Preserve actual collection timestamps; never refresh a cached proof.
    return [r for r in result if isinstance(r,dict) and type(r.get('observed_at')) in (int,float)
            and 0<=now-r['observed_at']<=300]

def coordinate(worker,db,out):
    db.execute("CREATE TABLE IF NOT EXISTS resume_intents(key TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL)")
    db.commit()
    # Recover durable intents before discovering additional conditions.
    pending=db.execute("SELECT key,payload FROM resume_intents WHERE status='prepared'").fetchall()
    for key,raw in pending:
        intent=json.loads(raw);target=worker.routes.get(intent['site'])
        if target!=intent['target']:continue
        view=worker.native(dict(operation='read',**{k:target[k] for k in ('project_id','thread_id')}))
        if view.get('turn_status') not in {'idle','completed','failed','interrupted'} or view.get('budget_exhausted'):continue
        local=db.execute("SELECT status FROM batches WHERE id=?",(intent.get('batch_id'),)).fetchone()
        if local and local[0]!='terminal':continue
        out['next_check']=min(out['next_check'],worker.clock()+60)
        result=worker.call('resumption_reopen',**intent['request'])
        bid=result['batch_id']
        detail=worker.call('details',batch_id=bid)
        batch=dict(detail['batch'],items=detail['items'])
        # Namespace the continuation without rewriting the original dispatch receipt.
        batch['dispatch_key']=batch['dispatch_key']+':'+key
        old=db.execute('SELECT task,receipt,status FROM batches WHERE id=?',(bid,)).fetchone()
        if old and old[2]!='terminal':
            # An existing local active executor must be reconciled first.
            raise ValueError('local executor active during resumption')
        if old and old[0] and old[1]:
            task=json.loads(old[0])
            db.execute('INSERT OR IGNORE INTO history VALUES(?,?,?)',(task['key'],old[0],old[1]))
        db.execute("INSERT OR REPLACE INTO batches(id,batch,status) VALUES(?,?,'claimed')",(bid,json.dumps(batch)))
        db.execute("UPDATE resume_intents SET status='reconciled' WHERE key=?",(key,))
        db.commit()
        out['resumptions']=out.get('resumptions',0)+1
        return
    inventory=worker.call('resumption_inventory')
    out['resumption_inventory']=len(inventory)
    from .feedback_readers import collect
    collect(worker,db,out,inventory)
    if any(r.get('revision') for r in inventory):
        out['next_check']=min(out['next_check'],worker.clock()+60)
    for record in inventory:
        if not record.get('revision') or not record.get('document'):continue
        doc=json.loads(record['document']);target=worker.routes.get(record['site'])
        if not target or doc.get('owner_project')!=target['project_id'] or doc.get('resident')!=target['thread_id']:continue
        if db.execute("SELECT 1 FROM batches WHERE status!='terminal' AND json_extract(batch,'$.site')=?",(record['site'],)).fetchone():continue
        obs=observations(worker.state_dir,record['event_id'],record['revision'],worker.clock())
        required=doc.get('required_dependency_ids')
        if not obs or not isinstance(required,list) or not required or not set(required)<={o.get('id') for o in obs}:continue
        checked=worker.call('resumption_check',event_id=record['event_id'],observations=obs)
        if not checked.get('ready'):continue
        view=worker.native(dict(operation='read',project_id=target['project_id'],thread_id=target['thread_id']))
        if view.get('turn_status') not in {'idle','completed','failed','interrupted'} or view.get('budget_exhausted') or not view.get('turn_id'):continue
        terminal=json.dumps(dict(project_id=target['project_id'],thread_id=target['thread_id'],turn_id=view['turn_id'],turn_status=view['turn_status']),sort_keys=True)
        key='feedback.resume.v1.'+hashlib.sha256((record['event_id']+record['revision']+json.dumps(obs,sort_keys=True)).encode()).hexdigest()
        request=dict(event_id=record['event_id'],observations=obs,revision=record['revision'],key=key,executor_terminal_ref=terminal)
        payload=dict(site=record['site'],target=target,request=request,batch_id=checked['batch_id'])
        db.execute("INSERT OR IGNORE INTO resume_intents VALUES(?,?,'prepared')",(key,json.dumps(payload)))
        db.commit()
        out['next_check']=min(out['next_check'],worker.clock()+15)
        return

