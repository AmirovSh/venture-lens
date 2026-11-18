"""Verify product controls at API, provider payload and persisted-state boundaries."""
import copy
import json
import time
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend import db,pipeline,llm,runtime_config,settings
from backend.app import app
from backend.auth_context import sign_identity
from backend.execution import DependencyUnavailable
from backend.schemas import Discovery
from test_pipeline import store,seed,source
from test_registration import registered,register,write,ORIGIN,PASSWORD,SECRET

def configuration(**changes):
    value=runtime_config.defaults();value.pop('encrypted_key');value.update(version=0,**changes)
    return value

def login_owner(client):
    return client.post('/auth/login',data={'username':'owner','password':PASSWORD},headers={'Origin':ORIGIN})

def test_market_and_adaptive_selection_are_snapshotted_without_fixed_count(store):
    client=TestClient(app)
    assert client.post('/api/runs',json={'region':'Custom'}).status_code==422
    response=client.post('/api/runs',json={'region':'Custom','market':'Бразилия','sampling':'quick','focus':'Scheduling for independent tutors','source_urls':['https://example.com/research']})
    assert response.status_code==201
    with store() as s:
        run=s.get(db.Run,response.json()['id']);config=db.read(run.settings);policy=db.read(run.policy)
        assert config['market']=='Бразилия' and config['adaptive_selection'] and config['count']==4
        assert config['manual_sources'][0]['url']=='https://example.com/research'
        assert set(policy['agent_config']['models'])==set(runtime_config.ROLES)
        assert 'encrypted_key' not in json.dumps(policy) and 'TEST_ONLY_KEY' not in json.dumps(policy)
    assert client.post('/api/runs',json={'count':1}).status_code==201  # Legacy callers can request smaller research.

def candidate(name,key):
    return {'name':name,'company':name,'description':'Subscription hypothesis','query':name+' pricing','discovery_source_id':'s1','business_model_key':key}

def discovery_run(store):
    with store.begin() as s:
        config={'region':'Custom','market':'Brazil','sampling':'balanced','adaptive_selection':True,'count':8,'focus':'Tutor scheduling','team':'Two developers','budget':5000,'discovery_sources':[source()]}
        s.add(db.Run(id='discovery',title='Unrelated UI title',status='running',settings=db.dump(config),policy=db.dump(settings.policy())))

def test_discovery_uses_selected_conditions_and_stops_at_coverage(store,monkeypatch):
    discovery_run(store);packets=[]
    def invoke(run,role,packet,*args,**kwargs):
        packets.append(packet)
        return {'candidates':[candidate('A','tutor-scheduling-subscription'),candidate('Duplicate','tutor-scheduling-subscription'),candidate('B','tutor-billing-subscription')],'coverage_sufficient':True,'coverage_summary':'Scheduling and billing cover the narrow task.'}
    monkeypatch.setattr(pipeline,'invoke',invoke)
    pipeline.discover('discovery')
    assert len(packets)==1 and packets[0]['market']=='Brazil' and packets[0]['focus']=='Tutor scheduling'
    assert packets[0]['batch_size']==4 and 'Unrelated UI title' not in json.dumps(packets)
    with store() as s:
        assert s.query(db.Model).filter_by(run_id='discovery').count()==2
        selection=db.read(s.get(db.Run,'discovery').settings)['selection']
        assert selection['stop_reason']=='coverage' and selection['found']==2

def test_discovery_retry_does_not_repay_completed_round(store,monkeypatch):
    discovery_run(store);calls=[]
    def invoke(run,role,packet,*args,**kwargs):
        calls.append(packet)
        if len(calls)==1:return {'candidates':[candidate('A','one'),candidate('B','two')],'coverage_sufficient':False}
        if len(calls)==2:raise DependencyUnavailable('MODEL_SERVER_WAIT',1)
        return {'candidates':[candidate('A again','one')],'coverage_sufficient':False,'coverage_summary':'No new variants.'}
    monkeypatch.setattr(pipeline,'invoke',invoke)
    with pytest.raises(DependencyUnavailable):pipeline.discover('discovery')
    with store() as s:assert db.read(s.get(db.Run,'discovery').settings)['selection_progress']['round']==1
    pipeline.discover('discovery')
    assert len(calls[2]['existing_models'])==2
    with store() as s:
        assert s.query(db.Model).filter_by(run_id='discovery').count()==2
        assert db.read(s.get(db.Run,'discovery').settings)['selection']['stop_reason']=='no_new_hypotheses'

