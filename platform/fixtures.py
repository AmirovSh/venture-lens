"""Explicitly synthetic wire-protocol provider for isolated CI/fault tests only."""
from pathlib import Path
import json
import yaml

ROOT=Path(__file__).resolve().parent.parent
QUOTE='Fixture company offers web-based subscription software. The company was valued at $500 million in 2025.'

def assessment():
    policy=yaml.safe_load((ROOT/'config/policy.yaml').read_text(encoding='utf-8'))
    return {'summary':'Synthetic CI fixture, not a real business recommendation','customer':'Fixture customer','problem':'Fixture problem','revenue':'Subscription','analogue':'Synthetic valuation','benchmark':{'company':'Fixture','metric':'valuation','amount_usd':500000000,'claim_id':'c1'},'first_sale':'Synthetic paid pilot','gates':[{'id':g['id'],'status':'pass','reason':'Synthetic fixture only','claim_ids':['c1']} for g in policy['criteria']],'claims':[{'id':'c1','statement':'Synthetic source statement','source_id':'fixture','quote':QUOTE,'source_date':'2025-01-01','method':'primary_source','confidence':'high','rationale':'Synthetic fixture'}],'dimensions':[{'id':k,'score':4,'reason':'Synthetic assessment','claim_ids':['c1']} for k in policy['weights']],'experiment':[{'days':'1–14','action':'Synthetic action','success':'Synthetic success','stop':'Synthetic stop'}],'unknowns':['Not a real company']}

def resources(namespace,image):
    responses={'screening':assessment(),'critic':{'risks':[{'id':'r1','title':'Synthetic risk','statement':'This is a synthetic judgment','impact':'low','factual':False,'search_query':'Fixture company','confidence':'low','method':'model_judgment','source_id':None,'quote':None,'source_date':None}]},'verifier':{'checks':[{'claim_id':'r1','status':'judgment','explanation':'Synthetic judgment','source_id':None,'quote':None,'source_date':None,'confidence':'low'}]},'verification_search':{'queries':['Fixture company']},'discovery':{'candidates':[]}}
    code='''import json,time,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
RESPONSES=json.load(open('/fixture/responses.json'))
STATE={'delay':0,'failures':0,'requests':0}
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(json.dumps(STATE).encode())
 def do_POST(self):
  data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path=='/control':
   STATE.update(data);self.send_response(200);self.end_headers();self.wfile.write(b'{}');return
  STATE['requests']+=1
  if STATE['failures']>0:
   STATE['failures']-=1;self.send_response(503);self.send_header('Retry-After','1');self.end_headers();self.wfile.write(b'{}');return
  time.sleep(STATE['delay'])
  role=data['response_format']['json_schema']['name']
  body={'choices':[{'message':{'content':json.dumps(RESPONSES[role])}}],'usage':{'total_tokens':100}}
  self.send_response(200);self.end_headers()
  try:self.wfile.write(json.dumps(body).encode())
  except BrokenPipeError:pass
 def log_message(self,*args):pass
ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''
    return [{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'llm-fixture','namespace':namespace},'data':{'provider.py':code,'responses.json':json.dumps(responses)}},{'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'llm-fixture','namespace':namespace},'spec':{'replicas':1,'selector':{'matchLabels':{'app':'llm-fixture'}},'template':{'metadata':{'labels':{'app':'llm-fixture'}},'spec':{'automountServiceAccountToken':False,'containers':[{'name':'fixture','image':image,'command':['python','/fixture/provider.py'],'ports':[{'containerPort':8080}],'volumeMounts':[{'name':'fixture','mountPath':'/fixture','readOnly':True}],'resources':{'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'cpu':'1','memory':'96Mi'}}}],'volumes':[{'name':'fixture','configMap':{'name':'llm-fixture'}}]}}}},{'apiVersion':'v1','kind':'Service','metadata':{'name':'llm-fixture','namespace':namespace},'spec':{'selector':{'app':'llm-fixture'},'ports':[{'port':8080,'targetPort':8080}]}}]

def seed_script(title):
    fixture=assessment()
    return f'''from backend.db import Session,Run,Model,Job,uid,dump
from backend.settings import policy
from backend.storage import put_source
import time
quote={QUOTE!r}
checksum,uri=put_source(quote)
with Session.begin() as session:
 run=Run(id=uid(),title={title!r},settings=dump({{'region':'US','count':1,'budget':10000,'team':'Synthetic CI team','focus':'Synthetic CI fixture'}}),policy=dump(policy()))
 session.add(run);session.flush()
 source={{'id':'fixture','url':'https://fixture.example.invalid','title':'SYNTHETIC CI SOURCE','text':quote,'published':'2025-01-01','origin':'page','retrieved':time.time(),'sha256':checksum,'storage_uri':uri}}
 model=Model(id=uid(),run_id=run.id,name='[TEST] Synthetic model',company='Fixture',data=dump({{'candidate':{{'name':'Synthetic','company':'Fixture','description':'Synthetic CI data','query':'Fixture'}},'sources':[source]}}))
 session.add(model);session.flush();session.add(Job(run_id=run.id,model_id=model.id,key='model:'+model.id))
 print(run.id)
'''
