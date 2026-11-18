from .runtime_config import default_prompt
import hashlib
import copy
import time
from .db import Session, Run, Model, Job, Invocation, dump, read, emit, uid
from .schemas import Discovery, Assessment, Critique, Verification, SearchPlan
from .evidence import collect, fetch, grounded, normalize, financial_quote_matches
from .llm import invoke, Cancelled
from .execution import assert_owner,current,draining,DependencyUnavailable
from .passports import normalize_data, provenance_for
from .consistency import reconcile
from .decision_support import business_conditions

TERMINAL={'passed','rejected','review','error'}

def guard(run_id):
    with Session.begin() as s:
        r=assert_owner(s,run_id)
        if r.active_seconds>=read(r.policy)['limits']['research_timeout_minutes']*60:
            raise RuntimeError('Превышен бюджет активного выполнения исследования')
    if current.get() is not None and draining.is_set():raise DependencyUnavailable('Исполнитель завершает работу; этап будет продолжен',1,False)

def save_model(model_id,data,**values):
    with Session.begin() as s:
        m=s.get(Model,model_id);run=assert_owner(s,m.run_id)
        normalize_data(data,provenance_for(s,model_id),run.created)
        m.data=dump(data)
        for k,v in values.items():setattr(m,k,v)

def source_errors(run_id,model_id=None):
    return lambda reason,target:emit(run_id,'source_warning',reason,model_id,{'target':target})

