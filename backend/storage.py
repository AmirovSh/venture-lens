"""Immutable source objects plus checksummed, disposable metadata caches."""
import hashlib
import json
import os
import time
import functools
import inspect
from pathlib import Path
import boto3
from botocore.exceptions import ClientError
from botocore.exceptions import BotoCoreError
from .execution import DependencyUnavailable
from .settings import DATA,setting

class IntegrityError(RuntimeError):pass

def resilient(function):
    if inspect.isgeneratorfunction(function):
        @functools.wraps(function)
        def generate(*args,**kwargs):
            try:yield from function(*args,**kwargs)
            except (BotoCoreError,ClientError) as error:raise DependencyUnavailable('Общее хранилище временно недоступно',30) from error
        return generate
    @functools.wraps(function)
    def invoke(*args,**kwargs):
        try:return function(*args,**kwargs)
        except (BotoCoreError,ClientError) as error:raise DependencyUnavailable('Общее хранилище временно недоступно',30) from error
    return invoke

def client():
    return boto3.client('s3',endpoint_url=setting('S3_ENDPOINT_URL') or None,region_name=setting('S3_REGION','us-east-1'),aws_access_key_id=setting('S3_ACCESS_KEY') or None,aws_secret_access_key=setting('S3_SECRET_KEY') or None)

def bucket():return setting('S3_BUCKET','venture-lens')
def enabled():return bool(setting('S3_ENDPOINT_URL'))

def atomic(path,content):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.'+str(time.time_ns())+'.tmp');temp.write_bytes(content);os.replace(temp,path)

@resilient
def put_source(text):
    content=text.encode();checksum=hashlib.sha256(content).hexdigest();key='sources/'+checksum+'.txt'
    if enabled():
        api=client()
        try:api.put_object(Bucket=bucket(),Key=key,Body=content,Metadata={'sha256':checksum},IfNoneMatch='*')
        except ClientError as error:
            if error.response['Error']['Code'] not in ('PreconditionFailed','412'):raise
            existing=api.get_object(Bucket=bucket(),Key=key)['Body'].read()
            if hashlib.sha256(existing).hexdigest()!=checksum:raise IntegrityError('Повреждён сохранённый источник')
        return checksum,'s3://'+bucket()+'/'+key
    path=DATA/key
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=checksum:atomic(path,content)
    return checksum,str(path)

@resilient
def get_source(checksum):
    if len(checksum)!=64 or any(c not in '0123456789abcdef' for c in checksum):raise ValueError('Invalid checksum')
    key='sources/'+checksum+'.txt'
    content=client().get_object(Bucket=bucket(),Key=key)['Body'].read() if enabled() else (DATA/key).read_bytes()
    if hashlib.sha256(content).hexdigest()!=checksum:raise IntegrityError('Контрольная сумма источника не совпала')
    return content.decode()

@resilient
def put_cache(kind,key,value):
    payload=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    envelope=json.dumps({'sha256':hashlib.sha256(payload).hexdigest(),'payload':payload.decode()},ensure_ascii=False).encode()
    target=f'cache/{kind}/{key}.json'
    if enabled():client().put_object(Bucket=bucket(),Key=target,Body=envelope,ContentType='application/json')
    else:atomic(DATA/target,envelope)

@resilient
def get_cache(kind,key):
    target=f'cache/{kind}/{key}.json'
    try:
        content=client().get_object(Bucket=bucket(),Key=target)['Body'].read() if enabled() else (DATA/target).read_bytes()
        envelope=json.loads(content);payload=envelope['payload'].encode()
        if hashlib.sha256(payload).hexdigest()!=envelope['sha256']:return None
        return json.loads(payload)
    except ClientError as error:
        if error.response['Error']['Code'] in ('NoSuchKey','404'):return None
        raise
    except (OSError,ValueError,KeyError):return None

@resilient
def cached_pages(limit=300):
    if enabled():
        objects=client().list_objects_v2(Bucket=bucket(),Prefix='cache/pages/',MaxKeys=limit).get('Contents',[])
        keys=[x['Key'].split('/')[-1][:-5] for x in objects]
    else:
        folder=DATA/'cache/pages';keys=[p.stem for p in list(folder.glob('*.json'))[:limit]]
    for key in keys:
        record=get_cache('pages',key)
        if record:yield record
