"""Private single-server GitOps and autoscaling bootstrap; no public ingress."""
import json
import subprocess
from pathlib import Path
import yaml
from deploy import kube,apply,service,ROOT,NS,run

def main():
    kube('apply','--server-side','-f',str(ROOT/'keda-install.yaml'))
    for name in ['keda-operator','keda-metrics-apiserver','keda-admission']:kube('-n','keda','rollout','status','deployment/'+name,'--timeout=240s')
    apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':'argocd'}})
    documents=list(yaml.safe_load_all((ROOT/'argocd-install.yaml').read_text()))
    for doc in documents:
        if doc and doc.get('kind')=='Deployment' and doc['metadata']['name'] in ['argocd-dex-server','argocd-notifications-controller','argocd-applicationset-controller']:doc['spec']['replicas']=0
    kube('apply','--server-side','-n','argocd','-f','-',input=yaml.safe_dump_all(documents).encode())
    kube('-n','argocd','patch','configmap','argocd-cmd-params-cm','--type=merge','-p',json.dumps({'data':{'server.insecure':'true','controller.status.processors':'5','controller.operation.processors':'2','reposerver.parallelism.limit':'1'}}))
    kube('-n','argocd','rollout','restart','deployment/argocd-server')
    for name in ['argocd-server','argocd-repo-server','argocd-redis']:kube('-n','argocd','rollout','status','deployment/'+name,'--timeout=240s')
    mirror=ROOT/'git/repo.git';mirror.parent.mkdir(exist_ok=True)
    if not mirror.exists():run(['git','clone','--bare',str(ROOT/'source'),str(mirror)])
    # Source bundles are checked out by SHA; a bare clone of detached HEAD has
    # no branch for Argo CD to resolve. Always create/update the desired ref.
    run(['git','-C',str(mirror),'fetch',str(ROOT/'source'),'+HEAD:refs/heads/master'])
    service('venture-lens-git',f'/usr/bin/git -c safe.directory=* daemon --reuseaddr --base-path={ROOT}/git --export-all --listen=127.0.0.1 --port=9418 {ROOT}/git','Read-only private GitOps source')
    repo='git://127.0.0.1:9418/repo.git'
    apply({'apiVersion':'argoproj.io/v1alpha1','kind':'AppProject','metadata':{'name':'venture-lens','namespace':'argocd'},'spec':{'sourceRepos':[repo],'destinations':[{'server':'https://kubernetes.default.svc','namespace':NS}],'clusterResourceWhitelist':[{'group':'','kind':'Namespace'}]}})
    apply({'apiVersion':'argoproj.io/v1alpha1','kind':'Application','metadata':{'name':'venture-lens','namespace':'argocd'},'spec':{'project':'venture-lens','source':{'repoURL':repo,'targetRevision':'master','path':'deploy/overlays/demo'},'destination':{'server':'https://kubernetes.default.svc','namespace':NS},'syncPolicy':{'automated':{'selfHeal':True,'prune':True},'syncOptions':['RespectIgnoreDifferences=true','PruneLast=true']},'ignoreDifferences':[{'group':'apps','kind':'Deployment','name':name,'jsonPointers':['/spec/replicas']} for name in ['api','worker']]}})
    service('venture-lens-argo-forward','/usr/local/bin/k3s kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n argocd port-forward --address 127.0.0.1 service/argocd-server 3090:80','Loopback-only Argo CD demo access')
    print('ADDONS_READY',flush=True)

if __name__=='__main__':main()
