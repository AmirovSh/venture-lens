"""Requeue risk checks, preserving previous results and successful upstream stages."""
from pathlib import Path
import sys
import time
import copy
import argparse
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from backend.db import Session,Run,Model,Job,dump,read,emit
from backend.pipeline import apply_evidence

def main(run_id,reuse_sources=False):
    changed=0
    with Session.begin() as s:
        r=s.get(Run,run_id)
        if not r or r.status not in ('completed','completed_with_gaps','partial'):raise RuntimeError('Run must exist and be inactive')
        if s.query(Job).filter_by(run_id=run_id,status='running').count():raise RuntimeError('Stop active workers before rechecking')
        pol=read(r.policy)
        for m in s.query(Model).filter_by(run_id=run_id):
            data=read(m.data)
            if data.get('screening_snapshot'):data['assessment']=copy.deepcopy(data['screening_snapshot'])
            if data.get('assessment'):data['assessment']=apply_evidence(data['assessment'],data.get('sources',[]),pol)
            if m.status=='rejected':continue
            if data.get('verification'):
                data.setdefault('verification_history',[]).append({'at':time.time(),'result':data['verification'],'sources':data.get('verification_sources',[]),'queries':data.get('verification_queries',[])})
            keys=['verification','scoring','consistency','consistency_event_emitted']
            if not reuse_sources:keys+=['verification_sources','verification_queries']
            for key in keys:data.pop(key,None)
            data['reuse_verification_sources']=reuse_sources
            m.data=dump(data);m.status='queued';m.error='';m.score=None;m.score_upper=None
            job=s.query(Job).filter_by(model_id=m.id).one();job.status='queued';job.lease_until=0;job.lease_owner=None
            job.epoch+=1;job.attempts=0;job.next_retry_at=0
            changed+=1
        r.status='queued';r.error='';r.waiting_reason='';r.generation+=1
    emit(run_id,'recheck',f'Повторная проверка {changed} карточек. Предыдущие версии сохранены; готовые досье и критика переиспользуются.')
    print('Requeued models:',changed)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('run_id');parser.add_argument('--reuse-sources',action='store_true')
    args=parser.parse_args();main(args.run_id,args.reuse_sources)
