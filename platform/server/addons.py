"""Single-server GitOps from public GitHub; administrative UI stays private."""
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
    kube('apply','-f',str(ROOT/'source/deploy/argocd/demo.yaml'))
    service('venture-lens-argo-forward','/usr/local/bin/k3s kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n argocd port-forward --address 127.0.0.1 service/argocd-server 3090:80','Loopback-only Argo CD demo access')
    print('ADDONS_READY',flush=True)

if __name__=='__main__':main()
