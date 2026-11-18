"""Credential rotation, persisted request boundaries and cursor log regression tests."""
import copy
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend import db,settings,runtime_config,llm,evidence,transport
from backend.app import app
from backend.auth_context import sign_identity
from backend.schemas import Discovery
from test_pipeline import store,seed
from test_product_controls import configuration
from test_registration import SECRET

OLD='TEST_ONLY_ROTATION_OLD_abcdefghijk'
NEW='TEST_ONLY_ROTATION_NEW_abcdefghijk'

def setup_rotation(store,monkeypatch,**changes):
    seed(store)
    monkeypatch.setattr(evidence,'public_url',lambda url:url)
    monkeypatch.setattr(settings,'env',settings.env|{'CONFIG_ENCRYPTION_KEY':'TEST_ONLY_ROTATION_ENCRYPTION','AUTH_CONTEXT_REQUIRED':'false'})
    body=configuration(connection_mode='custom',base_url='https://models.example/v1',api_key=OLD)
    body['models']['discovery']='original-model'
    body['prompts']['discovery']='Original discovery instruction must survive credential rotation.'
    runtime_config.save(runtime_config.ModelSettingsInput(**body),'owner:main')
    with store.begin() as s:
        run=s.get(db.Run,'run');policy=db.read(run.policy);policy['agent_config']=runtime_config.snapshot(s);run.policy=db.dump(policy);run.status='partial'
        s.get(db.Job,'job').status='failed'
    later=copy.deepcopy(body);later.update(version=1,api_key=NEW,**changes)
    later['models']['discovery']='later-model'
    later['prompts']['discovery']='A newer prompt must not rewrite the previous research.'
    runtime_config.save(runtime_config.ModelSettingsInput(**later),'owner:main')
    return TestClient(app),{'configuration_version':2,'connection_version':1,'generation':0}

def test_new_log_pages_cover_over_500_records_and_new_arrivals_without_duplicates(store,monkeypatch):
    seed(store)
    with store.begin() as s:
        s.add_all([db.Event(run_id='run',stage='test',message=str(i)) for i in range(650)])
    client=TestClient(app)
    page=client.get('/api/runs/run/events/page').json()
    assert len(page['events'])==100 and page['events'][-1]['message']=='649' and page['total']==650
    latest=page['after'];seen=[x['id'] for x in page['events']]
    with store.begin() as s:
        s.add_all([db.Event(run_id='run',stage='test',message='new-'+str(i)) for i in range(510)])
    while page['has_more']:
        page=client.get('/api/runs/run/events/page',params={'before':page['before']}).json()
        seen.extend(x['id'] for x in page['events'])
    assert len(seen)==len(set(seen))==650
    more=True;arrivals=[]
    while more:
        page=client.get('/api/runs/run/events/page',params={'after':latest,'limit':200}).json()
        arrivals.extend(x['id'] for x in page['events']);latest=page['after'];more=page['has_more']
    assert len(arrivals)==len(set(arrivals))==510 and not set(arrivals).intersection(seen)
    assert page['total']==1160 and page['events'][-1]['message']=='new-509'
    assert client.get('/api/runs/run/events/page?after=0&before=1').status_code==422
    assert client.get('/api/runs/run/events/page?limit=201').status_code==422
    monkeypatch.setattr(settings,'env',settings.env|{'AUTH_CONTEXT_REQUIRED':'true','AUTH_CONTEXT_SECRET':SECRET})
    headers=sign_identity({'role':'member','workspace':'foreign'},SECRET)
    assert client.get('/api/runs/run/events/page',headers=headers).status_code==404

def test_unreadable_key_is_actionable_and_replacement_recovers_without_paid_request(store,monkeypatch):
    client,_=setup_rotation(store,monkeypatch)
    monkeypatch.setattr(settings,'env',settings.env|{'CONFIG_ENCRYPTION_KEY':'TEST_ONLY_DIFFERENT_ENCRYPTION'})
    calls=[]
    monkeypatch.setattr(transport,'completion',lambda *args:(calls.append(args) or {'choices':[{'message':{'content':'OK'}}]}))
    response=client.post('/api/admin/model-settings/test',json={})
    assert response.status_code==409 and 'Обновите' in response.json()['detail']
    assert OLD not in response.text and NEW not in response.text and not calls
    with store() as s:assert s.query(db.ProviderState).count()==0
    body=configuration(connection_mode='custom',base_url='https://models.example/v1',api_key=NEW)|{'version':2}
    runtime_config.save(runtime_config.ModelSettingsInput(**body),'owner:main')
    assert client.post('/api/admin/model-settings/test',json={}).status_code==200 and len(calls)==1

def test_rotation_changes_actual_credential_but_preserves_analysis_and_audits_version(store,monkeypatch):
    client,body=setup_rotation(store,monkeypatch)
    with store() as s:
        run=s.get(db.Run,'run');original=db.read(run.policy);data=s.get(db.Model,'model').data
    assert client.post('/api/admin/model-settings/researches/run/rotate',json=body).status_code==200
    with store() as s:
        run=s.get(db.Run,'run');policy=db.read(run.policy)
        assert policy['agent_config']==original['agent_config']|{'connection_version':2}
        assert policy['limits']==original['limits'] and s.get(db.Model,'model').data==data
        assert run.status=='partial' and run.generation==1 and run.calls==0
        assert s.get(db.Job,'job').status=='failed'  # Rotation never queues work.
        event=s.scalar(select(db.Event).where(db.Event.stage=='connection_rotation'))
        assert db.read(event.payload)=={'from_connection_version':1,'to_connection_version':2,'analysis_version':1,'actor':'owner:main','generation':1}
        assert OLD not in event.payload and NEW not in event.payload
    assert client.post('/api/runs/run/retry').status_code==200
    sent=[]
    def complete(payload,timeout,route):
        sent.append((payload,route));return {'choices':[{'message':{'content':'{"candidates":[],"coverage_sufficient":false,"coverage_summary":"No new data"}'}}],'usage':{'total_tokens':12}}
    monkeypatch.setattr(llm,'completion',complete)
    llm.invoke('run','discovery',{'sources':[]},Discovery,'unused',max_tokens=100)
    payload,route=sent[0]
    assert route['key']==NEW and route['url']=='https://models.example/v1' and route['isolated_tls'] is True
    assert payload['model']=='original-model' and original['agent_config']['prompts']['discovery'] in payload['messages'][0]['content']
    with store() as s:
        call=s.scalar(select(db.Invocation));assert call.connection_version==2 and call.prompt_version.startswith('runtime-1-')
    assert NEW not in client.get('/api/runs/run/export').text

