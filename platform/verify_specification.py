import collections, hashlib, json, shlex, sys
from datetime import datetime, timezone
sys.path.insert(0, 'platform')
from ssh_client import connect, execute, ROOT

code = r'''
import collections, hashlib, json, os
import psycopg
with psycopg.connect(os.environ['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://')) as c:
 runs=c.execute("SELECT id,status,calls,tokens FROM runs WHERE workspace='main' ORDER BY created DESC").fetchall()
 report=[]
 for run_id,status,calls,tokens in runs:
  rows=c.execute('SELECT status,score,data FROM models WHERE run_id=%s',(run_id,)).fetchall()
  items=[];checks=0;findings=0;history=True;contracts=True
  for model_status,score,raw in rows:
   data=json.loads(raw)
   for stage,key in [('assessment','claims'),('critique','risks'),('verification','checks')]:items.extend(data.get(stage,{}).get(key,[]))
   checks+=bool(data.get('consistency',{}).get('checked'))
   findings+=len(data.get('consistency',{}).get('findings',[]))
   history=history and bool(data.get('verification_history') or any(item.get('data',{}).get('verification') for item in data.get('analysis_history',[])))
   contracts=contracts and data.get('verification',{}).get('contract_version')=='3.0'
  invocations=c.execute('SELECT role,input,input_hash FROM invocations WHERE run_id=%s',(run_id,)).fetchall()
  hashes=all(hashlib.sha256(packet.encode()).hexdigest()==digest for role,packet,digest in invocations)
  verifier_packets=[json.loads(packet) for role,packet,digest in invocations if role=='verifier']
  critic_packets=[json.loads(packet) for role,packet,digest in invocations if role=='critic']
  isolation=bool(verifier_packets and critic_packets) and all(set(packet)=={'company','claims','sources'} and all(set(claim)<= {'claim_id','statement','factual'} for claim in packet['claims']) for packet in verifier_packets) and all(set(packet)=={'candidate','conditions','sources'} for packet in critic_packets)
  report.append(dict(id=run_id,status=status,cards=len(rows),counts=dict(collections.Counter(row[0] for row in rows)),calls=calls,tokens=tokens,
    passports=len(items),complete_passports=all(all(item.get('passport',{}).get(k) for k in ('date','method','confidence','source_status','invocation_id')) for item in items),
    verifier_contract='3.0' if contracts else 'mixed',consistency_checks=checks,conflict_findings=findings,prior_verification_preserved=history,
    scores=[row[1] for row in rows if row[1] is not None],all_invocation_hashes_valid=hashes,invocations=len(invocations),agent_packet_isolation_valid=isolation))
 print(json.dumps(report))
'''
with connect() as client:
 output=execute(client,'k3s kubectl -n venture-lens-demo exec deployment/api -- python -c '+shlex.quote(code),timeout=60)
report=json.loads(output)
assert len(report)==2 and sum(row['cards'] for row in report)==20
assert all(row['complete_passports'] and row['all_invocation_hashes_valid'] and row['agent_packet_isolation_valid'] and row['verifier_contract']=='3.0' and row['consistency_checks']==10 and row['prior_verification_preserved'] for row in report)
(ROOT/'.platform/spec-live-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