def context_sources(sources,max_total_chars=24000):
    """Bound context size; preserve exact excerpts so grounding still uses original snapshots."""
    limit=max(1200,max_total_chars//max(1,len(sources)))
    terms={'valuation':5,'valued':5,'market cap':5,'acquisition':4,'billion':3,'million':3,'pricing':3,'subscription':3,'license':3,'licence':3,'payment':2,'monthly':2,'annually':2,'customer':1}
    result=[]
    for source in sources:
        record={k:v for k,v in source.items() if k in ['id','title','url','text','published','origin']}
        text=record.get('text','')
        if len(text)>limit:
            windows=[(i,text[i:i+900]) for i in range(0,len(text),800)]
            ranked=sorted(windows,key=lambda x:sum(x[1].casefold().count(term)*weight for term,weight in terms.items()),reverse=True)
            chosen={0:text[:700]};used=700
            for position,excerpt in ranked:
                if position==0 or used+len(excerpt)+8>limit:continue
                chosen[position]=excerpt;used+=len(excerpt)+8
            record['text']='\n[…]\n'.join(chosen[p] for p in sorted(chosen))
            record['excerpted']=True
        result.append(record)
    return result

def discover(run_id):
    from .runtime_config import PROFILES,market_label
    guard(run_id)
    with Session() as s:
        r=s.get(Run,run_id);config=read(r.settings);pol=read(r.policy)
        if s.query(Model).filter_by(run_id=run_id).count():return
    market={'US':'United States','UK':'United Kingdom','EU':'European Union','Global':'international markets'}.get(config['region'],config.get('market',''))
    sources=config.get('discovery_sources',[])
    if not sources:
        emit(run_id,'discovery','Ищем источники под выбранный рынок и направление',payload={'market':market_label(config),'sampling':config.get('sampling','legacy')})
        for url in config.get('source_urls',[]):
            try:sources.append(fetch(url))
            except (Cancelled,DependencyUnavailable):raise
            except Exception:source_errors(run_id)('Добавленный источник недоступен или заблокирован проверкой URL',url)
        try:
            sources.extend(collect([f"{config['focus']} {market} subscription companies examples",f"{market} B2B software business models {config['focus']}"],max_sources=6,on_error=source_errors(run_id)))
        except (Cancelled,DependencyUnavailable):
            if not sources:
                # Known catalogues are examples only; selected market remains mandatory.
                for url in pol.get('discovery_urls',[]):
                    try:sources.append(fetch(url))
                    except Exception:source_errors(run_id)('Резервный каталог недоступен',url)
            if not sources:raise
        except Exception:
            for url in pol.get('discovery_urls',[]):
                try:sources.append(fetch(url))
                except Exception:source_errors(run_id)('Резервный каталог недоступен',url)
        sources=list({item['id']:item for item in sources}.values())
        if not sources:raise RuntimeError('Нет доступных источников для выбранного рынка. Добавьте свои ссылки и повторите поиск.')
        with Session.begin() as s:
            r=assert_owner(s,run_id);value=read(r.settings);value['discovery_sources']=sources;r.settings=dump(value)
    profile=PROFILES.get(config.get('sampling','balanced'),PROFILES['balanced'])
    adaptive=config.get('adaptive_selection',False)
    rounds=profile['rounds'] if adaptive else 1
    progress=config.get('selection_progress',{})
    candidates=progress.get('candidates',[]);completed=progress.get('round',0)
    seen={normalize(item.get('business_model_key') or item['name']).casefold() for item in candidates}
    valid_ids={item['id'] for item in sources}
    cap=config['count'];reason='depth_limit';coverage=progress.get('coverage','')
    for round_index in range(completed,rounds):
        guard(run_id)
        with Session() as s:
            r=s.get(Run,run_id)
            available=max(0,(pol['limits']['max_llm_calls']-r.calls-1)//5-len(candidates))
        if not available:reason='request_budget';break
        remaining=min(cap-len(candidates),available)
        if remaining<=0:reason='depth_limit';break
        packet={'market':market,'focus':config['focus'],'team':config['team'],'budget':config['budget'],'batch_size':min(4,remaining) if adaptive else remaining,
                'existing_models':[{k:item.get(k) for k in ('name','company','business_model_key')} for item in candidates],'sources':context_sources(sources)}
        result=invoke(run_id,'discovery',packet,Discovery,default_prompt('discovery'),max_tokens=12000)
        new=[]
        for candidate in result['candidates']:
            key=normalize(candidate.get('business_model_key') or candidate['name']).casefold()
            if candidate['discovery_source_id'] not in valid_ids or key in seen:continue
            seen.add(key);new.append(candidate)
            if len(new)>=packet['batch_size']:break
        candidates.extend(new);coverage=result.get('coverage_summary','')
        with Session.begin() as s:
            r=assert_owner(s,run_id);value=read(r.settings)
            value['selection_progress']={'round':round_index+1,'candidates':candidates,'coverage':coverage};r.settings=dump(value)
        emit(run_id,'discovery',f'Добавлено новых гипотез: {len(new)}. Всего: {len(candidates)}.',payload={'round':round_index+1,'coverage':coverage})
        if not new:reason='no_new_hypotheses';break
        if adaptive and result.get('coverage_sufficient') and len(candidates)>=2:reason='coverage';break
    if not candidates:raise RuntimeError('Источники не дали проверяемых гипотез под заданные условия. Уточните направление или добавьте источники.')
    with Session.begin() as s:
        r=assert_owner(s,run_id);value=read(r.settings)
        value['selection']={'mode':'adaptive' if adaptive else 'legacy','sampling':config.get('sampling','balanced'),'found':len(candidates),'stop_reason':reason,'coverage':coverage,'source_count':len(sources)}
        value.pop('selection_progress',None);r.settings=dump(value)
        for candidate in candidates:
            m=Model(id=uid(),run_id=run_id,name=candidate['name'],company=candidate['company'],data=dump({'candidate':candidate,'discovery_source':next(item for item in sources if item['id']==candidate['discovery_source_id'])}))
            s.add(m);s.flush();s.add(Job(run_id=run_id,model_id=m.id,key=f'model:{m.id}'))
    emit(run_id,'discovery',f'Подбор завершён: {len(candidates)} разных гипотез для проверки.',payload={'found':len(candidates),'stop_reason':reason,'coverage':coverage})


def apply_evidence(assessment,sources,pol):
    seen=set()
    for claim in assessment['claims']:
        if claim['id'] in seen:raise ValueError('Повторяющийся идентификатор утверждения')
        seen.add(claim['id'])
        ok=grounded(claim,sources)
        source=next((x for x in sources if x['id']==claim['source_id']),None)
        claim['grounded']=ok
        claim['verification']='quoted' if ok else 'unverified'
        claim['source_url']=source['url'] if source else None
        claim['retrieved']=source['retrieved'] if source else None
        claim['source_date']=source.get('published') if source else None
        claim['source_kind']=source['origin'] if source else None
        if not ok and claim['method']=='primary_source':claim['confidence']='low'
    claim_map={x['id']:x for x in assessment['claims']}
    if {g['id'] for g in assessment['gates']}!={g['id'] for g in pol['criteria']} or len(assessment['gates'])!=len(pol['criteria']):
        raise ValueError('Неполный набор обязательных критериев')
    for gate in assessment['gates']:
        supported=[claim_map[c] for c in gate['claim_ids'] if c in claim_map and claim_map[c]['grounded'] and claim_map[c]['source_kind']!='search_snippet']
        if gate['status'] in ('pass','reject') and not supported:
            gate['status']='review';gate['reason']+=' Нет проверенной цитаты полной страницы; требуется проверка.'
        if gate['id']=='large_analogue':
            benchmark=assessment.get('benchmark',{})
            fact=claim_map.get(benchmark.get('claim_id'),{})
            if benchmark.get('metric') not in ('valuation','market_cap','acquisition_value') or (benchmark.get('amount_usd') or 0)<pol.get('minimum_analogue_usd',300000000) or not fact.get('grounded') or fact.get('source_kind')=='search_snippet' or not financial_quote_matches(fact,benchmark.get('amount_usd') or 0):
                gate['status']='review'
                gate['reason']='Порог оценки/капитализации/стоимости сделки от $300 млн не подтверждён доступными источниками. Объём привлечённых инвестиций и выручка не заменяют оценку бизнеса. Требуется дополнительный источник или другой сопоставимый аналог.'
                assessment['analogue']=f'{benchmark.get("company") or "Компания-аналог"}: масштаб от $300 млн пока не подтверждён. См. паспорта доказательств.'
            else:
                metric={'valuation':'оценка','market_cap':'капитализация','acquisition_value':'стоимость сделки'}[benchmark['metric']]
                assessment['analogue']=f'{benchmark["company"]}: {metric} ${benchmark["amount_usd"]/1e6:g} млн по сохранённому источнику (историческое значение).'
    if {d['id'] for d in assessment['dimensions']}!=set(pol['weights']) or len(assessment['dimensions'])!=len(pol['weights']):
        raise ValueError('Неполный набор критериев рейтинга')
    for d in assessment['dimensions']:
        if not any(c in claim_map and claim_map[c]['grounded'] for c in d['claim_ids']):d['score']=None
    return assessment

def calculate(dimensions,weights):
    lower=0.;upper=0.;coverage=0.
    for d in dimensions:
        w=weights[d['id']]['weight']
        if d['score'] is None:upper+=w*100
        else:lower+=w*d['score']*20;upper+=w*d['score']*20;coverage+=w
    return round(lower,1),round(upper,1),round(coverage*100)

def process_model(model_id):
    with Session() as s:
        m=s.get(Model,model_id);run_id=m.run_id;data=read(m.data);r=s.get(Run,run_id);config=read(r.settings);pol=read(r.policy)
    guard(run_id)
    candidate=data['candidate']
    save_model(model_id,data,status='running',error='')
    if 'sources' not in data:
        emit(run_id,'dossier',f'Собираем досье: {candidate["company"]}',model_id)
        try:
            data['sources']=collect([candidate.get('query') or candidate['company'],f'{candidate["company"]} {config.get("market") or config["region"]} pricing',f'{candidate["company"]} valuation'],pol['limits']['max_sources_per_model'],source_errors(run_id,model_id))
        except (Cancelled,DependencyUnavailable):raise
        except Exception:
            if not data.get('discovery_source'):raise
            data['sources']=[data['discovery_source']]
            emit(run_id,'source_warning','Поиск недоступен: предварительная карточка ограничена исходным каталогом. Пробелы не заполняются фактами из памяти.',model_id)
        manual=[item['url'] for item in config.get('manual_sources',[]) if item.get('model_id') in (None,model_id)]
        for url in list(dict.fromkeys(data.get('additional_source_urls',[])+manual))[:10]:
            if any(source['url']==url for source in data['sources']):continue
            try:data['sources'].append(fetch(url))
            except (Cancelled,DependencyUnavailable):raise
            except Exception:source_errors(run_id,model_id)('Добавленный материал недоступен или заблокирован проверкой URL',url)
        save_model(model_id,data,stage='screening')
    guard(run_id)
    if 'assessment' not in data:
        emit(run_id,'screening','Проверяем обязательные условия и готовим сопоставимые оценки',model_id)
        result=invoke(run_id,'screening',{'candidate':candidate,'conditions':business_conditions(config),'criteria':pol['criteria'],'weights':pol['weights'],'sources':context_sources(data['sources'],pol['limits'].get('context_chars',24000))},Assessment,
            default_prompt('screening'),model_id)
        data['assessment']=apply_evidence(result,data['sources'],pol)
        save_model(model_id,data,stage='critique')
    assessment=data['assessment']
    guard(run_id)
    statuses={g['status'] for g in assessment['gates']}
    if 'reject' in statuses:
        save_model(model_id,data,status='rejected',stage='completed')
        rejected=[{'criterion_id':g['id'],'reason':g['reason'],'claim_ids':g['claim_ids']} for g in assessment['gates'] if g['status']=='reject']
        emit(run_id,'rejected','Модель отклонена: '+'; '.join(item['criterion_id']+': '+item['reason'] for item in rejected),model_id,{'criteria':[item['criterion_id'] for item in rejected],'rejections':rejected});return
    provisional='review' in statuses
    if provisional and not pol.get('audit_review_models',False):
        save_model(model_id,data,status='review',stage='completed')
        emit(run_id,'review','Недостаточно данных для прохождения обязательных фильтров',model_id);return
    if provisional:emit(run_id,'review_audit','Предварительная карточка: проводим аудит рисков, сохраняя недопуск в рейтинг',model_id)
    guard(run_id)
    if 'critique' not in data:
        emit(run_id,'critique','Независимый критик ищет причины срыва запуска',model_id)
        data['critique']=invoke(run_id,'critic',{'candidate':candidate,'conditions':business_conditions(config),'sources':context_sources(data['sources'],pol['limits'].get('context_chars',24000))},Critique,
            default_prompt('critic'),model_id,max_tokens=8000)
        save_model(model_id,data,stage='verification')
    guard(run_id)
    if data.get('verification') and data['verification'].get('contract_version')!='3.0':
        data.setdefault('verification_history',[]).append({'at':time.time(),'result':data.pop('verification'),
                                                          'sources':data.get('verification_sources',[]),'queries':data.get('verification_queries',[])})
        data['reuse_verification_sources']=True
    if 'verification' not in data:
        emit(run_id,'verification','Фактчекер проверяет отдельные тезисы, не видя оценок и вердикта критика',model_id)
        risks=data['critique']['risks']
        # Whitelist: no critic title, severity, confidence, verdict, history or original citations.
        claims=[{'claim_id':x['id'],'statement':x['statement'],'factual':x['factual']} for x in risks]
        factual=[x for x in risks if x['factual']]
        verified_sources=data.get('verification_sources',[])
        if factual:
            if 'verification_queries' not in data:
                plan=invoke(run_id,'verification_search',{'company':candidate['company'],'claims':claims},SearchPlan,
                    default_prompt('verification_search'),model_id,max_tokens=1500)
                data['verification_queries']=plan['queries'];save_model(model_id,data,stage='verification')
            try:
                if not verified_sources and not data.get('reuse_verification_sources'):verified_sources=collect(data['verification_queries'],pol['limits']['max_sources_per_model'],source_errors(run_id,model_id))
            except (Cancelled,DependencyUnavailable):raise
            except Exception:emit(run_id,'source_warning','Независимый поиск недоступен; проверка помечается как неподтверждённая',model_id)
        data['verification_sources']=verified_sources
        result=invoke(run_id,'verifier',{'company':candidate['company'],'claims':claims,'sources':context_sources(verified_sources,pol['limits'].get('context_chars',24000))},Verification,
            default_prompt('verifier'),model_id,max_tokens=5000)
        checks={x['claim_id']:x for x in result['checks']}
        validated=[]
        for claim in claims:
            check=checks.get(claim['claim_id'],{'claim_id':claim['claim_id'],'status':'unknown','explanation':'Проверяющий не вернул результат','source_id':None,'quote':None,'source_date':None,'confidence':'low'})
            source=next((x for x in verified_sources if x['id']==check['source_id']),None)
            check['grounded']=grounded(check,verified_sources)
            check['source_url']=source['url'] if source else None
            check['retrieved']=source['retrieved'] if source else None
            check['source_date']=source.get('published') if source else None
            if not claim['factual']:check['status']='judgment'
            elif check['status']=='judgment':check['status']='unknown'
            elif check['status']!='unknown' and (not check['grounded'] or not source or source['origin']=='search_snippet'):
                check['status']='unknown';check['explanation']+=' Цитата полной страницы не подтверждена сервером.'
            validated.append(check)
        data['verification']={'checks':validated,'contract_version':'3.0'}
        save_model(model_id,data,stage='scoring')
    data.setdefault('screening_snapshot',copy.deepcopy(assessment))
    findings=reconcile(data,pol)
    provisional=any(g['status']=='review' for g in assessment['gates'])
    if findings and not data.get('consistency_event_emitted'):
        emit(run_id,'consistency','Модель направлена на проверку: независимые доказательства противоречат обязательным условиям',model_id,{'findings':findings})
        data['consistency_event_emitted']=True
    # Scores reflect the predeclared rubric; risk checks are separate, never silently rewrite history.
    low,high,coverage=calculate(assessment['dimensions'],pol['weights'])
    penalties=pol.get('risk_penalties',{})
    check_map={x['claim_id']:x for x in data['verification']['checks']}
    deductions=[]
    for risk in data['critique']['risks']:
        check=check_map.get(risk['id'],{})
        multiplier=1 if check.get('status')=='supported' else penalties.get('partial_multiplier',0) if check.get('status')=='partial' else 0
        deductions.append(penalties.get(risk['impact'],0)*multiplier)
    penalty=max(deductions,default=0)
    data['scoring']={'base_lower':low,'base_upper':high,'risk_penalty':penalty,'rule':'largest_confirmed_risk','policy_version':pol['version']}
    low,high=round(max(0,low-penalty),1),round(max(0,high-penalty),1)
    for risk in data['critique']['risks']:
        risk['grounded']=grounded(risk,data['sources'])
        src=next((x for x in data['sources'] if x['id']==risk['source_id']),None)
        risk['source_url']=src['url'] if src else None
        risk['retrieved']=src['retrieved'] if src else None
        risk['source_date']=src.get('published') if src else None
    guard(run_id)
    save_model(model_id,data,status='review' if provisional else 'passed',stage='completed',score=None if provisional else low,score_upper=None if provisional else high,coverage=coverage)
    emit(run_id,'completed',('Предварительная карточка с аудитом готова; условия допуска требуют проверки' if provisional else f'Карточка готова: индекс {low:g}/100, покрытие {coverage:g}%'),model_id)

def finish_run(run_id):
    with Session.begin() as s:
        from sqlalchemy import select
        query=select(Run).where(Run.id==run_id)
        if s.bind.dialect.name=='postgresql':query=query.with_for_update()
        r=s.scalar(query)
        if not r or r.status=='cancelled':return
        jobs=s.query(Job).filter_by(run_id=run_id).all()
        if not jobs:return
        active={j.status for j in jobs}
        if 'running' in active:r.status='running'
        elif 'queued' in active:r.status='queued'
        elif 'waiting' in active:r.status='waiting'
        else:
            r.status='partial' if 'failed' in active else 'completed_with_gaps' if r.degraded else 'completed'
            r.waiting_reason=''
            if r.status!='partial':r.error=''
        r.updated=time.time()