@pytest.mark.parametrize('changes',[{'base_url':'https://another.example/v1'},{'base_url':'https://models.example/another-tenant'},{'connection_mode':'default'}])
def test_rotation_rejects_other_host_path_or_transport_mode(store,monkeypatch,changes):
    client,body=setup_rotation(store,monkeypatch,**changes)
    assert client.post('/api/admin/model-settings/researches/run/rotate',json=body).status_code==409
    with store() as s:
        assert db.read(s.get(db.Run,'run').policy)['agent_config']['connection_version']==1
        assert s.query(db.Event).filter_by(stage='connection_rotation').count()==0

@pytest.mark.parametrize('status,job_status',[('queued','queued'),('running','running'),('waiting','waiting'),('completed','running')])
def test_rotation_blocks_active_work(store,monkeypatch,status,job_status):
    client,body=setup_rotation(store,monkeypatch)
    with store.begin() as s:s.get(db.Run,'run').status=status;s.get(db.Job,'job').status=job_status
    response=client.post('/api/admin/model-settings/researches/run/rotate',json=body)
    assert response.status_code==409 and 'остановите' in response.json()['detail']

def test_rotation_requires_admin_workspace_and_fresh_versions(store,monkeypatch):
    client,body=setup_rotation(store,monkeypatch)
    path='/api/admin/model-settings/researches/run/rotate'
    for delta in [{'configuration_version':1},{'connection_version':0},{'generation':1}]:
        assert client.post(path,json=body|delta).status_code==409
    monkeypatch.setattr(settings,'env',settings.env|{'AUTH_CONTEXT_REQUIRED':'true','AUTH_CONTEXT_SECRET':SECRET})
    for role in ('member','viewer'):
        headers=sign_identity({'role':role,'workspace':'main' if role=='viewer' else 'private'},SECRET)
        assert client.post(path,json=body,headers=headers).status_code==403
        assert client.get('/api/admin/model-settings/researches',headers=headers).status_code==403
    admin=sign_identity({'role':'admin','workspace':'private'},SECRET)
    assert client.post(path,json=body,headers=admin).status_code==404
    assert client.get('/api/admin/model-settings/researches',headers=admin).json()['researches']==[]
    with store.begin() as s:s.get(db.Run,'run').workspace='private'
    assert client.post(path,json=body,headers=admin).status_code==200
    assert client.post(path,json=body,headers=admin).status_code==409

def test_unreadable_target_cannot_be_bound_and_default_endpoint_changes_are_blocked(store,monkeypatch):
    client,body=setup_rotation(store,monkeypatch)
    monkeypatch.setattr(settings,'env',settings.env|{'CONFIG_ENCRYPTION_KEY':'TEST_ONLY_DIFFERENT_ENCRYPTION'})
    assert client.post('/api/admin/model-settings/researches/run/rotate',json=body).status_code==409
    snapshot={'version':0,'connection_version':0,'server_id':runtime_config.server_id(runtime_config.defaults())}
    monkeypatch.setattr(settings,'BASE_URL','https://changed.example/v1')
    with pytest.raises(runtime_config.ConfigurationError):runtime_config.connection(snapshot)

def test_postgresql_simultaneous_rotation_is_applied_once(store,monkeypatch):
    client,body=setup_rotation(store,monkeypatch)
    if store.kw['bind'].dialect.name!='postgresql':pytest.skip('Requires PostgreSQL row and configuration locks')
    def rotate(_):return client.post('/api/admin/model-settings/researches/run/rotate',json=body).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(rotate,range(2)))==[200,409]
    with store() as s:
        assert s.get(db.Run,'run').generation==1
        assert s.query(db.Event).filter_by(stage='connection_rotation').count()==1


def test_pre_rotation_snapshot_format_can_rebind_without_changing_analysis(store,monkeypatch):
    client,body=setup_rotation(store,monkeypatch)
    with store.begin() as s:
        run=s.get(db.Run,'run');policy=db.read(run.policy)
        policy['agent_config'].pop('connection_version');policy['agent_config'].pop('server_id')
        run.policy=db.dump(policy)
    assert client.post('/api/admin/model-settings/researches/run/rotate',json=body).status_code==200
    with store() as s:
        snapshot=db.read(s.get(db.Run,'run').policy)['agent_config']
        assert snapshot['version']==1 and snapshot['connection_version']==2
        assert runtime_config.key_for(runtime_config.connection(snapshot,s))==NEW


def test_rotation_waits_for_a_running_direct_model_call(store,monkeypatch):
    client,body=setup_rotation(store,monkeypatch)
    with store.begin() as s:
        s.add(db.Invocation(id='direct',run_id='run',role='launch_brief',model='test',input='{}',input_hash='0'*64,status='running'))
    assert client.post('/api/admin/model-settings/researches/run/rotate',json=body).status_code==409
    with store() as s:assert s.get(db.Run,'run').generation==0
