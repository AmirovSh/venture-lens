"""Bounded live regression for compact views and complete streamed exports.

Reads only the owner's shared demo research; never queues jobs or saves exports.
This is a regression check for OOM, not a capacity benchmark.
"""
import json
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from verify_decision_workspace import Client
from ssh_client import ROOT,connect,execute


def memory():
    code=r'''
import json,subprocess
base=['k3s','kubectl','-n','venture-lens-demo']
pods=json.loads(subprocess.check_output(base+['get','pods','-l','app=api','-o','json']))
report=[]
for pod in pods['items']:
 status=next(x for x in pod['status'].get('containerStatuses',[]) if x['name']=='api')
 if not status.get('ready'):continue
 code="from pathlib import Path;import json;p=Path('/sys/fs/cgroup');print(json.dumps({k:(p/k).read_text().strip() for k in ('memory.current','memory.peak','memory.max','memory.events')}))"
 raw=subprocess.check_output(base+['exec',pod['metadata']['name'],'-c','api','--','python','-c',code],stderr=subprocess.DEVNULL)
 value=json.loads(raw);events=dict(line.split() for line in value.pop('memory.events').splitlines())
 report.append({'pod':pod['metadata']['name'],'restarts':status['restartCount'],'oom_kills':int(events.get('oom_kill',0)),**{key:int(item) if item.isdigit() else item for key,item in value.items()}})
print(json.dumps(report))
'''
    with connect() as c:return json.loads(execute(c,'python3 -c '+shlex.quote(code),timeout=50))


def main():
    owner=json.loads((ROOT/'.platform/public-demo-credentials.json').read_text())['owner']
    client=Client({key:owner[key] for key in ('username','password')});client.login()
    try:
        runs=client.json('/api/runs');assert len(runs)>=2
        before=memory()
        requests=[('/api/runs/'+runs[i%2]['id'],False) for i in range(6)]+[('/api/runs',False)]*2+[
            ('/api/runs/'+runs[0]['id']+'/export',True),('/api/runs/'+runs[1]['id']+'/export',True)]
        def probe(item):
            path,export=item;started=time.monotonic();status,headers,raw=client.request(path)
            assert status==200 or (export and status==429 and headers.get('Retry-After')=='5'),(path,status)
            if status==200:
                body=json.loads(raw)
                if isinstance(body,dict) and 'models' in body:
                    assert len(body['models'])==10
                    if not export:
                        assert all('analysis_history' not in m['data'] and 'verification_history' not in m['data'] for m in body['models'])
                        assert all(len(source.get('text',''))<=300 for m in body['models'] for source in m['data'].get('sources',[]))
                    else:
                        assert any(len(source.get('text',''))>300 for m in body['models'] for source in m['data'].get('sources',[]))
            return {'kind':'export' if export else 'screen','status':status,'bytes':len(raw),'seconds':round(time.monotonic()-started,2)}
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(probe,requests))
        after=memory();old={item['pod']:item for item in before}
        assert after and all(item['oom_kills']==0 and item['restarts']==old.get(item['pod'],{'restarts':0})['restarts'] for item in after)
        assert any(item['kind']=='export' and item['status']==200 for item in results)
        report={'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'bounded concurrent read/export regression, not a capacity benchmark',
            'requests':results,'memory_before':before,'memory_after':after,'oom_or_restart_during_check':False,
            'compact_views_preserve_complete_exports':True,'new_research_started':False}
        (ROOT/'.platform/decision-memory-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))
    finally:client.logout()


if __name__=='__main__':main()
