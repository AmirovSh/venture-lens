"""Fetch verified public GHCR OCI blobs through the host HTTPS configuration.

Used only for pinned KEDA images when Docker Desktop's PAC route is unavailable.
Anonymous public pull token only; no corporate credentials are used.
"""
import hashlib
import io
import json
import tarfile
import urllib.request
from lab import STATE,CLUSTER,run

def pull(image):
    assert image.startswith('ghcr.io/kedacore/')
    repo,tag=image[len('ghcr.io/'):].rsplit(':',1)
    token=json.load(urllib.request.urlopen('https://ghcr.io/token?service=ghcr.io&scope=repository:'+repo+':pull',timeout=30))['token']
    accept='application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json'
    def get(path):
        req=urllib.request.Request('https://ghcr.io/v2/'+repo+'/'+path,headers={'Authorization':'Bearer '+token,'Accept':accept})
        with urllib.request.urlopen(req,timeout=60) as response:return response.read()
    index=json.loads(get('manifests/'+tag))
    descriptor=next(x for x in index['manifests'] if x.get('platform',{}).get('architecture')=='amd64' and x.get('platform',{}).get('os')=='linux')
    manifest=get('manifests/'+descriptor['digest']);assert 'sha256:'+hashlib.sha256(manifest).hexdigest()==descriptor['digest']
    cache=STATE/'oci-blobs';cache.mkdir(exist_ok=True)
    all_blobs={descriptor['digest']:manifest}
    for blob in [json.loads(manifest)['config'],*json.loads(manifest)['layers']]:
        digest=blob['digest'];path=cache/digest.split(':')[1]
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest.split(':')[1]:
            data=get('blobs/'+digest)
            if 'sha256:'+hashlib.sha256(data).hexdigest()!=digest:raise RuntimeError('OCI blob checksum mismatch')
            path.write_bytes(data)
        all_blobs[digest]=path.read_bytes()
    descriptor={**descriptor,'annotations':{'io.containerd.image.name':image,'org.opencontainers.image.ref.name':image}}
    archive=STATE/'keda-image.tar'
    with tarfile.open(archive,'w') as tar:
        def add(name,data):
            info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
        add('oci-layout',b'{"imageLayoutVersion":"1.0.0"}')
        add('index.json',json.dumps({'schemaVersion':2,'manifests':[descriptor]}).encode())
        for digest,data in all_blobs.items():add('blobs/sha256/'+digest.split(':')[1],data)
    node='k3d-'+CLUSTER+'-server-0'
    run(['docker','cp',archive,node+':/tmp/venture-lens-keda.tar'])
    run(['docker','exec',node,'ctr','-n','k8s.io','images','import','--platform','linux/amd64','/tmp/venture-lens-keda.tar'],capture_output=True)
    run(['docker','exec',node,'rm','/tmp/venture-lens-keda.tar']);archive.unlink()
