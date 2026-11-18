"""Versioned model settings and contextual page instructions."""
from alembic import op
import sqlalchemy as sa
revision='0006_product_controls'
down_revision='0005_decision_workspace'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('invocations',sa.Column('prompt_text',sa.Text(),nullable=True))
    op.create_table('service_configurations',sa.Column('version',sa.Integer(),primary_key=True),sa.Column('data',sa.Text(),nullable=False),sa.Column('created',sa.Float(),nullable=False),sa.Column('actor',sa.String(90),nullable=False))
    op.add_column('user_preferences',sa.Column('onboarding_seen_sections',sa.Text(),nullable=False,server_default='[]'))

def downgrade():
    raise RuntimeError('Preserve configuration history and instruction preferences; use a compatible rollback.')
