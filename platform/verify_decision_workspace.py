"""Live contracts using existing QA accounts and explicitly synthetic private cards.

No LLM jobs are queued. Credentials/cookies never enter output. The exact test
run may be retained for browser QA with --keep-fixture, then removed by --cleanup.
"""
import argparse
import http.client
import http.cookies
import json
import shlex
import ssl
from datetime import date,datetime,timedelta,timezone
from urllib.parse import urlencode,urlsplit
import socks
from ssh_client import ROOT,connect,execute
from public_settings import public_origin
from configure_public_demo import private_permissions

FIXTURE=ROOT/'.platform/decision-ui-fixture.json'


class Client:
    def __init__(self,account):
        self.account=account;self.token=None;self.origin=public_origin();self.host=urlsplit(self.origin).hostname
        self.route=json.loads((ROOT/'.platform/server.json').read_text()).get('socks')

    def request(self,path,method='GET',body=None):
        sock=socks.socksocket()
        if self.route:sock.set_proxy(socks.SOCKS5,self.route['host'],self.route['port'])
        sock.settimeout(25);sock.connect((self.host,443));sock=ssl.create_default_context().wrap_socket(sock,server_hostname=self.host)
        connection=http.client.HTTPConnection(self.host,timeout=25);connection.sock=sock;headers={}
        if self.token:headers['Cookie']='__Host-venturelens_session='+self.token
        if method!='GET':headers['Origin']=self.origin
        if isinstance(body,dict):body=json.dumps(body).encode();headers['Content-Type']='application/json'
        try:
            connection.request(method,path,body,headers);response=connection.getresponse()
            return response.status,dict(response.headers),response.read()
        finally:connection.close()

    def json(self,path,method='GET',body=None,status=200):
        code,_,raw=self.request(path,method,body)
        assert code==status,f'{method} {path}: expected {status}, got {code}'
        return json.loads(raw) if raw and code!=303 else None

    def login(self):
        code,headers,_=self.request('/auth/login','POST',urlencode(self.account).encode())
        assert code==303,'Existing QA login failed: HTTP '+str(code)
        cookies=http.cookies.SimpleCookie();cookies.load(headers['Set-Cookie']);self.token=cookies['__Host-venturelens_session'].value
        return self.json('/auth/session')

    def logout(self):
        if self.token:self.request('/auth/logout','POST',b'');self.token=None


def remote(code,timeout=60):
    with connect() as c:return execute(c,'k3s kubectl -n venture-lens-demo exec deployment/api -- python -c '+shlex.quote(code),timeout=timeout)


def cleanup(fixture,reset=False):
    # These IDs were created here and are guarded by both workspace and title.
    remote("""
from sqlalchemy import select,delete
from backend.db import Session,Run,Model,Job,Event,Invocation,EvidenceAction,Pilot
fixture="""+repr(fixture)+"""
with Session.begin() as s:
 run=s.get(Run,fixture['run_id'])
 if run:
  assert run.workspace==fixture['workspace'] and run.title.startswith('[UX CHECK]')
  ids=list(s.scalars(select(Model.id).where(Model.run_id==run.id)))
  assert set(ids)==set(fixture['model_ids'])
  s.execute(delete(EvidenceAction).where(EvidenceAction.model_id.in_(ids)))
  s.execute(delete(Pilot).where(Pilot.model_id.in_(ids)))
  if not """+repr(reset)+""":
   s.execute(delete(Event).where(Event.run_id==run.id));s.execute(delete(Invocation).where(Invocation.run_id==run.id))
   s.execute(delete(Job).where(Job.run_id==run.id));s.execute(delete(Model).where(Model.run_id==run.id));s.delete(run)
print('Only owned UX fixture '+('reset' if """+repr(reset)+""" else 'removed'))
""")


