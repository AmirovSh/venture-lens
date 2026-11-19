"""Prevent accidental production use of unfinished deployment configuration."""
import argparse
from pathlib import Path
import re
import yaml

def validate(text, *, template=False):
    if not template and 'REPLACE_' in text:
        raise ValueError('Production placeholders must be configured explicitly')
    documents=[item for item in yaml.safe_load_all(text) if isinstance(item,dict)]
    if any(item.get('kind')=='Secret' for item in documents):
        raise ValueError('Plaintext runtime secrets must not be stored in rendered GitOps configuration')
    api=next((item for item in documents if item.get('kind')=='Deployment' and item.get('metadata',{}).get('name')=='api'),None)
    containers=api['spec']['template']['spec'].get('containers',[]) if api else []
    container=next((item for item in containers if item['name']=='api'),{})
    env={item['name']:item for item in container.get('env',[])}
    secret=env.get('AUTH_CONTEXT_SECRET',{}).get('valueFrom',{}).get('secretKeyRef',{})
    if (env.get('AUTH_CONTEXT_REQUIRED',{}).get('value')!='true' or 'value' in env.get('AUTH_CONTEXT_SECRET',{})
            or not secret.get('name') or not secret.get('key') or secret.get('optional',False)):
        raise ValueError('Production API requires signed identity and a non-optional signing Secret')
    for line in text.splitlines():
        if not re.match(r'\s*image:',line):continue
        image=line.split(':',1)[1].strip()
        if template and re.fullmatch(r'ghcr\.io/REPLACE_OWNER/venture-lens-(api|web|backup):REPLACE_RELEASE_SHA',image):continue
        if not re.search(r'@sha256:[a-f0-9]{64}$',image):
            raise ValueError('Production application images must be pinned by digest')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('manifest')
    parser.add_argument('--template',action='store_true',help='Check auth and manifests while release placeholders are present')
    args=parser.parse_args()
    validate(Path(args.manifest).read_text(encoding='utf-8'),template=args.template)
    print('Production manifest preflight passed')
