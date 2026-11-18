"""Export the full Git repository after scanning every historical blob for local secrets.

Only Git-tracked code/history is exported. Databases, credentials and local state
are excluded. Values are never printed, including on a failed scan.
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT)


def main():
    secrets=set()
    def collect(value,key=''):
        if isinstance(value,dict):
            for name,child in value.items():collect(child,str(name))
        elif isinstance(value,list):
            for child in value:collect(child,key)
        elif isinstance(value,str) and len(value)>=16 and re.search(r'password|secret|token|api.?key|auth_users',key,re.I):
            secrets.add(value.encode())
            if value.startswith('{'):
                try:collect(json.loads(value))
                except ValueError:pass
    local=dotenv_values(ROOT/'.env.local');collect(local)
    external=local.get('CORPORATE_ENV_FILE')
    if external and Path(external).is_file():collect(dotenv_values(external))
    for name in ['public-demo-credentials.json','public-demo-secret.json','registration-check-accounts.json']:
        path=ROOT/'.platform'/name
        if path.is_file():collect(json.loads(path.read_text(encoding='utf-8')))
    objects=git('rev-list','--objects','--all').decode().splitlines()
    for line in objects:
        path=line.partition(' ')[2]
        if path and (path.split('/')[0] in ('.platform','.tools') or Path(path).name in ('.env','.env.local','runtime.json','kubeconfig')):
            raise RuntimeError('Private path found in Git history: '+path)
    ids=list(dict.fromkeys(line.split(' ')[0] for line in objects))
    raw=subprocess.run(['git','cat-file','--batch'],cwd=ROOT,input=('\n'.join(ids)+'\n').encode(),capture_output=True,check=True).stdout
    offset=0;blobs=0
    for _ in ids:
        newline=raw.index(b'\n',offset);header=raw[offset:newline].split();size=int(header[-1]);content=raw[newline+1:newline+1+size];offset=newline+size+2
        if header[1]!=b'blob':continue
        blobs+=1
        if any(value in content for value in secrets):raise RuntimeError('A local credential matches a historical Git blob. Export blocked; value suppressed.')
        if (b'-----BEGIN '+b'PRIVATE KEY-----') in content or (b'-----BEGIN RSA '+b'PRIVATE KEY-----') in content:
            raise RuntimeError('Private-key material found in Git history. Export blocked.')
    output=ROOT/'deliverables';output.mkdir(exist_ok=True)
    bundle=output/'venture-lens.bundle'
    subprocess.run(['git','bundle','create',str(bundle),'--all'],cwd=ROOT,check=True)
    subprocess.run(['git','bundle','verify',str(bundle)],cwd=ROOT,check=True,capture_output=True)
    head=git('rev-parse','HEAD').decode().strip();destination=ROOT/'.platform'/('submission-check-'+head[:12])
    if not destination.exists():subprocess.run(['git','clone',str(bundle),str(destination)],check=True,capture_output=True)
    restored=subprocess.check_output(['git','rev-parse','HEAD'],cwd=destination).decode().strip()
    assert restored==head
    # Publication is a separately verified receipt, never inferred from an origin alone.
    proof_path=ROOT/'.platform/github-publication.json'
    try:proof=json.loads(proof_path.read_text())
    except (OSError,ValueError):proof={}
    repository=proof.get('url','')
    safe_repository=bool(re.fullmatch(r'https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+',repository))
    published=bool(safe_repository and proof.get('head')==head and proof.get('remote_matches_local') is True)
    report={'head':head,'commit_count':int(git('rev-list','--count','HEAD')),'historical_blobs_scanned':blobs,
            'known_credentials_checked':len(secrets),'history_secret_scan_passed':True,'bundle_verified':True,
            'clean_clone_verified':True,'bytes':bundle.stat().st_size,
            'sha256':hashlib.file_digest(bundle.open('rb'),'sha256').hexdigest(),
            'github_repository':repository if safe_repository else None,'github_published':published,
            'github_blocker':None if published else 'GitHub publication has not been verified for this commit'}
    (output/'repository-verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
