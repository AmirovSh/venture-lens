"""Write database + immutable source objects to a separate backup S3 account.

The completion manifest is written last. Object lifecycle/retention is configured
on the backup bucket. No delete operations or restore into the live DB occur here.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import boto3

def client(prefix):
    return boto3.client('s3',endpoint_url=os.environ.get(prefix+'ENDPOINT_URL') or None,region_name=os.environ.get(prefix+'REGION','us-east-1'),aws_access_key_id=os.environ[prefix+'ACCESS_KEY'],aws_secret_access_key=os.environ[prefix+'SECRET_KEY'])

def main():
    source=client('S3_');target=client('BACKUP_S3_')
    bucket=os.environ['BACKUP_S3_BUCKET'];source_bucket=os.environ['S3_BUCKET']
    target.head_bucket(Bucket=bucket)
    prefix='snapshots/'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]+'/'
    entries=[]
    with tempfile.TemporaryDirectory() as directory:
        dump=Path(directory)/'database.dump'
        url=os.environ['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://')
        # libpq receives credentials in the environment, never process arguments.
        subprocess.run(['pg_dump','--format=custom','--no-owner','--no-acl','--file',str(dump)],env={**os.environ,'PGDATABASE':url},check=True)
        checksum=hashlib.file_digest(dump.open('rb'),'sha256').hexdigest()
        target.upload_file(str(dump),bucket,prefix+'database.dump',ExtraArgs={'Metadata':{'sha256':checksum}})
        entries.append({'key':'database.dump','sha256':checksum,'bytes':dump.stat().st_size})
        for page in source.get_paginator('list_objects_v2').paginate(Bucket=source_bucket,Prefix='sources/'):
            for item in page.get('Contents',[]):
                path=Path(directory)/'source';digest=hashlib.sha256()
                with path.open('wb') as file:
                    stream=source.get_object(Bucket=source_bucket,Key=item['Key'])['Body']
                    for part in stream.iter_chunks(1024*1024):digest.update(part);file.write(part)
                checksum=digest.hexdigest()
                if Path(item['Key']).stem!=checksum:raise RuntimeError('Source checksum validation failed')
                target.upload_file(str(path),bucket,prefix+item['Key'],ExtraArgs={'Metadata':{'sha256':checksum}})
                entries.append({'key':item['Key'],'sha256':checksum,'bytes':path.stat().st_size})
        manifest={'version':1,'created':time.time(),'prefix':prefix,'objects':entries,'completed':True}
        target.put_object(Bucket=bucket,Key=prefix+'manifest.json',Body=json.dumps(manifest).encode(),ContentType='application/json')
    print(json.dumps({'backup_completed':True,'prefix':prefix,'objects':len(entries)}),flush=True)

if __name__=='__main__':main()
