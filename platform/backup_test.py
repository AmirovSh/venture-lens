"""Exercise the real backup image and pg_restore into a new disposable database."""
import base64
import hashlib
import json
import uuid
from lab import kube,STATE,NAMESPACE,import_images
from fault_tests import inside

def main():
    ns=json.loads(kube('get','namespace',NAMESPACE,'-o','json',capture_output=True,text=True).stdout)
    assert ns['metadata']['labels'].get('venturelens.dev/environment')=='local-disposable'
    image='venture-lens-backup:lab-v1';import_images([image])
    inside("from backend.storage import client;from botocore.exceptions import ClientError\ns=client()\ntry:s.head_bucket(Bucket='venture-lens-backups-local')\nexcept ClientError:s.create_bucket(Bucket='venture-lens-backups-local')")
    job='backup-test-'+uuid.uuid4().hex[:8]
    env=[{'name':'BACKUP_S3_BUCKET','value':'venture-lens-backups-local'}]
    for suffix in ['ENDPOINT_URL','ACCESS_KEY','SECRET_KEY']:
        env.append({'name':'BACKUP_S3_'+suffix,'valueFrom':{'secretKeyRef':{'name':'venture-lens-runtime','key':'S3_'+suffix}}})
    doc={'apiVersion':'batch/v1','kind':'Job','metadata':{'name':job,'namespace':NAMESPACE},'spec':{'backoffLimit':0,'activeDeadlineSeconds':180,'template':{'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,'containers':[{'name':'backup','image':image,'env':env,'envFrom':[{'configMapRef':{'name':'venture-lens-config'}},{'secretRef':{'name':'venture-lens-runtime'}}],'resources':{'requests':{'cpu':'25m','memory':'64Mi'},'limits':{'cpu':'1','memory':'256Mi'}}}]}}}}
    kube('apply','-f','-',input=json.dumps(doc).encode())
    kube('-n',NAMESPACE,'wait','--for=condition=complete','job/'+job,'--timeout=180s')
    result=json.loads(kube('-n',NAMESPACE,'logs','job/'+job,capture_output=True,text=True).stdout.strip().splitlines()[-1])
    prefix=result['prefix']
    verify="""from backend.storage import client
import json,hashlib,base64
s=client();bucket='venture-lens-backups-local';prefix=PREFIX
m=json.loads(s.get_object(Bucket=bucket,Key=prefix+'manifest.json')['Body'].read());assert m['completed']
for item in m['objects']:
 data=s.get_object(Bucket=bucket,Key=prefix+item['key'])['Body'].read()
 assert hashlib.sha256(data).hexdigest()==item['sha256']
 if item['key']=='database.dump':print(base64.b64encode(data).decode())
""".replace('PREFIX',repr(prefix))
    dump=base64.b64decode(inside(verify));(STATE/'restore-check.dump').write_bytes(dump)
    name='restore_'+uuid.uuid4().hex[:12]
    kube('-n',NAMESPACE,'exec','statefulset/postgres','--','createdb','-U','venturelens',name)
    kube('-n',NAMESPACE,'exec','-i','statefulset/postgres','--','pg_restore','--exit-on-error','--no-owner','--no-privileges','-U','venturelens','-d',name,input=dump,capture_output=True)
    rows=kube('-n',NAMESPACE,'exec','statefulset/postgres','--','psql','-U','venturelens','-d',name,'-Atc',"SELECT count(*) FROM models WHERE decision='shortlist'",capture_output=True,text=True).stdout.strip()
    assert int(rows)>0
    report={'passed':True,'backup_prefix':prefix,'restored_database':name,'shortlisted_models_restored':int(rows),'objects':result['objects'],'dump_sha256':hashlib.sha256(dump).hexdigest(),'backup_target':'local S3 only; production must use a separate failure domain'}
    (STATE/'backup-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
