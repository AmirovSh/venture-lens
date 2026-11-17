"""Change only image digests in the production overlay; deployment is controlled by Git review."""
import argparse
from pathlib import Path
import re
import yaml
ROOT=Path(__file__).resolve().parent.parent

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--owner',required=True);parser.add_argument('--api-digest',required=True);parser.add_argument('--web-digest',required=True);parser.add_argument('--backup-digest',required=True);args=parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+',args.owner):raise ValueError('Invalid registry owner')
    for value in [args.api_digest,args.web_digest,args.backup_digest]:
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',value):raise ValueError('An immutable SHA256 image digest is required')
    path=ROOT/'deploy/overlays/production/kustomization.yaml';data=yaml.safe_load(path.read_text())
    for image in data['images']:
        image['newName']='ghcr.io/'+args.owner.lower()+'/'+image['name']
        image.pop('newTag',None);image['digest']={'venture-lens-api':args.api_digest,'venture-lens-web':args.web_digest,'venture-lens-backup':args.backup_digest}[image['name']]
    path.write_text(yaml.safe_dump(data,sort_keys=False));print('Production image digests updated; merge the reviewed Git change to deploy.')

if __name__=='__main__':main()
