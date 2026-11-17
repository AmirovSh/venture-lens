import hashlib
import time
from .db import Session, Run, Model, Job, Invocation, dump, read, emit, uid
from .schemas import Discovery, Assessment, Critique, Verification
from .evidence import collect, grounded, normalize
from .llm import invoke, Cancelled

TERMINAL={'passed','rejected','review','error'}

def guard(run_id):
    with Session() as s:
        r=s.get(Run,run_id)
        if not r or r.status=='cancelled':raise Cancelled('Исследование остановлено')
        if time.time()-r.created > read(r.policy)['limits']['research_timeout_minutes']*60:
            raise RuntimeError('Превышен лимит времени исследования')

def save_model(model_id,data,**values):
    with Session.begin() as s:
        m=s.get(Model,model_id);m.data=dump(data)
        for k,v in values.items():setattr(m,k,v)

def source_errors(run_id,model_id=None):
    return lambda reason,target:emit(run_id,'source_warning',reason,model_id,{'target':target})

def context_sources(sources):
    return [{k:v for k,v in x.items() if k in ['id','title','url','text','published','origin']} for x in sources]

def discover(run_id):
    guard(run_id)
    with Session() as s:
        r=s.get(Run,run_id); config=read(r.settings); pol=read(r.policy)
        if s.query(Model).filter_by(run_id=run_id).count():return
    emit(run_id,'discovery','Ищем бизнес-модели и компании-примеры в открытых источниках')
    market={'US':'United States','UK':'United Kingdom','EU':'European Union','Global':'US Europe excluding CIS'}[config['region']]
    # Short catalogue queries work across public search engines; the LLM applies scope afterwards.
    sources=collect(['SaaS','subscription business','marketplace business model'],max_sources=9,on_error=source_errors(run_id))
    with Session.begin() as s:
        # Preserve discovery provenance even when the provider fails.
        r=s.get(Run,run_id); c=read(r.settings);c['discovery_sources']=sources;r.settings=dump(c)
    result=invoke(run_id,'discovery',{'market':market,'focus':config['focus'],'count':config['count'],'sources':context_sources(sources)},Discovery,
        'Извлеки указанное число РАЗЛИЧНЫХ бизнес-моделей из данных источников. Не дублируй одну модель под разными компаниями. '
        'discovery_source_id обязан существовать в источниках. query — поисковый запрос на английском для проверки компании, продукта и оценки. '
        'Не требуется, чтобы все модели прошли фильтры; допускаются кандидаты для обоснованного отклонения. Не выдумывай недостающих кандидатов.',max_tokens=5000)
    valid_ids={x['id'] for x in sources};seen=set();candidates=[]
    for candidate in result['candidates']:
        key=normalize(candidate['name']).casefold()
        if candidate['discovery_source_id'] not in valid_ids or key in seen:continue
        seen.add(key);candidates.append(candidate)
    if not candidates:raise RuntimeError('Источники не дали проверяемых кандидатов')
    with Session.begin() as s:
        for candidate in candidates[:config['count']]:
            m=Model(id=uid(),run_id=run_id,name=candidate['name'],company=candidate['company'],data=dump({'candidate':candidate,'discovery_source':next(x for x in sources if x['id']==candidate['discovery_source_id'])}))
            s.add(m);s.flush();s.add(Job(run_id=run_id,model_id=m.id,key=f'model:{m.id}'))
    emit(run_id,'discovery',f'Получено {len(candidates[:config["count"]])} различных кандидатов',payload={'requested':config['count'],'found':len(candidates[:config['count']])})

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
        data['sources']=collect([candidate['query'], f'{candidate["company"]} official pricing subscription product terms',f'{candidate["company"]} valuation funding acquisition company'],pol['limits']['max_sources_per_model'],source_errors(run_id,model_id))
        save_model(model_id,data,stage='screening')
    guard(run_id)
    if 'assessment' not in data:
        emit(run_id,'screening','Проверяем обязательные условия и готовим сопоставимые оценки',model_id)
        result=invoke(run_id,'screening',{'candidate':candidate,'conditions':{k:v for k,v in config.items() if k!='discovery_sources'},'criteria':pol['criteria'],'weights':pol['weights'],'sources':context_sources(data['sources'])},Assessment,
            'Оцени возможность НАШЕГО запуска теста и первой оплаты за 90 дней в указанной юрисдикции и команде. '
            'gates: ровно все criteria с их id. dimensions: ровно все weights с их id. Шкала 0..5; null если нет оснований. '
            '5 = доступен дешёвый быстрый самостоятельный запуск и покупка; 0 = практически невозможно за 90 дней. '
            'Для gate pass/reject обязательны claim_ids с цитатами источников. Не подменяй оценку компании суммой инвестиций или выручкой. '
            'Юридическая неопределённость = review; CIS исключён. Данные о лицензиях нужны для предполагаемой нашей модели. '
            'Условие крупного аналога — подтверждённая оценка/капитализация/сделка порядка $300м и выше с датой. '
            'Оценки скорости и продаж — суждения с явными предпосылками; не придумывай CAC, сроки и покупателей. '
            'Каждому существенному утверждению карточки дай паспорт в claims. experiment: 3 этапа 1–14, 15–30, 31–90 дней с success и stop.',model_id)
        data['assessment']=apply_evidence(result,data['sources'],pol)
        save_model(model_id,data,stage='critique')
    assessment=data['assessment']
    statuses={g['status'] for g in assessment['gates']}
    if 'reject' in statuses:
        save_model(model_id,data,status='rejected',stage='completed')
        emit(run_id,'rejected','Модель отклонена обязательными фильтрами',model_id,{'criteria':[g['id'] for g in assessment['gates'] if g['status']=='reject']});return
    if 'review' in statuses:
        save_model(model_id,data,status='review',stage='completed')
        emit(run_id,'review','Недостаточно данных для прохождения обязательных фильтров',model_id);return
    guard(run_id)
    if 'critique' not in data:
        emit(run_id,'critique','Независимый критик ищет причины срыва запуска',model_id)
        data['critique']=invoke(run_id,'critic',{'candidate':candidate,'conditions':{k:v for k,v in config.items() if k!='discovery_sources'},'sources':context_sources(data['sources'])},Critique,
            'Найди 1–3 САМЫЕ СИЛЬНЫЕ причины, почему за 90 дней не получим первую оплату. Ты не видишь оценки других агентов. '
            'Отделяй проверяемое фактическое утверждение factual=true от прогноза/суждения factual=false. '
            'Не выдумывай факты и источники. Для каждого риска укажи метод, уверенность, source_id и дословную quote либо null, source_date либо null. '
            'Для суждений метод model_judgment или human_required. search_query — нейтральный запрос для независимого поиска факта.',model_id,max_tokens=3500)
        save_model(model_id,data,stage='verification')
    guard(run_id)
    if 'verification' not in data:
        emit(run_id,'verification','Фактчекер проверяет отдельные тезисы, не видя оценок и вердикта критика',model_id)
        risks=data['critique']['risks']
        # Whitelist: no critic title, severity, confidence, verdict, history or original citations.
        claims=[{'claim_id':x['id'],'statement':x['statement'],'factual':x['factual']} for x in risks]
        factual=[x for x in risks if x['factual']]
        verified_sources=[]
        if factual:
            try:verified_sources=collect([f'{candidate["company"]} {x["statement"]}' for x in factual],pol['limits']['max_sources_per_model'],source_errors(run_id,model_id))
            except Exception:emit(run_id,'source_warning','Независимый поиск недоступен; проверка помечается как неподтверждённая',model_id)
        data['verification_sources']=verified_sources
        result=invoke(run_id,'verifier',{'company':candidate['company'],'claims':claims,'sources':context_sources(verified_sources)},Verification,
            'Независимо проверь каждый claims по sources. Ты не видишь автора тезиса, его оценки, уверенности и вердикта. '
            'Не соглашайся автоматически: ищи опровержения и область применимости. Без достаточного источника — unknown. '
            'Для factual=false — judgment: прогноз не проверяется цитатой. supported/refuted/partial только с точной цитатой. '
            'Источник из поискового фрагмента недостаточен для supported. checks: ровно один на каждый claim_id. Даты не угадывай.',model_id,max_tokens=3500)
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
        data['verification']={'checks':validated}
        save_model(model_id,data,stage='scoring')
    # Scores reflect the predeclared rubric; risk checks are separate, never silently rewrite history.
    low,high,coverage=calculate(assessment['dimensions'],pol['weights'])
    for risk in data['critique']['risks']:
        risk['grounded']=grounded(risk,data['sources'])
        src=next((x for x in data['sources'] if x['id']==risk['source_id']),None)
        risk['source_url']=src['url'] if src else None
        risk['retrieved']=src['retrieved'] if src else None
        risk['source_date']=src.get('published') if src else None
    save_model(model_id,data,status='passed',stage='completed',score=low,score_upper=high,coverage=coverage)
    emit(run_id,'completed',f'Карточка готова: индекс {low:g}/100, покрытие {coverage:g}%',model_id)

def finish_run(run_id):
    with Session.begin() as s:
        r=s.get(Run,run_id)
        if not r or r.status=='cancelled':return
        jobs=s.query(Job).filter_by(run_id=run_id).all()
        if not jobs or any(j.status in ('queued','running') for j in jobs):return
        r.status='partial' if any(j.status=='failed' for j in jobs) else 'completed';r.updated=time.time()
        if r.status=='completed':r.error=''