def test_admin_settings_are_role_scoped_and_keys_never_echo(registered,monkeypatch):
    client,_,_,_=registered
    assert register(client).status_code==303
    assert client.get('/api/admin/model-settings').status_code==403
    assert write(client,'/api/admin/model-settings',configuration(),method='PUT').status_code==403
    write(client,'/auth/logout');assert login_owner(client).status_code==303
    initial=client.get('/api/admin/model-settings').json();assert initial['version']==0 and initial['base_url']==''
    bad=configuration(api_key='a secret too short',models={})
    response=write(client,'/api/admin/model-settings',bad,method='PUT')
    assert response.status_code==422 and bad['api_key'] not in response.text
    from backend import evidence
    monkeypatch.setattr(evidence,'public_url',lambda url:url)
    secret='TEST_ONLY_EXTERNAL_SERVER_KEY_abcdefghijk'
    body=configuration(connection_mode='custom',base_url='https://models.example/v1',api_key=secret)
    saved=write(client,'/api/admin/model-settings',body,method='PUT')
    assert saved.status_code==200 and saved.json()['version']==1 and saved.json()['key_configured']
    assert secret not in saved.text and 'encrypted_key' not in saved.text
    with db.Session() as s:
        row=s.get(db.ServiceConfiguration,1);assert secret not in row.data
        assert runtime_config.key_for(db.read(row.data))==secret
    assert write(client,'/api/admin/model-settings',body,method='PUT').status_code==409
    changed=body|{'version':1,'base_url':'https://another.example/v1'};changed.pop('api_key')
    assert write(client,'/api/admin/model-settings',changed,method='PUT').status_code==409
    assert client.put('/api/admin/model-settings',json=body,headers={'Origin':'https://evil.invalid'}).status_code==403

def test_model_and_prompt_snapshot_controls_actual_provider_payload(store,monkeypatch):
    seed(store);body=configuration();body['models']['discovery']='selected-model';body['prompts']['discovery']='First administrator instruction, kept for this research.'
    runtime_config.save(runtime_config.ModelSettingsInput(**body),'owner:main')
    with store.begin() as s:
        run=s.get(db.Run,'run');policy=db.read(run.policy);policy['agent_config']=runtime_config.snapshot(s);run.policy=db.dump(policy)
    body.update(version=1);body['models']['discovery']='later-model';body['prompts']['discovery']='New administrator instruction applies only to later research.'
    runtime_config.save(runtime_config.ModelSettingsInput(**body),'owner:main')
    seen=[]
    def complete(payload,timeout,route=None):
        seen.append((payload,route));return {'choices':[{'message':{'content':'{"candidates":[],"coverage_sufficient":false,"coverage_summary":"No data"}'}}],'usage':{'total_tokens':22}}
    monkeypatch.setattr(llm,'completion',complete)
    llm.invoke('run','discovery',{'sources':[]},Discovery,'Fallback instruction',max_tokens=100)
    assert seen[0][0]['model']=='selected-model' and seen[0][1]['model']=='selected-model'
    prompt=seen[0][0]['messages'][0]['content'];assert 'First administrator instruction' in prompt and llm.SYSTEM in prompt and 'New administrator instruction' not in prompt
    with store() as s:
        call=s.scalar(select(db.Invocation));assert call.prompt_text==prompt and call.prompt_version.startswith('runtime-1-')

def test_connection_check_obeys_shared_request_limit(store,monkeypatch):
    body=configuration(requests_per_minute=1);runtime_config.save(runtime_config.ModelSettingsInput(**body),'owner:main')
    from backend import transport
    calls=[]
    monkeypatch.setattr(transport,'completion',lambda *args:(calls.append(args) or {'choices':[{'message':{'content':'OK'}}]}))
    client=TestClient(app)
    assert client.post('/api/admin/model-settings/test',json={}).status_code==200
    assert client.post('/api/admin/model-settings/test',json={}).status_code==503
    assert len(calls)==1
    with store() as s:assert s.query(db.Run).count()==0

