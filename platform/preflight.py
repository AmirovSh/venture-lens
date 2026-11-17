"""Prevent accidental production use of unfinished deployment configuration."""
import argparse
from pathlib import Path
import re
import sys
ROOT=Path(__file__).resolve().parent.parent

def validate(text):
    if 'REPLACE_' in text:raise ValueError('Production placeholders must be configured explicitly')
    for line in text.splitlines():
        if re.match(r'\s*image:',line) and '@sha256:' not in line:raise ValueError('Production application images must be pinned by digest')
    if 'kind: Secret' in text:raise ValueError('Plaintext runtime secrets must not be stored in rendered GitOps configuration')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('manifest');args=parser.parse_args()
    validate(Path(args.manifest).read_text());print('Production manifest preflight passed')
