"""Prove Argo self-healing by changing a harmless environment marker."""
import json
from lab import kube,STATE,NAMESPACE
from fault_tests import wait,rollout

def app():return json.loads(kube('-n','argocd','get','application','venture-lens','-o','json',capture_output=True,text=True).stdout)
def synced():
    status=app().get('status',{})
    return status.get('sync',{}).get('status')=='Synced' and status.get('health',{}).get('status')=='Healthy'
def marker():
    doc=json.loads(kube('-n',NAMESPACE,'get','deployment','web','-o','json',capture_output=True,text=True).stdout)
    return next(item['value'] for item in doc['spec']['template']['spec']['containers'][0]['env'] if item['name']=='VENTURELENS_GITOPS_MARKER')

def main():
    wait('Argo sync',synced,240)
    expected=marker();assert expected=='declared'
    kube('-n',NAMESPACE,'set','env','deployment/web','VENTURELENS_GITOPS_MARKER=drift-test')
    assert marker()=='drift-test'
    kube('-n','argocd','annotate','application','venture-lens','argocd.argoproj.io/refresh=hard','--overwrite')
    wait('Argo self-heal',lambda:marker()==expected,180);rollout('deployment','web');wait('Argo healthy',synced)
    (STATE/'gitops-report.json').write_text(json.dumps({'passed':True,'sync':'Synced','health':'Healthy','self_heal':'manual Deployment drift restored from Git','revision':app()['status']['sync'].get('revision')},indent=2)+'\n')
    print('PASS Argo CD restored the declared deployment from Git',flush=True)

if __name__=='__main__':main()
