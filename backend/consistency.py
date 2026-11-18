"""Gate quarantine: independent evidence may invalidate admission, never upgrade it."""
VERSION = '3.0'


def reconcile(data, policy):
    allowed = {criterion['id'] for criterion in policy['criteria']}
    risks = {risk['id']: risk for risk in data.get('critique', {}).get('risks', [])}
    findings = []
    for check in data.get('verification', {}).get('checks', []):
        risk = risks.get(check['claim_id'], {})
        if not risk.get('factual') or check.get('status') not in ('supported', 'partial') or not check.get('grounded'):
            continue
        for impact in check.get('criterion_impacts', []):
            if impact.get('criterion_id') not in allowed or impact.get('effect') not in ('contradicts', 'uncertain'):
                continue
            findings.append({'criterion_id': impact['criterion_id'], 'risk_id': check['claim_id'],
                             'check_status': check['status'], 'effect': impact['effect'],
                             'reason': impact['reason'], 'source_url': check.get('source_url'), 'quote': check.get('quote')})
    for gate in data['assessment']['gates']:
        conflicts = [item for item in findings if item['criterion_id'] == gate['id']]
        if conflicts and gate['status'] == 'pass':
            gate['status'] = 'review'
            gate['reason'] += ' Независимая проверка выявила противоречие: ' + '; '.join(item['reason'] for item in conflicts)
    data['consistency'] = {'version': VERSION, 'findings': findings,
                           'rule': 'grounded_independent_conflict_quarantines_gate',
                           'checked': data.get('verification', {}).get('contract_version') == VERSION}
    return findings
