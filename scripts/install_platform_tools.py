"""Install pinned, checksum-verified tools into this checkout only."""
import hashlib
import json
import os
from pathlib import Path
import platform
import ssl
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
TOOLS=ROOT/'.tools'
LOCK=ROOT/'platform/toolchain.json'

def get(url):
    request=urllib.request.Request(url,headers={'User-Agent':'VentureLens-platform-bootstrap'})
    with urllib.request.urlopen(request,context=ssl.create_default_context(),timeout=90) as response:return response.read()

def install(name,url,checksum):
    target=TOOLS/name
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==checksum:return
    content=get(url)
    if hashlib.sha256(content).hexdigest()!=checksum:raise RuntimeError(f'Checksum mismatch: {name}')
    temporary=target.with_suffix('.download');temporary.write_bytes(content);temporary.replace(target)
    if os.name!='nt':target.chmod(0o755)
    print(f'Installed {name} (SHA256 verified)')

def main():
    TOOLS.mkdir(exist_ok=True)
    lock=json.loads(LOCK.read_text(encoding='utf-8'))
    system='windows' if os.name=='nt' else 'linux'
    arch='arm64' if platform.machine().lower() in ('aarch64','arm64') else 'amd64'
    suffix='.exe' if os.name=='nt' else ''
    version=lock['k3d']
    filename=f'k3d-{system}-{arch}'+suffix
    base=f'https://github.com/k3d-io/k3d/releases/download/{version}'
    checksums=get(base+'/checksums.txt').decode()
    checksum=next(line.split()[0] for line in checksums.splitlines() if line.split()[-1].replace('\\','/').split('/')[-1].lstrip('*')==filename)
    install('k3d'+suffix,base+'/'+filename,checksum)
    version=lock['kubectl']
    url=f'https://dl.k8s.io/release/{version}/bin/{system}/{arch}/kubectl'+suffix
    install('kubectl'+suffix,url,get(url+'.sha256').decode().strip())
    print('Tools installed inside .tools; global kubeconfig is unchanged.')

if __name__=='__main__':main()
