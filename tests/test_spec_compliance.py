"""Specification boundaries: reasons, provenance and contradictory admission evidence."""
import copy
import time
import pytest
from sqlalchemy import select
from backend import db, pipeline
from backend.consistency import reconcile
from backend.passports import normalize_data
from backend.settings import policy
from test_pipeline import store, seed, assessment, source


def test_rejection_event_contains_human_reason_and_evidence_ids(store, monkeypatch):
    seed(store)
    result=assessment();result['gates'][0].update(status='reject',reason='Requires our own financial licence')
    monkeypatch.setattr(pipeline,'invoke',lambda *args,**kwargs:copy.deepcopy(result))
    pipeline.process_model('model')
    with store() as s:
        event=s.scalar(select(db.Event).where(db.Event.stage=='rejected'))
        assert 'legal_online' in event.message and 'Requires our own financial licence' in event.message
        assert db.read(event.payload)['rejections']==[{'criterion_id':'legal_online','reason':'Requires our own financial licence','claim_ids':['c1']}]


def test_verified_conflict_removes_persisted_score_without_rewriting_original_screening(store,monkeypatch):
    seed(store)
    independent=source('s2');independent['text']='Operators of this proposed activity must obtain their own financial licence before serving a first customer.'
    def invoke(run_id,role,packet,*args,**kwargs):
        if role=='screening':return assessment()
        if role=='critic':return {'risks':[{'id':'r1','title':'Licence required','statement':'This activity requires our own licence','impact':'high','factual':True,'search_query':'licence','confidence':'high','method':'primary_source','source_id':'s1','quote':source()['text'],'source_date':None}]}
        if role=='verification_search':return {'queries':['independent company licence']}
        assert set(packet)=={'company','claims','sources'}
        return {'checks':[{'claim_id':'r1','status':'supported','explanation':'Independent source supports licence requirement','source_id':'s2','quote':independent['text'],'source_date':None,'confidence':'high','criterion_impacts':[{'criterion_id':'legal_online','effect':'contradicts','reason':'The proposed activity needs our own licence before the first client'}]}]}
    monkeypatch.setattr(pipeline,'invoke',invoke);monkeypatch.setattr(pipeline,'collect',lambda *args,**kwargs:[independent])
    pipeline.process_model('model')
    with store() as s:
        model=s.get(db.Model,'model');data=db.read(model.data)
        assert model.status=='review' and model.score is None and model.score_upper is None
        assert data['assessment']['gates'][0]['status']=='review'
        assert data['screening_snapshot']['gates'][0]['status']=='pass'
        assert data['consistency']['checked'] and data['consistency']['findings'][0]['criterion_id']=='legal_online'
        assert s.scalar(select(db.Event).where(db.Event.stage=='consistency')) is not None
        for claim in data['assessment']['claims']+data['critique']['risks']+data['verification']['checks']:
            assert claim['passport']['date'] and claim['passport']['method'] and claim['passport']['source_status']


@pytest.mark.parametrize('status,grounded,factual', [('refuted',True,True),('unknown',False,True),('supported',False,True),('supported',True,False)])
def test_unverified_or_refuted_risk_does_not_invent_a_gate_conflict(status,grounded,factual):
    data={'assessment':assessment(),'critique':{'risks':[{'id':'r','factual':factual}]},
          'verification':{'contract_version':'3.0','checks':[{'claim_id':'r','status':status,'grounded':grounded,
              'criterion_impacts':[{'criterion_id':'legal_online','effect':'contradicts','reason':'Claimed contradiction'}]}]}}
    assert reconcile(data,policy())==[]
    assert all(g['status']=='pass' for g in data['assessment']['gates'])


def test_judgment_passport_has_real_invocation_time_and_explicit_source_applicability():
    created=1700000000.0
    data={'assessment':{'claims':[{'id':'j','method':'model_judgment','confidence':'low','source_id':None}]},
          'verification':{'checks':[{'claim_id':'j','status':'judgment','source_id':None,'confidence':'low'}]}}
    provenance={'screening':{'id':'actual-call','role':'screening','model':'test-model','created':created},
                'verifier':{'id':'check-call','role':'verifier','model':'test-model','created':created+10}}
    normalize_data(data,provenance,created-100)
    claim=data['assessment']['claims'][0]['passport'];check=data['verification']['checks'][0]['passport']
    assert claim['source_url'] is None and claim['source_status']=='not_applicable'
    assert claim['generated_at']==created and claim['generated_at_basis']=='invocation' and claim['date_kind']=='generated'
    assert check['method']=='model_judgment' and check['generated_at']==created+10


def test_consistency_does_not_claim_a_new_conflict_for_an_unadmitted_gate():
    screening=assessment();screening['gates'][-1]['status']='review'
    data={'assessment':screening,'screening_snapshot':copy.deepcopy(screening),
          'critique':{'risks':[{'id':'r','factual':True}]},
          'verification':{'contract_version':'3.0','checks':[{'claim_id':'r','status':'partial','grounded':True,
             'criterion_impacts':[{'criterion_id':'large_analogue','effect':'contradicts','reason':'Market capitalization is above the threshold'}]}]}}
    assert reconcile(data,policy())==[]
    assert data['assessment']['gates'][-1]['status']=='review'


def test_recheck_keeps_history_sources_and_resets_fencing_for_completed_with_gaps(store,monkeypatch):
    from scripts import recheck
    monkeypatch.setattr(recheck,'Session',store)
    seed(store)
    with store.begin() as s:
        run=s.get(db.Run,'run');run.status='completed_with_gaps'
        model=s.get(db.Model,'model');data=db.read(model.data)
        data.update(assessment=assessment(),verification={'checks':[]},verification_sources=[source('v')],verification_queries=['query'])
        model.data=db.dump(data);model.status='review'
        job=s.get(db.Job,'job');job.status='completed';job.attempts=5;job.epoch=7
    recheck.main('run',reuse_sources=True)
    with store() as s:
        run=s.get(db.Run,'run');job=s.get(db.Job,'job');data=db.read(s.get(db.Model,'model').data)
        assert run.status=='queued' and run.generation==1 and job.epoch==8 and job.attempts==0
        assert data['verification_history'][0]['result']=={'checks':[]}
        assert data['verification_sources'][0]['id']=='v' and data['reuse_verification_sources']


def test_ci_wire_provider_matches_current_llm_response_schemas():
    import importlib.util,json
    from pathlib import Path
    from backend.schemas import Assessment,Critique,Verification,SearchPlan,Discovery
    spec=importlib.util.spec_from_file_location('ci_spec_fixtures',Path(__file__).parents[1]/'platform/fixtures.py')
    fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)
    responses=json.loads(fixtures.resources('isolated-test','test-image')[0]['data']['responses.json'])
    for role,schema in [('screening',Assessment),('critic',Critique),('verifier',Verification),('verification_search',SearchPlan),('discovery',Discovery)]:
        schema.model_validate(responses[role])