def seed(workspace):
    from fixtures import assessment,QUOTE
    a=assessment();a.update(summary='SYNTHETIC UX CHECK: проверка интерфейса, не бизнес-рекомендация',customer='Тестовый небольшой магазин',first_sale='Десять интервью с тестовыми покупателями')
    a['launch_brief']={'product':'Тестовый сервис закупок по подписке для небольших магазинов','first_customer':'Тестовый владелец магазина','acquisition_channel':'Тестовые интервью, без реальных рассылок','team':'Тестовая команда',
        'budget_usd':{'low':100,'high':500,'basis':'SYNTHETIC UX CHECK, не рыночная оценка','method':'model_judgment','confidence':'low','claim_ids':[]},
        'first_payment_days':{'low':14,'high':30,'basis':'SYNTHETIC UX CHECK, не прогноз','method':'model_judgment','confidence':'low','claim_ids':[]},'assumptions':['Только проверка функций интерфейса']}
    a['unknowns']=['Тестовая задача: записать результат интервью']
    code="""
import json
from backend.db import Session,Run,Model,uid,dump
from backend.settings import policy
workspace="""+repr(workspace)+"""
a="""+repr(a)+"""
with Session.begin() as s:
 run=Run(id=uid(),workspace=workspace,title='[UX CHECK] Только тест интерфейса',status='completed',settings=dump({'region':'US','count':10,'budget':10000,'team':'Тестовая команда','focus':'SYNTHETIC UX CHECK'}),policy=dump(policy()))
 s.add(run);s.flush();ids=[]
 for i in range(2):
  import copy
  data=copy.deepcopy(a)
  if i:data['gates'][0].update(status='review',reason='Тест: требуется подтверждение юридического условия')
  model=Model(id=uid(),run_id=run.id,name='Тестовая гипотеза '+str(i+1),company='SYNTHETIC analogue '+str(i+1),status='review' if i else 'passed',stage='completed',score=None if i else 75,score_upper=None if i else 80,coverage=100,data=dump({'candidate':{'name':'Тестовая гипотеза','company':'SYNTHETIC analogue','description':'Только тест интерфейса','business_model_key':'small-retailer-procurement-subscription'},'assessment':data}))
  s.add(model);s.flush();ids.append(model.id)
 print(json.dumps({'run_id':run.id,'workspace':workspace,'model_ids':ids}))
"""
    fixture=json.loads(remote(code));FIXTURE.write_text(json.dumps(fixture,indent=2)+'\n');private_permissions(FIXTURE)
    return fixture


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--keep-fixture',action='store_true');parser.add_argument('--cleanup',action='store_true');args=parser.parse_args()
    if args.cleanup:
        if FIXTURE.exists():cleanup(json.loads(FIXTURE.read_text()));FIXTURE.unlink()
        return
    if FIXTURE.exists():raise RuntimeError('An owned UX fixture already exists; finish browser QA or run --cleanup first')
    accounts=json.loads((ROOT/'.platform/registration-check-accounts.json').read_text())['accounts']
    access=json.loads((ROOT/'.platform/public-demo-credentials.json').read_text())
    clients=[Client({key:a[key] for key in ('username','password')}) for a in [accounts[0],accounts[1],access['viewer'],access['owner']]]
    fixture=None;success=False;original_preferences=None
    try:
        identities=[client.login() for client in clients];subject,other,guest,owner=clients
        original_preferences=subject.json('/auth/preferences')
        value={'onboarding_enabled':False,'onboarding_seen_version':1}
        subject.json('/auth/preferences','PATCH',value)
        subject.logout();subject.login();assert subject.json('/auth/preferences')==value
        fixture=seed(identities[0]['workspace']);run_path='/api/runs/'+fixture['run_id'];passed,review=fixture['model_ids']
        detail=subject.json(run_path)
        assert detail['summary']['priorities']==[passed] and len(detail['summary']['possible_duplicates'])==1
        task=next(t for m in detail['models'] if m['id']==review for t in m['support']['tasks'] if t['kind']=='criterion')
        task_path='/api/models/'+review+'/tasks/'+task['key']
        body={'version':0,'status':'prepared','owner':'UX QA','note':'SYNTHETIC UX CHECK: тест записи материалов','source_urls':['https://example.com/ux-check']}
        subject.json(task_path,'PATCH',body);subject.json(task_path,'PATCH',body,status=409)
        exported=subject.json(run_path+'/export');card=next(m for m in exported['models'] if m['id']==review)
        assert card['status']=='review' and card['score'] is None and card['support']['tasks'][0]['status']=='prepared'
        for client in [other,guest,owner]:
            client.json(run_path,status=404);client.json(run_path+'/export',status=404)
        other.json('/api/models/'+passed+'/pilot','PUT',{'version':0},status=404)
        guest.json(task_path,'PATCH',body,status=403)
        plan={'version':0,'status':'planned','owner':'UX QA','hypothesis':'SYNTHETIC UX CHECK','channel':'Тестовый канал','start_date':date.today().isoformat(),'due_date':(date.today()+timedelta(days=14)).isoformat(),'budget_usd':100,'success':'Тестовый критерий','stop':'Тестовый стоп-критерий'}
        subject.json('/api/models/'+review+'/pilot','PUT',plan)
        subject.json('/api/models/'+review+'/pilot','PUT',plan|{'version':1,'status':'active'},status=409)
        subject.json('/api/models/'+passed+'/pilot','PUT',plan)
        subject.json('/api/models/'+passed+'/pilot','PUT',plan|{'version':1,'status':'active'})
        subject.json('/api/models/'+passed+'/reassess','POST',{},status=409)
        subject.json('/api/models/'+passed+'/pilot','PUT',plan|{'version':2,'status':'completed','paying_customers':1,'payments':1,'revenue_usd':10,'spent_usd':5,'result':'SYNTHETIC UX CHECK: проверка сохранения результата'})
        result=next(m for m in subject.json(run_path+'/export')['models'] if m['id']==passed)['support']['pilot']
        assert result['version']==3 and result['revenue_usd']==10 and result['status']=='completed'
        main=owner.json('/api/runs');cards=[m for r in main for m in owner.json('/api/runs/'+r['id'])['models']]
        assert len(cards)==20 and all(m['support']['brief']['origin']=='model_estimate' and m['support']['brief_provenance']['invocation_id'] for m in cards)
        proof={'checked_at':datetime.now(timezone.utc).isoformat(),'url':public_origin(),'private_synthetic_qa_only':True,'no_paid_jobs_queued':True,'main_cards_preserved':len(cards),'real_launch_briefs':20,'evidence_task_persisted_and_exported':True,'prepared_materials_do_not_admit':True,'stale_task_update_rejected':True,'review_cannot_start_pilot':True,'pilot_lifecycle_and_metrics_persisted':True,'active_pilot_blocks_reassessment':True,'foreign_member_owner_viewer_denied_private_run':True,'viewer_mutation_denied':True,'onboarding_disable_survives_relogin':True,'possible_duplicates_visible':True}
        (ROOT/'.platform/decision-live-report.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof,indent=2));success=True
    finally:
        if fixture:
            if args.keep_fixture and success:cleanup(fixture,reset=True)
            else:cleanup(fixture);FIXTURE.unlink(missing_ok=True)
        if original_preferences:
            try:clients[0].json('/auth/preferences','PATCH',original_preferences)
            except Exception:print('QA preference restore pending; original value retained only in process state')
        for client in clients:client.logout()


if __name__=='__main__':main()
