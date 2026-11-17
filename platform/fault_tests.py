"""Destructive checks restricted to our disposable lab; never uses the default context."""
import json
import time
from pathlib import Path
import urllib.request
from lab import kube,STATE,NAMESPACE,private_values
from fixtures import resources,seed_script

REPORT={'environment':'local-disposable','checks':[]}

def wait(description,check,timeout=180):
    deadline=time.monotonic()+timeout;last=None
    while time.monotonic()<deadline:
        try:
            last=check()
            if last:return last
        except Exception as error:last=type(error).__name__
        time.sleep(2)
    raise RuntimeError(description+' timed out: '+str(last))

def passed(name,**details):
    REPORT['checks'].append({'check':name,'passed':True,**details});print('PASS',name,flush=True)

def inside(code):
    return kube('-n',NAMESPACE,'exec','deployment/api','--','python','-c',code,capture_output=True,text=True).stdout.strip()

def control(**values):
    inside("import urllib.request,json;urllib.request.urlopen(urllib.request.Request('http://llm-fixture:8080/control',data="+repr(json.dumps(values).encode())+",headers={'Content-Type':'application/json'}),timeout=5).read()")

def seed(title):return inside(seed_script('[FAULT TEST] '+title)).splitlines()[-1]

def state(run):
    return json.loads(inside("from backend.db import Session,Run,Job,Model,read;from sqlalchemy import select;import json\nwith Session() as s:\n r=s.get(Run,"+repr(run)+");j=s.scalar(select(Job).where(Job.run_id==r.id));m=s.scalar(select(Model).where(Model.run_id==r.id));print(json.dumps({'run':r.status,'job':j.status,'epoch':j.epoch,'model':m.status,'id':m.id,'score':m.score,'sources':read(m.data).get('sources',[])}))"))

def completed(run):
    result=state(run)
    if result['run'] in ('partial','cancelled'):raise RuntimeError(result)
    return result if result['run'].startswith('completed') else None

def scale(kind,name,count):kube('-n',NAMESPACE,'scale',kind+'/'+name,'--replicas='+str(count))
def rollout(kind,name):kube('-n',NAMESPACE,'rollout','status',kind+'/'+name,'--timeout=180s')
def pod(app):return json.loads(kube('-n',NAMESPACE,'get','pods','-l','app='+app,'-o','json',capture_output=True,text=True).stdout)['items'][0]['metadata']['name']

def configure(values):
    secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'venture-lens-runtime','namespace':NAMESPACE},'type':'Opaque','stringData':values}
    kube('apply','-f','-',input=json.dumps(secret).encode(),capture_output=True)
    for name in ['api','worker']:kube('-n',NAMESPACE,'rollout','restart','deployment/'+name)
    for name in ['api','worker']:rollout('deployment',name)

