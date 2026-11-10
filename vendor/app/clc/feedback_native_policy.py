"""Fixed audited identity policies. Classification is not application acceptance."""
import hashlib
import json
import re
from pathlib import Path
from .feedback_native_receipts import FACTS, IDENTITY, fresh, wire


def policy(binding):
    data=json.loads(Path(__file__).with_name('nc_identity_policy.json').read_text())
    if data.get('contract')!='nc-native-policy-v1':raise ValueError('native policy registry')
    found=[p for p in data['policies'] if p['event_id']==binding['event_id']
           and p['dependency_id']==binding['dependency_id']]
    if len(found)!=1:raise ValueError('native profile policy not registered')
    p=found[0]
    if (any(p[k]!=binding[k] for k in ('contract_revision','source_report_id','module'))
            or p['policy_id']!='canonical-owner-n5-v1' or p['required_level']!=5
            or p['canonical_account_id']!=7 or p['required_identity_schema']!='nc-owner-identity-v1'
            or p['device']!='android' or p['origin']!='https://nc.sasocq.com'):
        raise ValueError('native profile policy mismatch')
    return p


def classify(binding,witness,now):
    p=policy(binding);facts=witness['result']
    if (not isinstance(facts,dict) or set(facts) not in (FACTS,FACTS|{'identity'})
            or type(facts['authenticated']) is not bool or type(facts['allowed']) is not bool):
        raise ValueError('native fixed facts')
    identity=facts.get('identity')
    if not fresh(witness['collected_at'],now) or not fresh(facts.get('observed_at'),now):
        raise ValueError('native witness expired')
    flags=IDENTITY-{'schema','identity_revision','observed_at'}
    valid=(isinstance(identity,dict) and set(identity)==IDENTITY
           and identity['schema']==p['required_identity_schema']
           and all(type(identity[k]) is bool and identity[k] for k in flags)
           and isinstance(identity['identity_revision'],str)
           and re.fullmatch('[a-f0-9]{64}',identity['identity_revision']) is not None
           and fresh(identity['observed_at'],now))
    # The receipt validator already rejects malformed hashes/types/extra data.
    satisfied=(facts['authenticated'] is True and facts['allowed'] is True and valid
               and type(facts['level']) is int and facts['level']==p['required_level'])
    reason=('canonical_owner_n5_session_observed' if satisfied
            else 'existing_session_absent_no_login_started' if not facts['authenticated']
            else 'canonical_owner_n5_identity_not_proven')
    stamps=[witness['collected_at'],facts['observed_at']]
    if isinstance(identity,dict):stamps.append(identity['observed_at'])
    revision=hashlib.sha256(wire({'binding':binding,'receipt_revision':witness['revision'],
                                'policy':p['policy_id'],'facts':facts}).encode()).hexdigest()
    return dict(id=binding['dependency_id'],kind='authenticated_session',
        reader_id='nc-fixed-observation-v1.native_session',subject=dict(
            event_id=binding['event_id'],source_report_id=binding['source_report_id'],
            contract_revision=binding['contract_revision'],origin=p['origin'],
            module=binding['module'],device=p['device']),revision=revision,
        observed_at=min(stamps),evidence_ref=witness['evidence_ref'],satisfied=bool(satisfied),reason=reason)
