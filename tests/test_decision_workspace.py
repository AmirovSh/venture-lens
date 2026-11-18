"""Decision support must preserve evidence gates and persist isolated human actions."""
import copy
from datetime import date,timedelta
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend import db
from backend.app import app
from backend.decision_support import model_support,research_summary,main_risk,hypothesis_name
from backend.schemas import PilotInput,LaunchEstimate
from backend.settings import policy
from test_pipeline import store,seed,assessment
from test_registration import registered,register,write,PASSWORD,ORIGIN


def ready_card(store,status='passed',score=19):
    seed(store)
    with store.begin() as s:
        run=s.get(db.Run,'run');run.status='completed_with_gaps'
        model=s.get(db.Model,'model');data=db.read(model.data);data['assessment']=assessment()
        if status=='review':data['assessment']['gates'][0]['status']='review'
        model.status=status;model.score=score if status=='passed' else None;model.score_upper=score if status=='passed' else None;model.coverage=100
        model.data=db.dump(data);s.get(db.Job,'job').status='completed'


def test_low_score_is_not_promoted_to_launch_recommendation(store):
    ready_card(store)
    with TestClient(app) as client:
        run=client.get('/api/runs/run').json();model=run['models'][0]
        assert model['status']=='passed' and model['score']==19
        assert model['support']['admitted'] and not model['support']['attractive']
        assert model['support']['verdict']=='assumptions' and run['summary']['priorities']==[]
        assert run['summary']['candidates_to_clarify']==['model']
        assert model['support']['brief']['budget_usd'] is None
        assert model['support']['budget_cap_usd']==10000


def test_recommendation_requires_both_gates_and_snapshot_thresholds(store):
    ready_card(store,score=75)
    with TestClient(app) as client:
        assert client.get('/api/runs/run').json()['summary']['priorities']==['model']
        with store.begin() as s:
            run=s.get(db.Run,'run');p=db.read(run.policy);p['decision_support']['minimum_priority_score']=80;run.policy=db.dump(p)
        assert client.get('/api/runs/run').json()['summary']['priorities']==[]
        with store.begin() as s:
            model=s.get(db.Model,'model');model.status='review'
        assert not client.get('/api/runs/run').json()['models'][0]['support']['admitted']


def test_main_risk_uses_verification_instead_of_first_critic_item():
    data={'critique':{'risks':[{'id':'a','title':'Speculation','statement':'Guess','impact':'high'},
                                {'id':'b','title':'Supported','statement':'Evidence','impact':'medium'}]},
          'verification':{'checks':[{'claim_id':'a','status':'judgment','grounded':False},
                                    {'claim_id':'b','status':'supported','grounded':True}]}}
    result=main_risk(data)
    assert result['claim_id']=='b' and result['confirmed']
    data['verification']['checks'][0].update(status='refuted',grounded=True)
    data['verification']['checks'][1].update(status='refuted',grounded=True)
    assert main_risk(data) is None


def test_similar_hypotheses_are_flagged_without_dropping_companies():
    models=[]
    for i,company in enumerate(['Analogue One','Analogue Two']):
        model=SimpleNamespace(name='Product',status='review',score=None,coverage=0)
        data={'candidate':{'description':'restaurant procurement recurring subscription platform'},'assessment':assessment()}
        support=model_support(model,data,{'team':'Founders'},policy())
        models.append({'id':str(i),'name':'Product','company':company,'data':data,'support':support,'status':'review','score':None})
    result=research_summary(models,policy())
    assert len(result['possible_duplicates'])==1 and len(models)==2
    assert result['priorities']==[]


def test_legacy_company_name_becomes_a_hypothesis_without_rewriting_database(store):
    ready_card(store)
    with store.begin() as s:s.get(db.Model,'model').name='Example'
    with TestClient(app) as client:
        card=client.get('/api/runs/run').json()['models'][0]
        assert card['name']=='SaaS' and card['original_name']=='Example' and card['company']=='Example'
    with store() as s:assert s.get(db.Model,'model').name=='Example'
    assert hypothesis_name('Our distinct product','Example',{'product':'Other description'})=='Our distinct product'


