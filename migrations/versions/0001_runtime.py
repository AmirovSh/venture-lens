"""Adopt the original local schema and add recovery primitives."""
import json
from pathlib import Path
from alembic import op
import sqlalchemy as sa

revision='0001_runtime'
down_revision=None
branch_labels=None
depends_on=None

def upgrade():
    schema=json.loads((Path(__file__).parent.parent/'baseline.json').read_text())
    inspector=sa.inspect(op.get_bind())
    existing=set(inspector.get_table_names())
    for table in schema:
        def column(spec):
            kind={'string':sa.String,'text':sa.Text,'integer':sa.Integer,'float':sa.Float}[spec['type']]
            kwargs={'primary_key':spec['primary_key'],'nullable':spec['nullable']}
            if spec.get('default') is not None:kwargs['server_default']=sa.text(spec['default'])
            return sa.Column(spec['name'],kind(),**kwargs)
        if table['name'] not in existing:
            constraints=[sa.ForeignKeyConstraint([x['column']],[x['target']]) for x in table['foreign_keys']]+[sa.UniqueConstraint(*columns) for columns in table['unique']]
            op.create_table(table['name'],*[column(c) for c in table['columns']],*constraints)
        else:
            names={x['name'] for x in inspector.get_columns(table['name'])}
            for spec in table['columns']:
                if spec['name'] not in names:op.add_column(table['name'],column(spec))
        index_names={x['name'] for x in sa.inspect(op.get_bind()).get_indexes(table['name'])}
        for index in table['indexes']:
            if index['name'] not in index_names:op.create_index(index['name'],table['name'],index['columns'],unique=index['unique'])

def downgrade():
    raise RuntimeError('Destructive schema downgrade is disabled. Roll back the application image; restore data using a verified backup.')
