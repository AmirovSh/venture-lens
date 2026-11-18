"""Persistent self-service accounts and isolated research workspaces."""
from alembic import op
import sqlalchemy as sa

revision = '0003_registration'
down_revision = '0002_demo_access'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('registered_users',
                    sa.Column('username', sa.String(64), primary_key=True),
                    sa.Column('name', sa.String(60), nullable=False),
                    sa.Column('password_hash', sa.String(256), nullable=False),
                    sa.Column('workspace', sa.String(64), unique=True, nullable=False),
                    sa.Column('created', sa.Float(), nullable=False))
    op.add_column('runs', sa.Column('workspace', sa.String(64), nullable=False, server_default='main'))
    op.create_index('ix_runs_workspace_created', 'runs', ['workspace', 'created'])


def downgrade():
    raise RuntimeError('Keep account and workspace ownership data; roll back the application image instead.')