def test_screen_payload_is_compact_but_streamed_export_preserves_full_history(store):
    ready_card(store)
    with store.begin() as s:
        model=s.get(db.Model,'model');data=db.read(model.data)
        data['sources'][0]['text']='Large preserved source. '*2000
        data['analysis_history']=[{'data':{'old_source':'Saved historical material. '*2000}}]
        model.data=db.dump(data)
    with TestClient(app) as client:
        screen=client.get('/api/runs/run').json()['models'][0]['data']
        assert len(screen['sources'][0]['text'])==300 and screen['sources'][0]['preview']
        assert 'analysis_history' not in screen and screen['history_available']
        response=client.get('/api/runs/run/export');full=response.json()['models'][0]['data']
        assert len(full['sources'][0]['text'])>30000
        assert len(full['analysis_history'][0]['data']['old_source'])>30000
        assert response.headers['content-type'].startswith('application/json')


def test_export_concurrency_is_bounded_and_released(store):
    from backend.app import export_limiter
    ready_card(store)
    assert export_limiter.acquire(blocking=False)
    try:
        with TestClient(app) as client:
            response=client.get('/api/runs/run/export')
            assert response.status_code==429 and response.headers['Retry-After']=='5'
    finally:export_limiter.release()
    with TestClient(app) as client:
        assert client.get('/api/runs/run/export').status_code==200
        assert client.get('/api/runs/run/export').status_code==200


def test_task_materials_persist_export_and_never_change_admission(store):
    ready_card(store,status='review')
    body={'version':0,'status':'prepared','owner':'Counsel','note':'Check with local counsel; not yet independently verified','source_urls':['https://example.com/regulation']}
    with TestClient(app) as client:
        result=client.patch('/api/models/model/tasks/gate:legal_online',json=body)
        assert result.status_code==200 and result.json()['version']==1
        assert client.patch('/api/models/model/tasks/gate:legal_online',json=body).status_code==409
        exported=client.get('/api/runs/run/export').json()['models'][0]
        task=next(t for t in exported['support']['tasks'] if t['key']=='gate:legal_online')
        assert task['status']=='prepared' and task['owner']=='Counsel' and task['source_urls']==body['source_urls']
        assert exported['status']=='review' and exported['score'] is None
        assert exported['data']['assessment']['gates'][0]['status']=='review'
        assert client.patch('/api/models/model/tasks/gate:invented',json=body).status_code==404
        assert client.patch('/api/models/model/tasks/gate:legal_online',json=body|{'version':1,'source_urls':['javascript:alert(1)']}).status_code==422


def pilot_body(**changes):
    return {'version':0,'status':'planned','owner':'Founder','hypothesis':'Customers will pay for this problem','channel':'Direct customer interviews',
        'start_date':date.today().isoformat(),'due_date':(date.today()+timedelta(days=14)).isoformat(),'budget_usd':500,
        'success':'Two paid pilots','stop':'No demand after ten interviews',**changes}


def test_pilot_lifecycle_is_persisted_audited_and_optimistic(store):
    ready_card(store,score=75)
    with TestClient(app) as client:
        assert client.put('/api/models/model/pilot',json=pilot_body()).json()['version']==1
        active=pilot_body(version=1,status='active')
        assert client.put('/api/models/model/pilot',json=active).json()['version']==2
        assert client.put('/api/models/model/pilot',json=active).status_code==409
        completed=pilot_body(version=2,status='completed',paying_customers=2,payments=2,revenue_usd=120,spent_usd=80,result='Two customers paid; continue the experiment')
        assert client.put('/api/models/model/pilot',json=completed).status_code==200
        record=client.get('/api/runs/run/export').json()['models'][0]['support']['pilot']
        assert record['status']=='completed' and record['version']==3 and record['revenue_usd']==120 and record['spent_usd']==80
    with store() as s:
        events=list(s.scalars(select(db.Event).where(db.Event.stage=='pilot').order_by(db.Event.id)))
        assert len(events)==3 and db.read(events[-1].payload)['previous']['status']=='active'


def test_review_can_plan_but_cannot_start_paid_test_or_fabricate_result(store):
    ready_card(store,status='review')
    with TestClient(app) as client:
        assert client.put('/api/models/model/pilot',json=pilot_body()).status_code==200
        assert client.put('/api/models/model/pilot',json=pilot_body(version=1,status='active')).status_code==409
        assert client.put('/api/models/model/pilot',json=pilot_body(version=1,status='completed',result='Not actually run')).status_code==409
        assert client.put('/api/models/model/pilot',json=pilot_body(version=1,budget_usd=10001)).status_code==422
        assert client.put('/api/models/model/pilot',json=pilot_body(version=1,revenue_usd=1)).status_code==422


