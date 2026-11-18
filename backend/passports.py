"""One provenance format for facts, model judgments and independent checks."""
from datetime import datetime, timezone

VERSION = '3.0'


def enrich(item, sources, provenance, fallback_at, checking=False):
    source = next((source for source in sources if source['id'] == item.get('source_id')), None)
    method = item.get('method')
    if not method:
        method = 'model_judgment' if item.get('status') == 'judgment' else 'primary_source' if item.get('grounded') else 'human_required'
    generated = provenance.get('created') or fallback_at
    published = source.get('published') if source else None
    retrieved = source.get('retrieved') if source else None
    item.update(method=method, source_url=source['url'] if source else None, source_date=published, retrieved=retrieved)
    item['passport'] = {
        'version': VERSION, 'method': method, 'confidence': item.get('confidence', 'low'),
        'source_url': item['source_url'], 'publication_date': published, 'retrieved_at': retrieved,
        'generated_at': generated, 'generated_at_basis': 'invocation' if provenance.get('created') else 'research_created',
        'date': published or datetime.fromtimestamp(retrieved or generated, timezone.utc).isoformat(),
        'date_kind': 'publication' if published else 'retrieved' if retrieved else 'generated',
        'source_status': 'referenced' if source else 'not_applicable' if method in ('model_judgment', 'human_required') else 'missing',
        'source_note': 'Суждение модели; внешний источник не заявлен.' if not source and method == 'model_judgment'
                       else 'Требуется проверка человеком; внешний источник пока отсутствует.' if not source else '',
        'invocation_id': provenance.get('id'), 'model': provenance.get('model'), 'role': provenance.get('role'),
        'input_hash': provenance.get('input_hash'), 'grounded': bool(item.get('grounded')),
    }
    return item


def normalize_data(data, provenance, fallback_at):
    for role, items, sources in [
        ('screening', data.get('assessment', {}).get('claims', []), data.get('sources', [])),
        ('critic', data.get('critique', {}).get('risks', []), data.get('sources', [])),
        ('verifier', data.get('verification', {}).get('checks', []), data.get('verification_sources', [])),
    ]:
        for item in items:
            enrich(item, sources, provenance.get(role, {}), fallback_at, checking=role == 'verifier')
    return data


def provenance_for(session, model_id):
    from sqlalchemy import select
    from .db import Invocation
    rows = session.execute(select(Invocation.id, Invocation.role, Invocation.model, Invocation.created, Invocation.input_hash)
                           .where(Invocation.model_id == model_id, Invocation.status == 'completed').order_by(Invocation.created)).mappings()
    return {row['role']: dict(row) for row in rows}


def materialize(workspace='main'):
    from sqlalchemy import select
    from .db import Session, Run, Model, dump, read
    count = 0
    with Session.begin() as session:
        query = select(Run).where(Run.workspace == workspace)
        if session.bind.dialect.name == 'postgresql':query=query.with_for_update()
        for run in session.scalars(query):
            if run.status not in ('completed','completed_with_gaps','partial'):
                raise RuntimeError('Passport migration requires inactive research')
            for model in session.scalars(select(Model).where(Model.run_id == run.id)):
                data=normalize_data(read(model.data),provenance_for(session,model.id),run.created)
                model.data=dump(data);count+=1
    return count


if __name__=='__main__':
    import argparse,json
    parser=argparse.ArgumentParser();parser.add_argument('--workspace',default='main');args=parser.parse_args()
    print(json.dumps({'passport_cards_updated':materialize(args.workspace),'version':VERSION}))
