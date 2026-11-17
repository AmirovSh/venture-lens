"""Install pinned KEDA in the lab and test scaling with a disposable queue probe."""
import json
import subprocess
import ssl
import urllib.request
import yaml
from lab import STATE,LOCK,CLUSTER,NAMESPACE,kube,run
from fault_tests import inside,wait,scale,rollout

def main():
    ns=json.loads(kube('get','namespace',NAMESPACE,'-o','json',capture_output=True,text=True).stdout)
    assert ns['metadata']['labels'].get('venturelens.dev/environment')=='local-disposable'
    version=LOCK['keda'].lstrip('v');manifest=STATE/('keda-'+version+'.yaml')
    if not manifest.exists():
        with urllib.request.urlopen(f'https://github.com/kedacore/keda/releases/download/v{version}/keda-{version}.yaml',context=ssl.create_default_context(),timeout=60) as response:manifest.write_bytes(response.read())
    docs=list(yaml.safe_load_all(manifest.read_text()))
    images={c['image'] for d in docs if d and d.get('kind')=='Deployment' for c in d['spec']['template']['spec']['containers']}
    for image in sorted(images):
        # Direct containerd pull avoids Docker Desktop's unrelated system PAC proxy.
        try:run(['docker','exec','k3d-'+CLUSTER+'-server-0','ctr','-n','k8s.io','images','pull','--platform','linux/amd64',image],capture_output=True)
        except subprocess.CalledProcessError:
            from public_oci import pull
            pull(image)
        print('Pulled KEDA component',image,flush=True)
    kube('apply','--server-side','-f',manifest)
    for d in docs:
        if d and d.get('kind')=='Deployment':kube('-n','keda','rollout','status','deployment/'+d['metadata']['name'],'--timeout=180s')
    probe={'apiVersion':'keda.sh/v1alpha1','kind':'ScaledObject','metadata':{'name':'worker-scaling-probe','namespace':NAMESPACE},'spec':{'scaleTargetRef':{'name':'worker'},'minReplicaCount':1,'maxReplicaCount':2,'pollingInterval':5,'cooldownPeriod':15,'advanced':{'restoreToOriginalReplicaCount':True,'horizontalPodAutoscalerConfig':{'behavior':{'scaleDown':{'stabilizationWindowSeconds':10}}}},'triggers':[{'type':'postgresql','metadata':{'connectionFromEnv':'KEDA_DATABASE_URL','query':'SELECT COUNT(*) FROM scaling_probe','targetQueryValue':'4'}}]}}
    try:
        inside("from backend.db import engine;from sqlalchemy import text\nwith engine.begin() as c:c.execute(text('CREATE TABLE IF NOT EXISTS scaling_probe (id integer)'));c.execute(text('TRUNCATE scaling_probe'));c.execute(text('INSERT INTO scaling_probe SELECT generate_series(1,8)'))")
        kube('apply','-f','-',input=json.dumps(probe).encode())
        kube('-n',NAMESPACE,'wait','--for=condition=Ready','scaledobject/worker-scaling-probe','--timeout=120s')
        def replicas():return json.loads(kube('-n',NAMESPACE,'get','deployment','worker','-o','json',capture_output=True,text=True).stdout)['spec']['replicas']
        wait('KEDA scale out',lambda:replicas()==2,180);rollout('deployment','worker')
        inside("from backend.db import engine;from sqlalchemy import text\nwith engine.begin() as c:c.execute(text('TRUNCATE scaling_probe'))")
        wait('KEDA scale in',lambda:replicas()==1,180)
        (STATE/'scaling-report.json').write_text(json.dumps({'passed':True,'keda':LOCK['keda'],'replicas_observed':[1,2,1],'probe':'isolated PostgreSQL queue metric; no paid LLM calls'},indent=2)+'\n')
        print('PASS KEDA PostgreSQL metric: 1 -> 2 -> 1 workers',flush=True)
    finally:
        kube('-n',NAMESPACE,'delete','scaledobject','worker-scaling-probe','--ignore-not-found')
        scale('deployment','worker',1)
        inside("from backend.db import engine;from sqlalchemy import text\nwith engine.begin() as c:c.execute(text('DROP TABLE IF EXISTS scaling_probe'))")

if __name__=='__main__':main()
