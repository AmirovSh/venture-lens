"""Run on the private demonstration VPS after images are built."""
import json
import subprocess
from pathlib import Path

ROOT=Path('/opt/venture-lens')
NS='venture-lens-demo'

def run(args,**kwargs):return subprocess.run(args,check=True,**kwargs)
def kube(*args,**kwargs):return run(['k3s','kubectl',*args],**kwargs)
def apply(doc):kube('apply','-f','-',input=json.dumps(doc).encode())
def service(name,command,description,user='root'):
    path=Path('/etc/systemd/system')/(name+'.service')
    path.write_text(f'''[Unit]
Description={description}
After=k3s.service network-online.target
Requires=k3s.service
[Service]
User={user}
ExecStart={command}
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectHome=read-only
ProtectSystem=full
[Install]
WantedBy=multi-user.target
''')
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now',name])

def main():
    # Only pod/service networks can reach host-side private adapters.
    for network in ['10.42.0.0/16','10.43.0.0/16']:run(['ufw','allow','from',network,'to','any'])
    apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':NS}})
    values=json.loads((ROOT/'runtime.json').read_text())
    apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':'venture-lens-runtime','namespace':NS},'type':'Opaque','stringData':values})
    service('venture-lens-llm-relay',f'/usr/bin/python3 {ROOT}/loopback_proxy.py','Private temporary demo LLM relay','nobody')
    kube('apply','-k',str(ROOT/'source/deploy/overlays/demo'))
    for name in ['postgres','source-store']:kube('-n',NS,'rollout','status','statefulset/'+name,'--timeout=240s')
    kube('-n',NS,'wait','--for=condition=complete','job/bucket-init','--timeout=240s')
    for name in ['api','worker','web']:kube('-n',NS,'rollout','status','deployment/'+name,'--timeout=240s')
    service('venture-lens-web-forward',f'/usr/local/bin/k3s kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n {NS} port-forward --address 127.0.0.1 service/web 3210:3100','Loopback-only Venture Lens demo access')
    print('DEMO_DEPLOY_COMPLETE',flush=True)

if __name__=='__main__':main()