def test_manual_source_links_remain_pending_and_workspace_scoped(store,monkeypatch):
    seed(store)
    from backend import evidence
    monkeypatch.setattr(evidence,'public_url',lambda url:url)
    client=TestClient(app)
    response=client.post('/api/runs/run/source-links',json={'model_id':'model','urls':['https://example.com/new']})
    assert response.status_code==201
    assert client.post('/api/runs/run/source-links',json={'model_id':'model','urls':['https://example.com/new']}).json()['added']==0
    with store() as s:
        assert len(db.read(s.get(db.Run,'run').settings)['manual_sources'])==1
        assert 'assessment' not in db.read(s.get(db.Model,'model').data)  # No invented evidence/admission.
        assert s.query(db.Invocation).count()==0
    monkeypatch.setattr(settings,'env',settings.env|{'AUTH_CONTEXT_SECRET':SECRET,'AUTH_CONTEXT_REQUIRED':'true'})
    headers=sign_identity({'workspace':'foreign-space','role':'member'},SECRET)
    assert client.post('/api/runs/run/source-links',json={'urls':['https://example.com/foreign']},headers=headers).status_code==404

def test_contextual_instructions_merge_seen_pages_and_can_be_disabled(registered):
    client,_,_,_=registered;register(client)
    first={'onboarding_enabled':True,'onboarding_seen_version':2,'onboarding_seen_sections':['overview']}
    assert write(client,'/auth/preferences',first,method='PATCH').status_code==200
    stale={'onboarding_enabled':False,'onboarding_seen_version':2,'onboarding_seen_sections':['sources']}
    result=write(client,'/auth/preferences',stale,method='PATCH').json()
    assert result['onboarding_seen_sections']==['overview','sources'] and not result['onboarding_enabled']
    assert write(client,'/auth/preferences',stale|{'onboarding_seen_sections':['unknown-section']},method='PATCH').status_code==422
    write(client,'/auth/logout')
    client.post('/auth/login',data={'username':'alice','password':PASSWORD},headers={'Origin':ORIGIN})
    assert client.get('/auth/preferences').json()==result

def test_added_source_is_fetched_during_reassessment_without_inventing_gates(store,monkeypatch):
    from test_decision_workspace import ready_card
    ready_card(store,status='review')
    from backend import evidence
    monkeypatch.setattr(evidence,'public_url',lambda url:url)
    client=TestClient(app)
    assert client.post('/api/runs/run/source-links',json={'model_id':'model','urls':['https://example.com/new']}).status_code==201
    assert client.post('/api/models/model/reassess').status_code==202
    fetched=[]
    monkeypatch.setattr(pipeline,'collect',lambda *args,**kwargs:[source()])
    def fetch(url):
        fetched.append(url);value=source('manual');value['url']=url;return value
    monkeypatch.setattr(pipeline,'fetch',fetch)
    def stop_after_packet(run,role,packet,*args,**kwargs):
        assert role=='screening' and any(item['url']=='https://example.com/new' for item in packet['sources'])
        raise DependencyUnavailable('Test stopped after input boundary',1)
    monkeypatch.setattr(pipeline,'invoke',stop_after_packet)
    with pytest.raises(DependencyUnavailable):pipeline.process_model('model')
    assert fetched==['https://example.com/new']
    with store() as s:
        data=db.read(s.get(db.Model,'model').data)
        assert data['analysis_history'] and 'assessment' not in data
        assert s.query(db.Invocation).count()==0

def test_custom_server_does_not_receive_system_client_certificate(monkeypatch):
    from backend import transport
    certificates=[];clients=[]
    class Context:
        def load_cert_chain(self,*args):certificates.append(args)
    monkeypatch.setattr(transport.ssl,'create_default_context',lambda **kwargs:Context())
    original=transport.setting
    monkeypatch.setattr(transport,'setting',lambda key,default='':{'LLM_CLIENT_CERT_FILE':'private-client-cert','LLM_CLIENT_KEY_FILE':'private-client-key'}.get(key,original(key,default)))
    class Response:
        is_success=True
        def json(self):return {'choices':[]}
    class Client:
        def __init__(self,**kwargs):clients.append(kwargs)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):return Response()
    monkeypatch.setattr(transport.httpx,'Client',Client)
    transport.completion({'model':'other-model'},1,{'url':'https://models.example/v1','key':'TEST_ONLY_KEY','isolated_tls':True})
    assert not certificates and clients[0]['trust_env'] is False