def test_pilot_dates_and_estimates_do_not_accept_impossible_ranges():
    with pytest.raises(ValueError):PilotInput.model_validate(pilot_body(due_date=(date.today()+timedelta(days=91)).isoformat()))
    with pytest.raises(ValueError):LaunchEstimate(low=1000,high=100,basis='No valid range',method='model_judgment',confidence='low',claim_ids=[])
    with pytest.raises(ValueError):LaunchEstimate(low=None,high=100,basis='Only one bound',method='human_required',confidence='low',claim_ids=[])


def test_reassessment_preserves_history_and_fences_old_work(store):
    ready_card(store,status='review')
    with TestClient(app) as client:
        client.patch('/api/models/model/tasks/gate:legal_online',json={'version':0,'status':'prepared','note':'New regulation found','source_urls':['https://example.com/new']})
        assert client.post('/api/models/model/reassess').status_code==202
        assert client.post('/api/models/model/reassess').status_code==409
    with store() as s:
        run=s.get(db.Run,'run');model=s.get(db.Model,'model');job=s.get(db.Job,'job');data=db.read(model.data)
        assert run.status=='queued' and run.generation==1 and job.epoch==1
        assert model.status=='queued' and 'assessment' not in data and len(data['analysis_history'])==1
        assert data['analysis_history'][0]['data']['assessment']['gates'][0]['status']=='review'
        assert data['additional_source_urls']==['https://example.com/new']


def test_active_pilot_and_llm_budget_block_reassessment(store):
    ready_card(store,score=75)
    with TestClient(app) as client:
        client.put('/api/models/model/pilot',json=pilot_body(status='active'))
        assert client.post('/api/models/model/reassess').status_code==409
        with store.begin() as s:
            s.delete(s.get(db.Pilot,'model'));s.get(db.Run,'run').calls=90
        assert client.post('/api/models/model/reassess').status_code==409


def test_personal_workspace_and_guest_cannot_modify_main_tasks_or_pilots(registered):
    client,database,_,backend=registered
    from sqlalchemy.orm import sessionmaker
    factory=sessionmaker(database.engine,expire_on_commit=False);ready_card(factory,status='review')
    assert register(client).status_code==303
    assert write(client,'/api/models/model/pilot',pilot_body(),method='PUT').status_code==404
    assert write(client,'/api/models/model/tasks/gate:legal_online',{'version':0,'status':'open'},method='PATCH').status_code==404
    assert write(client,'/api/models/model/reassess').status_code==404
    assert write(client,'/auth/logout').status_code==303
    assert client.post('/auth/login',data={'username':'demo','password':PASSWORD},headers={'Origin':ORIGIN}).status_code==303
    assert write(client,'/api/models/model/pilot',pilot_body(),method='PUT').status_code==403
    assert write(client,'/api/models/model/tasks/gate:legal_online',{'version':0,'status':'open'},method='PATCH').status_code==403


def test_first_login_preferences_persist_per_account_including_guest(registered):
    client,_,_,_=registered
    assert client.get('/auth/preferences').status_code==401
    assert register(client).status_code==303
    assert client.get('/auth/preferences').json()=={'onboarding_enabled':True,'onboarding_seen_version':0}
    preference={'onboarding_enabled':False,'onboarding_seen_version':1}
    assert write(client,'/auth/preferences',preference,method='PATCH').json()==preference
    assert client.patch('/auth/preferences',json=preference,headers={'Origin':'https://evil.invalid'}).status_code==403
    assert write(client,'/auth/logout').status_code==303
    assert client.post('/auth/login',data={'username':'demo','password':PASSWORD},headers={'Origin':ORIGIN}).status_code==303
    assert client.get('/auth/preferences').json()['onboarding_seen_version']==0
    assert write(client,'/auth/preferences',preference,method='PATCH').status_code==200
    assert write(client,'/auth/logout').status_code==303
    client.post('/auth/login',data={'username':'alice','password':PASSWORD},headers={'Origin':ORIGIN})
    assert client.get('/auth/preferences').json()==preference
    assert write(client,'/auth/preferences',preference|{'username':'demo'},method='PATCH').status_code==422
