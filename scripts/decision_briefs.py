from backend.runtime_config import default_prompt
"""Enrich inactive real cards with labelled launch hypotheses; never changes admission or ranking."""
import argparse
import hashlib
import time
from sqlalchemy import select
from backend.db import Session,Run,Model,Job,Event,read,dump
from backend.llm import invoke
from backend.schemas import LaunchBrief
from backend.passports import normalize_data,provenance_for
from backend.decision_support import business_conditions


def main(run_id=None,model_id=None,replace=False):
    if replace and not model_id:raise ValueError('Replacement requires an explicit model ID')
    with Session() as session:
        query=select(Model.id).join(Run,Model.run_id==Run.id).where(Run.workspace=='main')
        if run_id:query=query.where(Run.id==run_id)
        if model_id:query=query.where(Model.id==model_id)
        ids=list(session.scalars(query))
    completed=0
    for id in ids:
        with Session() as session:
            model=session.get(Model,id);run=session.get(Run,model.run_id);data=read(model.data)
            if not data.get('assessment'):continue
            if (data.get('launch_brief') or data.get('assessment',{}).get('launch_brief')) and not replace:continue
            if run.status not in ('completed','completed_with_gaps','partial'):raise RuntimeError('Only inactive research may be enriched')
            if session.scalar(select(Job.id).where(Job.run_id==run.id,Job.status=='running').limit(1)):raise RuntimeError('Research has a running job')
            generation=run.generation;fingerprint=hashlib.sha256(model.data.encode()).hexdigest();rid=run.id
            packet={'hypothesis':data['candidate'],'conditions':business_conditions(read(run.settings)),
                    'existing_card':{key:data['assessment'].get(key) for key in ('summary','customer','problem','revenue','first_sale','experiment','unknowns','claims')}}
        brief=invoke(rid,'launch_brief',packet,LaunchBrief,
            default_prompt('launch_brief'),id,max_tokens=3000)
        with Session.begin() as session:
            query=select(Run).where(Run.id==rid)
            if session.bind.dialect.name=='postgresql':query=query.with_for_update()
            run=session.scalar(query);model=session.get(Model,id)
            if run.generation!=generation or run.status not in ('completed','completed_with_gaps','partial') or hashlib.sha256(model.data.encode()).hexdigest()!=fingerprint:
                raise RuntimeError('Card changed while generating its brief; result not applied')
            data=read(model.data)
            if replace:
                data.setdefault('launch_brief_history',[]).append({'at':time.time(),'brief':data.get('launch_brief') or data['assessment'].get('launch_brief'),'provenance':data.get('launch_brief_provenance')})
                data['assessment'].pop('launch_brief',None)
            data['launch_brief']=LaunchBrief.model_validate(brief).model_dump()
            normalize_data(data,provenance_for(session,id),run.created);model.data=dump(data)
            session.add(Event(run_id=rid,model_id=id,stage='launch_brief',message='Добавлена гипотеза первого эксперимента с допущениями. Допуск и индекс сохранены.'))
        completed+=1
        print('BRIEF_READY '+id,flush=True)
    print('BRIEFS_COMPLETED '+str(completed),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-id');parser.add_argument('--model-id');parser.add_argument('--replace',action='store_true');args=parser.parse_args()
    main(args.run_id,args.model_id,args.replace)
