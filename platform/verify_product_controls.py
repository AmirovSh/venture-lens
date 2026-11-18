"""Read-only live verification using existing QA accounts; never queues LLM work."""
import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from ssh_client import ROOT
from verify_decision_workspace import Client


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    credentials = json.loads((ROOT/'.platform/public-demo-credentials.json').read_text())
    qa = json.loads((ROOT/'.platform/new-user-journey-account.json').read_text())
    clients = []
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'read_only': True}
    try:
        access = {}
        for role, account in [('owner', credentials['owner']), ('viewer', credentials['viewer']), ('member', qa)]:
            client = Client({key: account[key] for key in ('username', 'password')})
            clients.append(client)
            client.login()
            code, _, raw = client.request('/api/admin/model-settings')
            assert code == (200 if role == 'owner' else 403)
            access[role] = code
            if role == 'owner':
                value = json.loads(raw)
                assert 'api_key' not in value and 'encrypted_key' not in value
                report['configuration'] = {key: value[key] for key in ('version', 'connection_mode', 'key_configured')}
                assert client.request('/api/runs/'+args.run_id)[0] == 404
            elif role == 'member':
                run = client.json('/api/runs/'+args.run_id)
                audit = client.json('/api/runs/'+args.run_id+'/audit')
                preferences = client.json('/auth/preferences')
        assert run['status'] in ('completed', 'completed_with_gaps')
        assert run['settings']['adaptive_selection'] is True
        assert run['settings']['region'] == 'Custom' and run['settings']['market'] == 'Canada'
        assert run['counts']['error'] == 0
        assert all(model['stage'] == 'completed' for model in run['models'])
        assert len(run['models']) == run['settings']['selection']['found'] == 4
        snapshot = run['policy']['agent_config']
        for invocation in audit:
            content = json.dumps(invocation['input'], ensure_ascii=False, separators=(',', ':'))
            assert digest(content) == invocation['input_hash']
            instruction = snapshot['prompts'][invocation['role']]
            if invocation['role'] == 'verifier':
                instruction += '\nОбязательные условия: '+json.dumps(run['policy']['criteria'], ensure_ascii=False, separators=(',', ':'))
                assert set(invocation['input']) == {'company', 'claims', 'sources'}
            assert invocation['prompt'].endswith('\n'+instruction)
            assert invocation['prompt_version'] == f"runtime-{snapshot['version']}-{digest(instruction)[:12]}"
        assert audit and any(item['status'] == 'completed' for item in audit)
        report.update(access=access, owner_cannot_read_private_qa=True,
                      run={key: run[key] for key in ('id', 'status', 'counts', 'calls', 'tokens')},
                      selection=run['settings']['selection'], market='Canada',
                      models=sorted({item['model'] for item in audit}),
                      audit_statuses=dict(Counter(item['status'] for item in audit)),
                      input_hashes_verified=len(audit), effective_prompts_verified=len(audit),
                      verifier_context_isolated=True, preferences=preferences)
        path = ROOT/'.platform/product-live-verification.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        for client in clients:
            client.logout()


if __name__ == '__main__':
    main()
