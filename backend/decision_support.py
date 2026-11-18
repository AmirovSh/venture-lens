"""Explain existing evidence and scores without inventing facts or admitting models."""
import hashlib
import re

CRITERION_ACTIONS={
    'legal_online':('Юрист по выбранной стране','Проверить требования к нашей предполагаемой деятельности до первого клиента.','Официальные требования регулятора или заключение специалиста с указанием юрисдикции.'),
    'recurring':('Основатель / аналитик','Подтвердить, за какое повторяемое действие клиент будет платить.','Тарифы, договор или описание регулярной подписки/комиссии.'),
    'no_financial_risk':('Юрист / специалист по платежам','Проверить движение денег и распределение финансовой ответственности.','Документ о том, кто кредитует, страхует и хранит средства клиентов.'),
    'no_trading_government':('Аналитик','Уточнить продукт и покупателя: исключить трейдинг и государственный сервис.','Описание продукта, целевых клиентов и сферы применения.'),
    'large_analogue':('Аналитик','Найти сопоставимый аналог с оценкой, капитализацией или стоимостью сделки от $300 млн.','Датированный источник с видом метрики, суммой и компанией; выручка и сумма инвестиций не заменяют оценку.'),
}


def business_conditions(settings):
    # The research title is a UI label, never a product requirement.
    return {key:settings[key] for key in ('region','budget','team','focus') if key in settings}


def tasks_for(data,policy):
    titles={item['id']:item['title'] for item in policy.get('criteria',[])}
    tasks=[]
    for gate in data.get('assessment',{}).get('gates',[]):
        if gate['status']!='review':continue
        who,action,proof=CRITERION_ACTIONS.get(gate['id'],('Аналитик','Уточнить обязательное условие.','Проверяемый источник.'))
        tasks.append({'key':'gate:'+gate['id'],'kind':'criterion','priority':100 if gate['id'] in ('legal_online','no_financial_risk') else 80,
            'title':titles.get(gate['id'],gate['id']),'reason':gate['reason'],'action':action,'proof':proof,'suggested_owner':who,'criterion_id':gate['id']})
    for unknown in data.get('assessment',{}).get('unknowns',[]):
        tasks.append({'key':'unknown:'+hashlib.sha256(unknown.encode()).hexdigest()[:16],'kind':'assumption','priority':40,
            'title':unknown,'reason':'Неизвестное из карточки; может повлиять на проверку спроса.','action':'Проверить это допущение и записать наблюдаемый результат.',
            'proof':'Источник, интервью или измерение с датой и описанием метода.','suggested_owner':'Ответственный за эксперимент','criterion_id':None})
    return sorted(tasks,key=lambda item:(-item['priority'],item['key']))


def main_risk(data):
    checks={item['claim_id']:item for item in data.get('verification',{}).get('checks',[])}
    risks=[risk for risk in data.get('critique',{}).get('risks',[]) if not (checks.get(risk['id'],{}).get('status')=='refuted' and checks.get(risk['id'],{}).get('grounded'))]
    def rank(risk):
        check=checks.get(risk['id'],{})
        validated=check.get('grounded') and check.get('status') in ('supported','partial')
        return (bool(validated),{'high':3,'medium':2,'low':1}.get(risk.get('impact'),0))
    if not risks:return None
    risk=max(risks,key=rank);check=checks.get(risk['id'],{})
    confirmed=bool(check.get('grounded') and check.get('status') in ('supported','partial'))
    return {'title':risk['title'],'statement':risk['statement'],'impact':risk['impact'],
        'basis':'Подтверждён источником' if confirmed and check['status']=='supported' else 'Подтверждён частично' if confirmed else 'Суждение / неподтверждённый риск',
        'confirmed':confirmed,'claim_id':risk['id']}


