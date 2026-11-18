"""Static deployment invariants and obvious secret-file mistakes; no credentials are printed."""
from pathlib import Path
import re
import subprocess
import yaml
ROOT=Path(__file__).resolve().parent.parent

def main():
    paths=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    for name in paths:
        path=ROOT/name
        if not path.is_file():continue
        if Path(name).parts[0] in ('.platform','.tools'):raise RuntimeError('Private workstation state is tracked: '+name)
        if path.name in ('.env','.env.local','kubeconfig','secrets.json','argocd-admin.txt'):raise RuntimeError('Private runtime file is tracked: '+name)
        if path.suffix in ('.pem','.key','.p12','.pfx'):raise RuntimeError('Certificate/key file is tracked: '+name)
        if (b'-----BEGIN '+b'PRIVATE KEY-----') in path.read_bytes():raise RuntimeError('Private key material is tracked: '+name)
    for path in (ROOT/'deploy/base').glob('*.yaml'):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc.get('kind')=='Deployment':
                for c in doc['spec']['template']['spec']['containers']:
                    if not all(k in c for k in ['resources','livenessProbe','readinessProbe']):raise RuntimeError('Deployment lacks resource limits or probes: '+str(path))
    print('Repository and deployment invariants passed')

if __name__=='__main__':main()
