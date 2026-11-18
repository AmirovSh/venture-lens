"""Credential binding audit and cursor event lookup; existing data is retained."""
from alembic import op
import sqlalchemy as sa
revision='0007_connection_rotation'
down_revision='0006_product_controls'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('invocations',sa.Column('connection_version',sa.Integer(),nullable=True))
    op.create_index('ix_events_run_id_id','events',['run_id','id'])

def downgrade():
    raise RuntimeError('Preserve connection audit history; use a compatible rollback.')
