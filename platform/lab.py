"""Owns only venture-lens-lab resources; never changes the user's default context."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import hashlib
import secrets
import time
import urllib.request
import ssl
import yaml

ROOT=Path(__file__).resolve().parent.parent
STATE=ROOT/'.platform'
CLUSTER='venture-lens-lab'
NAMESPACE='venture-lens-lab'
LOCK=json.loads((ROOT/'platform/toolchain.json').read_text())
SUFFIX='.exe' if os.name=='nt' else ''
K3D=str(ROOT/'.tools'/('k3d'+SUFFIX))
KUBECTL=str(ROOT/'.tools'/('kubectl'+SUFFIX))
CONFIG=str(STATE/'kubeconfig')

def run(args,**kwargs):
    env={**os.environ,'NO_PROXY':os.environ.get('NO_PROXY','')+',127.0.0.1,localhost,.svc,.cluster.local'}
    return subprocess.run([str(a) for a in args],cwd=ROOT,env=env,check=True,**kwargs)

def kube(*args,**kwargs):
    return run([KUBECTL,'--kubeconfig',CONFIG,'--context','k3d-'+CLUSTER,*args],**kwargs)

def init():
    STATE.mkdir(exist_ok=True)
    listed=json.loads(run([K3D,'cluster','list','-o','json'],capture_output=True,text=True).stdout)
    if not any(x['name']==CLUSTER for x in listed):
        run([K3D,'cluster','create',CLUSTER,'--image',LOCK['k3s'],'--servers','1','--agents','0','--servers-memory','1800m','--api-port','127.0.0.1:6551','--port','127.0.0.1:3210:30310@server:0','--port','127.0.0.1:3090:30390@server:0','--k3s-arg','--disable=traefik@server:*','--k3s-arg','--disable=servicelb@server:*','--kubeconfig-update-default=false','--kubeconfig-switch-context=false','--timeout','180s'])
    content=run([K3D,'kubeconfig','get',CLUSTER],capture_output=True).stdout
    Path(CONFIG).write_bytes(content)
    kube('get','nodes')
    print('Isolated kubeconfig saved; default context unchanged.')

def fingerprint():
    digest=hashlib.sha256()
    files=[]
    for folder in ['backend','config','migrations','app']:
        files += [p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    files += [ROOT/p for p in ['Dockerfile.api','Dockerfile.web','requirements.lock.txt','alembic.ini','package.json','package-lock.json','next.config.ts','tsconfig.json','next-env.d.ts']]
    for path in sorted(files):digest.update(str(path.relative_to(ROOT)).encode());digest.update(path.read_bytes())
    return digest.hexdigest()[:16]

def build():
    version=fingerprint();tag='lab-'+version
    mirror=os.name=='nt'
    for name,file,argument,value in [('api','Dockerfile.api','PYTHON_IMAGE','public.ecr.aws/docker/library/python:3.11-slim'),('web','Dockerfile.web','NODE_IMAGE','public.ecr.aws/docker/library/node:22-alpine')]:
        args=['docker','build','-f',file,'-t',f'venture-lens-{name}:{tag}']
        if mirror:args+=['--build-arg',argument+'='+value]
        run([*args,'.'])
    (STATE/'release.json').write_text(json.dumps({'tag':tag,'source_fingerprint':version},indent=2)+'\n')
    overlay=ROOT/'deploy/overlays/local/kustomization.yaml'
    data=yaml.safe_load(overlay.read_text())
    for image in data['images']:image['newTag']=tag
    overlay.write_text(yaml.safe_dump(data,sort_keys=False))
    print('Built immutable local release:',tag)

def private_values():
    path=STATE/'secrets.json'
    if path.is_file():return json.loads(path.read_text())
    password=secrets.token_hex(24);bridge=secrets.token_hex(32)
    values={'POSTGRES_PASSWORD':password,'DATABASE_URL':f'postgresql+psycopg://venturelens:{password}@postgres:5432/venturelens','KEDA_DATABASE_URL':f'postgresql://venturelens:{password}@postgres:5432/venturelens','LLM_BASE_URL':'http://host.docker.internal:8112/v1','LLM_API_KEY':bridge,'SEARXNG_URL':'http://host.docker.internal:8088','S3_ENDPOINT_URL':'http://objectstore:9000','S3_ACCESS_KEY':'venturelens-local','S3_SECRET_KEY':secrets.token_hex(24)}
    if os.environ.get('LAB_FIXTURE_PROVIDER')=='1':values.update(LLM_BASE_URL='http://llm-fixture:8080/v1',LLM_API_KEY='synthetic-test-only',SEARXNG_URL='')
    path.write_text(json.dumps(values,indent=2))
    return values

def start_bridge():
    if os.name!='nt' or os.environ.get('LAB_FIXTURE_PROVIDER')=='1':return
    values=private_values()
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8112/live',timeout=2) as response:
            if json.load(response).get('status')=='alive':return
    except OSError:pass
    flags=subprocess.CREATE_NO_WINDOW
    process=subprocess.Popen([str(ROOT/'.venv/Scripts/python.exe'),'-m','uvicorn','backend.mtls_bridge:app','--host','127.0.0.1','--port','8112'],cwd=ROOT,env={**os.environ,'LOCAL_LLM_BRIDGE_TOKEN':values['LLM_API_KEY']},stdout=(STATE/'bridge.stdout.log').open('ab'),stderr=(STATE/'bridge.stderr.log').open('ab'),creationflags=flags)
    (STATE/'bridge.pid').write_text(str(process.pid))
    print('Local mTLS bridge started; private key remains in Windows.')

def import_images(images):
    # Recent K3s no longer exposes `k3s ctr`; use containerd's explicit binary.
    archive=STATE/'images.tar'
    run(['docker','save','-o',archive,*images])
    node='k3d-'+CLUSTER+'-server-0'
    run(['docker','cp',archive,node+':/tmp/venture-lens-images.tar'])
    run(['docker','exec',node,'ctr','-n','k8s.io','images','import','--platform','linux/amd64','/tmp/venture-lens-images.tar'])
    run(['docker','exec',node,'rm','/tmp/venture-lens-images.tar'])
    archive.unlink()

def deploy():
    start_bridge()
    values=private_values();release=json.loads((STATE/'release.json').read_text());tag=release['tag']
    for image in ['postgres:17-alpine','quay.io/minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e']:
        if subprocess.run(['docker','image','inspect',image],capture_output=True).returncode:
            if image.startswith('minio') and subprocess.run(['docker','image','inspect','minio/minio:latest'],capture_output=True).returncode==0:run(['docker','tag','minio/minio:latest',image])
            else:run(['docker','pull',image])
    import_images([f'venture-lens-api:{tag}',f'venture-lens-web:{tag}','postgres:17-alpine','quay.io/minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e'])
    ns={'apiVersion':'v1','kind':'Namespace','metadata':{'name':NAMESPACE,'labels':{'venturelens.dev/environment':'local-disposable'}}}
    kube('apply','-f','-',input=json.dumps(ns).encode())
    secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'venture-lens-runtime','namespace':NAMESPACE},'type':'Opaque','stringData':values}
    kube('apply','-f','-',input=json.dumps(secret).encode())
    if os.environ.get('LAB_FIXTURE_PROVIDER')=='1':
        from fixtures import resources
        for item in resources(NAMESPACE,'venture-lens-api:'+tag):kube('apply','-f','-',input=json.dumps(item).encode())
    kube('apply','-k','deploy/overlays/local')
    for name in ['postgres','objectstore']:kube('-n',NAMESPACE,'rollout','status','statefulset/'+name,'--timeout=180s')
    kube('-n',NAMESPACE,'wait','--for=condition=complete','job/bucket-init','--timeout=180s')
    for name in ['api','worker','web']:kube('-n',NAMESPACE,'rollout','status','deployment/'+name,'--timeout=240s')
    print('Local Kubernetes application: http://127.0.0.1:3210')

def gitops():
    manifest=STATE/'argocd-install.yaml'
    if not manifest.is_file():
        url=f'https://raw.githubusercontent.com/argoproj/argo-cd/{LOCK["argocd"]}/manifests/install.yaml'
        with urllib.request.urlopen(url,context=ssl.create_default_context(),timeout=60) as response:manifest.write_bytes(response.read())
    docs=list(yaml.safe_load_all(manifest.read_text()))
    images=sorted({c['image'] for d in docs if d and d.get('kind') in ['Deployment','StatefulSet'] and d['metadata']['name'] not in ['argocd-dex-server','argocd-notifications-controller','argocd-applicationset-controller'] for c in d['spec']['template']['spec'].get('containers',[])})
    for image in images:
        run(['docker','pull',image]);import_images([image])
    kube('create','namespace','argocd','--dry-run=client','-o','json',capture_output=True)
    kube('apply','-f','-',input=json.dumps({'apiVersion':'v1','kind':'Namespace','metadata':{'name':'argocd'}}).encode())
    kube('apply','--server-side','-n','argocd','-f',str(manifest))
    for name in ['argocd-dex-server','argocd-notifications-controller','argocd-applicationset-controller']:kube('-n','argocd','scale','deployment/'+name,'--replicas=0')
    kube('-n','argocd','patch','configmap','argocd-cmd-params-cm','--type=merge','-p',json.dumps({'data':{'server.insecure':'true','controller.status.processors':'5','controller.operation.processors':'2','reposerver.parallelism.limit':'1'}}))
    kube('-n','argocd','patch','service','argocd-server','--type=merge','-p',json.dumps({'spec':{'type':'NodePort','ports':[{'name':'http','port':80,'targetPort':8080,'nodePort':30390}]}}))
    for name in ['argocd-server','argocd-repo-server','argocd-redis']:kube('-n','argocd','rollout','status','deployment/'+name,'--timeout=240s')
    mirror=STATE/'git/repo.git';mirror.parent.mkdir(parents=True,exist_ok=True)
    if not mirror.exists():run(['git','init','--bare',str(mirror)])
    run(['git','-C',str(mirror),'fetch',str(ROOT),'HEAD:refs/heads/lab']);run(['git','-C',str(mirror),'symbolic-ref','HEAD','refs/heads/lab']);run(['git','-C',str(mirror),'update-server-info'])
    if subprocess.run(['docker','inspect','venture-lens-gitops'],capture_output=True).returncode:
        run(['docker','run','-d','--name','venture-lens-gitops','--network','k3d-'+CLUSTER,'--read-only','--user','10001','--volume',str(mirror.parent)+':/srv:ro',f'venture-lens-api:{json.loads((STATE/"release.json").read_text())["tag"]}','python','-m','http.server','8080','--directory','/srv'])
    ip=run(['docker','inspect','venture-lens-gitops','--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'],capture_output=True,text=True).stdout.strip()
    repo='http://'+ip+':8080/repo.git'
    project={'apiVersion':'argoproj.io/v1alpha1','kind':'AppProject','metadata':{'name':'venture-lens','namespace':'argocd'},'spec':{'sourceRepos':[repo],'destinations':[{'server':'https://kubernetes.default.svc','namespace':NAMESPACE}],'clusterResourceWhitelist':[{'group':'','kind':'Namespace'}]}}
    application={'apiVersion':'argoproj.io/v1alpha1','kind':'Application','metadata':{'name':'venture-lens','namespace':'argocd'},'spec':{'project':'venture-lens','source':{'repoURL':repo,'targetRevision':'lab','path':'deploy/overlays/local'},'destination':{'server':'https://kubernetes.default.svc','namespace':NAMESPACE},'syncPolicy':{'automated':{'selfHeal':True,'prune':True},'syncOptions':['RespectIgnoreDifferences=true','PruneLast=true']},'ignoreDifferences':[{'group':'apps','kind':'Deployment','name':name,'jsonPointers':['/spec/replicas']} for name in ['api','worker']]}}
    for item in [project,application]:kube('apply','-f','-',input=json.dumps(item).encode())
    secret=json.loads(kube('-n','argocd','get','secret','argocd-initial-admin-secret','-o','json',capture_output=True,text=True).stdout)
    import base64
    (STATE/'argocd-admin.txt').write_text('Username: admin\nPassword: '+base64.b64decode(secret['data']['password']).decode()+'\n')
    print('Argo CD: http://127.0.0.1:3090 (credentials saved privately in .platform/argocd-admin.txt)')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['init','build','deploy','gitops','status']);args=parser.parse_args()
    if args.action=='init':init()
    elif args.action=='build':build()
    elif args.action=='deploy':deploy()
    elif args.action=='gitops':gitops()
    else:kube('get','pods','-A')

if __name__=='__main__':main()
