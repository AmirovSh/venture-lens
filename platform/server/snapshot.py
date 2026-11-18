"""Manual closed-demo snapshot. Run on VPS; download directory off-host afterward.

PostgreSQL provides a consistent database snapshot; immutable source objects are
copied afterward and are never deleted by the application. This helper never
restores into or modifies the live database and never prints credentials.
"""
import hashlib
import json
import subprocess
import time
from pathlib import Path

NS = 'venture-lens-demo'
BASE = ['k3s', 'kubectl', '-n', NS]

# Export only immutable source objects, verifying their content-addressed names.
SOURCE_EXPORT = r'''
import boto3, hashlib, io, json, os, tarfile, sys, tempfile
from pathlib import Path
s3=boto3.client('s3',endpoint_url=os.environ['S3_ENDPOINT_URL'],aws_access_key_id=os.environ['S3_ACCESS_KEY'],aws_secret_access_key=os.environ['S3_SECRET_KEY'],region_name=os.environ.get('S3_REGION','us-east-1'))
bucket=os.environ['S3_BUCKET']; entries=[]
with tempfile.TemporaryDirectory() as directory:
    with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as archive:
        for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix='sources/'):
            for item in page.get('Contents',[]):
                path=Path(directory)/'source'
                s3.download_file(bucket,item['Key'],str(path))
                with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                if Path(item['Key']).stem!=digest:raise RuntimeError('Source checksum mismatch')
                archive.add(path,arcname=item['Key'])
                entries.append({'key':item['Key'],'sha256':digest,'bytes':path.stat().st_size})
        data=json.dumps({'version':1,'objects':entries}).encode()
        info=tarfile.TarInfo('manifest.json');info.size=len(data)
        archive.addfile(info,io.BytesIO(data))
'''


def main():
    root = Path('/opt/venture-lens/backups') / time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    root.mkdir(parents=True, mode=0o700)
    with (root / 'database.dump').open('wb') as output:
        subprocess.run([*BASE, 'exec', 'postgres-0', '--', 'sh', '-c',
                        'pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                       stdout=output, check=True)
    with (root / 'sources.tar.gz').open('wb') as output:
        subprocess.run([*BASE, 'exec', 'deployment/api', '--', 'python', '-c', SOURCE_EXPORT], stdout=output, check=True)
    entries = []
    for path in sorted(root.iterdir()):
        path.chmod(0o600)
        with path.open('rb') as stream:
            entries.append({'file': path.name, 'bytes': path.stat().st_size, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()})
    manifest = {'version': 1, 'created': time.time(), 'completed': True, 'files': entries}
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (root / 'manifest.json').chmod(0o600)
    print(json.dumps({'backup_directory': str(root), 'files': len(entries)}), flush=True)


if __name__ == '__main__':
    main()