def model_support(model,data,conditions,policy,actions=None,pilot=None):
    assessment=data.get('assessment',{})
    brief=assessment.get('launch_brief') or data.get('launch_brief')
    if not brief:
        brief={'product':data.get('candidate',{}).get('description') or model.name,'first_customer':assessment.get('customer') or 'Сегмент пока не определён',
            'acquisition_channel':assessment.get('first_sale') or 'Канал ещё нужно определить','team':conditions.get('team','Не задана'),
            'budget_usd':None,'first_payment_days':None,'assumptions':assessment.get('unknowns',[]),'origin':'existing_card'}
    else:brief={**brief,'origin':'model_estimate'}
    tasks=tasks_for(data,policy)
    current_keys={task['key'] for task in tasks}
    archived_tasks=[{'key':key,**value} for key,value in (actions or {}).items() if key not in current_keys]
    for task in tasks:
        saved=(actions or {}).get(task['key'])
        task.update(saved or {'status':'open','owner':'','note':'','source_urls':[],'version':0,'updated':None})
    threshold=policy.get('decision_support',{}).get('minimum_priority_score',60)
    min_coverage=policy.get('decision_support',{}).get('minimum_evidence_coverage',70)
    admitted=model.status=='passed' and bool(assessment.get('gates')) and all(gate['status']=='pass' for gate in assessment['gates'])
    attractive=admitted and model.score is not None and model.score>=threshold and model.coverage>=min_coverage
    if model.status=='rejected':verdict='exclude';label='Исключить по обязательным условиям'
    elif model.status in ('queued','running','waiting'):verdict='processing';label='Дождаться завершения проверки'
    elif not admitted:verdict='evidence';label='Сначала закрыть пробелы'
    elif attractive:verdict='test';label='Рассмотреть проверку спроса'
    else:verdict='assumptions';label='Уточнить условия первого эксперимента'
    contributions=[{'id':item['id'],'title':policy.get('weights',{}).get(item['id'],{}).get('title',item['id']),
        'score':item['score'],'points':None if item['score'] is None else round(item['score']*20*policy.get('weights',{}).get(item['id'],{}).get('weight',0),2),'reason':item['reason']}
        for item in assessment.get('dimensions',[])]
    return {'brief':brief,'brief_provenance':data.get('launch_brief_provenance'),'tasks':tasks,'archived_tasks':archived_tasks,'open_tasks':sum(task['status']=='open' for task in tasks),
        'verdict':verdict,'verdict_label':label,'admitted':admitted,'attractive':attractive,'main_risk':main_risk(data),
        'contributions':contributions,'budget_cap_usd':conditions.get('budget'),'pilot':pilot,
        'revenue_model':assessment.get('revenue'),'analogue':assessment.get('analogue'),'thresholds':{'score':threshold,'coverage':min_coverage}}


STOPWORDS={'для','на','и','с','по','в','the','for','and','of','a','an','to','with','b2b','saas','сервис','платформа','software','platform','service'}


def hypothesis_name(original,company,brief):
    if re.sub(r'\W','',original.casefold())!=re.sub(r'\W','',company.casefold()):return original
    text=' '.join(brief.get('product','').split())
    if not text:return original
    if len(text)<=100:return text
    return text[:98].rsplit(' ',1)[0]+'…'


def hypothesis_signature(model):
    data=model['data'];a=data.get('assessment',{});brief=model['support']['brief']
    explicit=data.get('candidate',{}).get('business_model_key')
    text=explicit or ' '.join([brief.get('product',''),a.get('customer',''),a.get('revenue','')])
    return {word for word in re.findall(r'[\w]+',text.casefold()) if len(word)>2 and word not in STOPWORDS}


def research_summary(models,policy,active=False):
    passed=[m for m in models if m['support']['admitted']]
    qualified=sorted([m for m in passed if m['support']['attractive']],key=lambda m:-(m['score'] or 0))
    tasks=sorted([dict(task,model_id=m['id'],model_name=m['name']) for m in models if m['status']!='rejected' for task in m['support']['tasks'] if task['status']=='open'],key=lambda t:(-t['priority'],t['model_name']))
    if active:title='Исследование продолжается';text='Карточки и выводы обновляются по мере завершения этапов.'
    elif qualified:title='Есть варианты для проверки спроса';text='Фильтры пройдены, индекс и покрытие достигли заданных порогов. Следующий шаг — ограниченный эксперимент, решение остаётся за вами.'
    elif passed:title='Сначала уточните условия эксперимента';text='Есть допущенные модели, но ни одна не достигла одновременно порогов индекса и покрытия. Прохождение фильтров само по себе не означает, что стоит запускать бизнес.'
    elif models and all(m['status']=='rejected' for m in models):title='Нужен другой набор кандидатов';text='Текущие модели не соответствуют обязательным условиям. Причины сохранены в карточках.'
    elif models:title='Сначала закройте пробелы в доказательствах';text='Подтверждённого кандидата для проверки спроса пока нет. Начните с обязательных условий и существенных неопределённостей.'
    else:title='Задайте условия первого исследования';text='Выберите рынок, команду и бюджет. Сервис подготовит сравнимые гипотезы и основания для решения.'
    duplicates=[]
    signatures={m['id']:hypothesis_signature(m) for m in models}
    for i,left in enumerate(models):
        for right in models[i+1:]:
            a,b=signatures[left['id']],signatures[right['id']]
            similarity=len(a&b)/len(a|b) if a|b else 0
            if len(a&b)>=4 and similarity>=policy.get('decision_support',{}).get('duplicate_similarity',.72):
                duplicates.append({'model_ids':[left['id'],right['id']],'names':[left['name'],right['name']],'similarity':round(similarity,2)})
    return {'title':title,'explanation':text,'priorities':[m['id'] for m in qualified[:3]],
        'candidates_to_clarify':[m['id'] for m in sorted(passed,key=lambda m:-(m['score'] or 0))[:3]],
        'next_tasks':tasks[:3],'open_tasks':len(tasks),'possible_duplicates':duplicates[:12],
        'active_pilots':sum((m['support'].get('pilot') or {}).get('status')=='active' for m in models),
        'rule':'Фильтры + пороги из снимка policy.decision_support; не вероятность и не автоматическое решение.'}