def main():
    ns=json.loads(kube('get','namespace',NAMESPACE,'-o','json',capture_output=True,text=True).stdout)
    if ns['metadata']['labels'].get('venturelens.dev/environment')!='local-disposable':raise RuntimeError('Fault tests require the disposable lab label')
    argo=kube('-n','argocd','get','application','venture-lens','--ignore-not-found','-o','json',capture_output=True,text=True).stdout
    automated=json.loads(argo).get('spec',{}).get('syncPolicy',{}).get('automated') if argo.strip() else None
    if automated is not None:kube('-n','argocd','patch','application','venture-lens','--type=merge','-p',json.dumps({'spec':{'syncPolicy':{'automated':None}}}))
    original=private_values();tag=json.loads((STATE/'release.json').read_text())['tag']
    try:
        for item in resources(NAMESPACE,'venture-lens-api:'+tag):kube('apply','-f','-',input=json.dumps(item).encode(),capture_output=True)
        rollout('deployment','llm-fixture')
        configure({**original,'LLM_BASE_URL':'http://llm-fixture:8080/v1','LLM_API_KEY':'synthetic-test-only','SEARXNG_URL':''})
        control(delay=0,failures=0)
        run=seed('full cycle');result=wait('full cycle',lambda:completed(run))
        assert result['model']=='passed' and result['score']==80,result
        inside("from fastapi.testclient import TestClient;from backend.app import app;c=TestClient(app);assert c.patch('/api/models/"+result['id']+"/decision',json={'decision':'shortlist','note':'Synthetic fault test'}).status_code==200")
        passed('full_cycle_and_persisted_score',run_id=run,score=result['score'])
        previous=pod('api');kube('-n',NAMESPACE,'delete','pod',previous,'--wait=false')
        wait('API replacement',lambda:pod('api')!=previous);rollout('deployment','api')
        checksum=result['sources'][0]['sha256']
        assert inside("from backend.storage import get_source;print(len(get_source("+repr(checksum)+")))").isdigit()
        inside("from backend.db import Session,Model\nwith Session() as s:assert s.get(Model,"+repr(result['id'])+").decision=='shortlist'")
        passed('api_recreated_database_and_s3_artifacts_preserved')
        control(delay=12)
        takeover=seed('worker kill')
        wait('job ownership',lambda:state(takeover)['job']=='running')
        previous=pod('worker');kube('-n',NAMESPACE,'delete','pod',previous,'--grace-period=0','--force','--wait=false')
        wait('worker replacement',lambda:pod('worker')!=previous);rollout('deployment','worker')
        result=wait('lease takeover',lambda:completed(takeover),240)
        assert result['epoch']>=2,result
        passed('killed_worker_lease_takeover',run_id=takeover,epoch=result['epoch'])
        control(delay=0,failures=6)
        outage=seed('provider outage')
        wait('scheduled dependency wait',lambda:state(outage)['job']=='waiting')
        control(failures=0)
        wait('provider recovery',lambda:completed(outage),240)
        passed('llm_outage_wait_and_automatic_recovery',run_id=outage)
        scale('statefulset','postgres',0)
        wait('database stopped',lambda:not json.loads(kube('-n',NAMESPACE,'get','pods','-l','app=postgres','-o','json',capture_output=True,text=True).stdout)['items'])
        probe=inside("from fastapi.testclient import TestClient;from backend.app import app;c=TestClient(app);print(str(c.get('/api/live').status_code)+':'+str(c.get('/api/ready').status_code))")
        assert probe=='200:503',probe
        passed('database_outage_live_200_ready_503')
        scale('statefulset','postgres',1);rollout('statefulset','postgres')
        wait('database recovery',lambda:state(run)['model']=='passed')
        passed('database_pvc_recovery_preserves_results')
        inside("from backend.db import Session,ProviderState;from backend.providers import identity\nwith Session.begin() as s:\n p=s.get(ProviderState,identity('llm','http://llm-fixture:8080/v1'));p.window_requests=0;p.blocked_until=0")
        scale('deployment','worker',2);rollout('deployment','worker');control(delay=1)
        concurrent=[seed('parallel '+str(i)) for i in range(3)]
        for item in concurrent:
            final=wait('parallel jobs',lambda:completed(item));assert final['model']=='passed'
            inside("from backend.db import Session,Invocation;from sqlalchemy import select;from collections import Counter\nwith Session() as s:\n roles=Counter(s.scalars(select(Invocation.role).where(Invocation.run_id=="+repr(item)+",Invocation.status=='completed')));assert roles=={'screening':1,'critic':1,'verifier':1},roles")
        passed('two_workers_claim_distinct_jobs',runs=concurrent)
        REPORT['passed']=True
    finally:
        scale('statefulset','postgres',1);rollout('statefulset','postgres')
        scale('deployment','worker',1)
        configure(original)
        control(delay=0,failures=0)
        scale('deployment','llm-fixture',0)
        if automated is not None:kube('-n','argocd','patch','application','venture-lens','--type=merge','-p',json.dumps({'spec':{'syncPolicy':{'automated':automated}}}))
        (STATE/'fault-report.json').write_text(json.dumps(REPORT,indent=2)+'\n')
        print('Original lab provider configuration restored.',flush=True)

if __name__=='__main__':main()
