"""Fixed reviewed v3 snapshot structure; never an eligibility decision.

Policy is immutable System configuration, not feedback/tool input. Transport,
challenge, HMAC, fresh source capsule and current CAS remain separate checks.
"""
import hashlib
import json
import math
import re
from .feedback_source_runtime import decode

CONTRACT_SHA = 'a121aeb258a93b336f0fa9aecbd454386cb661c7f292fbe32fbdc83766bd3a64'
PRODUCER_SHA = '71fe463425cf792928ff679bd9ef3058d7462c4116b2e6d87f575f0aee6b9fcc'
QUERY_SHA = 'ccf17cc57d6b58cc8cefaca3cc35eef8294bc610d823f799baefb5583a6f5d59'
IMAGE = 'sha256:9345cb0f9d6171896324300bda0b98f37b46d04bf09559d378a6d72bc1f261ec'
TABLES = ['consumables','inventory_movements','pdc_counts','pdc_visits','pdc_visit_reversals','equipment','weekly_assignments']
IDENTITY = {'role','session_role','superuser','create_role','create_db','replication','bypass_rls','inherits','memberships','readonly','isolation','statement_timeout','lock_timeout','idle_timeout','checked_tables','table_write_privileges','table_select_privileges','schema_create','server_encoding','other_table_writes'}
FIELDS = {
 'consumables': {'id','code','unit','lot','pdc_location','blocked','balance'},
 'movements': {'id','consumable_id','movement_type','quantity','direction','status','original_movement_id','occurred_at'},
 'physical_counts': {'id','consumable_id','previous_balance','counted_balance','movement_id','corrected_count_id','counted_on','created_at','discarded','visit_id'},
 'visits': {'id','state','created_at','reviewed_at'},
 'reversals': {'visit_id','movement_id'},
 'equipment': {'id','asset_tag','pdc_location','status','updated_at'},
}
GROUPS = {'controls_stock_reconciliation': {'consumables','movements','physical_counts','visits','reversals'}, 'controls_equipment_inventory': {'equipment'}, 'controls_team_distribution': {'week','week_state'}}

def encoded(v):
    # Producer v3 preserves insertion order; sorting would change its digests.
    return json.dumps(v,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()

def exact(v, keys):
    if type(v) is not dict or set(v) != set(keys): raise ValueError('history schema mismatch')

class ReviewedHistoryValidator:
    def __init__(self, policy_bytes):
        if type(policy_bytes) is not bytes or hashlib.sha256(policy_bytes).hexdigest() != CONTRACT_SHA:
            raise ValueError('unreviewed history contract')
        self.policy = decode(policy_bytes,100000)
        if (self.policy['snapshot']['producer_sha256'] != PRODUCER_SHA or self.policy['query_hash'] != QUERY_SHA
            or self.policy['runtime_image_id'] != IMAGE): raise ValueError('history contract pins')
        self.snapshot_keys = frozenset(self.policy['snapshot']['required'])

    def __call__(self, value):
        exact(value,self.snapshot_keys)
        if (value['schema'] != 'nc-private-history-snapshot-v3' or value['producer_version'] != 'nc-private-history-v3'
            or value['producer_sha256'] != PRODUCER_SHA or value['queries_sha256'] != QUERY_SHA): raise ValueError('history pins')
        event=self.policy['fixed_registry'].get(value['event_id'])
        if (not event or type(value['source_report_id']) is not int or value['source_report_id'] != event['source_report_id']
            or value['contract_revision'] != event['contract_revision'] or value['dependency_id'] not in event['dependencies']):
            raise ValueError('history fixed registry mismatch')
        for key in ['event_id','contract_revision','nonce','capsule_sha256']:
            if type(value[key]) is not str or re.fullmatch('[a-f0-9]{64}',value[key]) is None: raise ValueError('history identifier')
        at=value['db_observed_at']
        if type(at) not in (int,float) or not math.isfinite(at): raise ValueError('history timestamp')
        identity=value['db_identity'];exact(identity,IDENTITY)
        for key in ['superuser','create_role','create_db','replication','bypass_rls','inherits','schema_create','other_table_writes']:
            if identity[key] is not False: raise ValueError('history privileged identity')
        expected={'role':'nc_history_select_v3','session_role':'nc_history_select_v3','readonly':'on','isolation':'repeatable read','statement_timeout':'5s','lock_timeout':'1s','idle_timeout':'5s','server_encoding':'UTF8','checked_tables':TABLES}
        if any(identity[k] != v for k,v in expected.items()) or type(identity['memberships']) is not int or identity['memberships'] != 0:
            raise ValueError('history identity/settings')
        for key,required in [('table_write_privileges',False),('table_select_privileges',True)]:
            if type(identity[key]) is not list or len(identity[key]) != 7 or any(x is not required for x in identity[key]):
                raise ValueError('history privileges')
        h=value['history'];groups=GROUPS[value['dependency_id']];exact(h,groups|{'observed_at'})
        if type(h['observed_at']) is not type(at) or h['observed_at'] != at or len(encoded(h)) > 2000000: raise ValueError('history timestamp/size')
        coverage=value['history_coverage'];exact(coverage,groups);total=0
        for key in groups:
            if key in FIELDS:
                rows=h[key]
                if type(rows) is not list or len(rows)>10000: raise ValueError('history row count')
                for row in rows:
                    exact(row,FIELDS[key])
                    if len(encoded(row))>32768: raise ValueError('history row bytes')
                    for col,item in row.items():
                        if type(item) not in (str,int,float,bool,type(None)) or type(item) is float and not math.isfinite(item): raise ValueError('history scalar')
                        if col in ('blocked','discarded') and type(item) is not bool: raise ValueError('history bool')
                        if col in ('id','consumable_id','movement_id','original_movement_id','corrected_count_id') and item is not None:
                            if type(item) is not int and not (key=='visits' and col=='id' and type(item) is str): raise ValueError('history row identity')
            elif key == 'week':
                if type(h[key]) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}',h[key]) is None: raise ValueError('history week')
                rows=[{'week':h[key]}]
            else:
                exact(h[key],{'count','published','unique_people'});rows=[h[key]]
                if any(type(x) is not int or not 0<=x<=1000000 for x in h[key].values()) or h[key]['published']>h[key]['count'] or h[key]['unique_people']>h[key]['count']:
                    raise ValueError('history week counts')
            m=coverage[key];exact(m,{'rows','server_bytes','server_max_row_bytes','serialized_bytes','serialized_sha256'})
            if any(type(m[k]) is not int or m[k]<0 for k in ['rows','server_bytes','server_max_row_bytes','serialized_bytes']): raise ValueError('history coverage counts')
            raw=encoded(rows)
            if m['rows'] != len(rows) or m['serialized_bytes'] != len(raw) or m['serialized_sha256'] != hashlib.sha256(raw).hexdigest(): raise ValueError('history coverage digest')
            if m['rows']>10000 or m['server_max_row_bytes']>32768 or m['server_bytes']>2000000 or m['rows']>0 and not 0<m['server_max_row_bytes']<=m['server_bytes']:
                raise ValueError('history coverage bounds')
            total+=m['server_bytes']
        if total>2000000-65536: raise ValueError('history total bytes')
        return True
