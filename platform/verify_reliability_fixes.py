"""Live cursor/rotation contracts on one guarded disposable main-workspace fixture.

No model requests or jobs are started. Existing studies and global settings are
not changed. The fixture is removed in finally, guarded by ID, title and workspace.
"""
import json
import shlex
import uuid
from datetime import datetime,timezone
from ssh_client import ROOT,connect,execute
from verify_decision_workspace import Client


def remote(code):
    with connect() as ssh:
        return execute(ssh,'k3s kubectl -n venture-lens-demo exec deployment/api -- python -c '+shlex.quote(code))


def main():
    rid='fixqa'+uuid.uuid4().hex[:11]
    title='[RELIABILITY QA] Disposable cursor and rotation check '+rid
    fixture={'id':rid,'title':title}
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'fixture':rid,'paid_model_requests':0}
    clients=[]
    credentials=json.loads((ROOT/'.platform/public-demo-credentials.json').read_text())
    try:
        created=json.loads(remote("""
import json
from sqlalchemy import select,func
from backend import db,runtime_config,settings
fixture="""+repr(fixture)+"""
with db.Session.begin() as s:
 version,config=runtime_config.current(s)
 assert config['connection_mode']=='default' and version>0
 defaults=runtime_config.defaults();policy=settings.policy()
 policy['agent_config']={'version':0,'connection_version':0,'server_id':runtime_config.server_id(defaults),'models':defaults['models'],'prompts':defaults['prompts']}
 run=db.Run(id=fixture['id'],workspace='main',title=fixture['title'],status='partial',settings=db.dump({'region':'Global','sampling':'quick','adaptive_selection':True,'count':0,'focus':'SYNTHETIC QA; no recommendations','budget':0,'team':'SYNTHETIC QA'}),policy=db.dump(policy))
 s.add(run);s.flush()
 s.add_all([db.Event(run_id=run.id,stage='qa_fixture',message='SYNTHETIC QA event '+str(i)) for i in range(650)])
 print(json.dumps({'target_version':version,'policy':policy}))
"""))
        for role in ('owner','viewer'):
            client=Client({key:credentials[role][key] for key in ('username','password')});client.login();clients.append(client)
        owner,viewer=clients
        path='/api/runs/'+rid+'/events/page'
        page=owner.json(path);assert page['total']==650 and len(page['events'])==100
        assert page['events'][-1]['message']=='SYNTHETIC QA event 649'
        cursor=page['after'];seen=[row['id'] for row in page['events']]
        while page['has_more']:
            page=owner.json(path+'?before='+str(page['before']));seen.extend(row['id'] for row in page['events'])
        assert len(seen)==len(set(seen))==650
        remote("""
from backend import db
fixture="""+repr(fixture)+"""
with db.Session.begin() as s:
 r=s.get(db.Run,fixture['id']);assert r and r.workspace=='main' and r.title==fixture['title']
 s.add_all([db.Event(run_id=r.id,stage='qa_fixture',message='SYNTHETIC QA arrival '+str(i)) for i in range(510)])
""")
        more=True;arrivals=[]
        while more:
            page=owner.json(path+'?after='+str(cursor)+'&limit=200')
            arrivals.extend(row['id'] for row in page['events']);cursor=page['after'];more=page['has_more']
        assert len(arrivals)==len(set(arrivals))==510 and not set(seen).intersection(arrivals)
        rows=owner.json('/api/admin/model-settings/researches')['researches']
        choice=next(item for item in rows if item['id']==rid);assert choice['can_rotate']
        body={'configuration_version':created['target_version'],'connection_version':0,'generation':0}
        rotate='/api/admin/model-settings/researches/'+rid+'/rotate'
        assert viewer.request(rotate,'POST',body)[0]==403
        assert owner.request(rotate,'POST',body|{'generation':1})[0]==409
        assert owner.json(rotate,'POST',body)['status']=='rotated'
        assert owner.request(rotate,'POST',body)[0]==409
        run=owner.json('/api/runs/'+rid)
        expected=created['policy'];expected['agent_config']['connection_version']=created['target_version']
        assert run['policy']==expected and run['models']==[] and run['calls']==0 and run['status']=='partial'
        last=owner.json(path)['events'][-1]
        assert last['stage']=='connection_rotation'
        assert last['payload']['from_connection_version']==0 and last['payload']['to_connection_version']==created['target_version']
        report.update(history_events=650,new_events=510,cursors_unique_and_complete=True,
                      rotation='same-server default binding 0 to current version; no credentials changed',
                      analysis_preserved=True,no_jobs_started=True,audit_event=True,viewer_forbidden=True,stale_version_conflict=True)
    finally:
        for client in clients:client.logout()
        cleanup=remote("""
from sqlalchemy import select,func,delete
from backend import db
fixture="""+repr(fixture)+"""
with db.Session.begin() as s:
 run=s.get(db.Run,fixture['id'])
 if run:
  assert run.workspace=='main' and run.title==fixture['title']
  for table in (db.Model,db.Job,db.Invocation):assert s.scalar(select(func.count()).select_from(table).where(table.run_id==run.id))==0
  s.execute(delete(db.Event).where(db.Event.run_id==run.id));s.delete(run)
print('GUARDED_FIXTURE_REMOVED')
""")
        assert 'GUARDED_FIXTURE_REMOVED' in cleanup
        report['fixture_removed']=True
    (ROOT/'.platform/reliability-live-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
