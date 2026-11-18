"""Persistent demonstration sessions and shared login throttling."""
from alembic import op
import sqlalchemy as sa

revision = '0002_demo_access'
down_revision = '0001_runtime'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('demo_sessions', sa.Column('token_hash', sa.String(64), primary_key=True),
                    sa.Column('username', sa.String(64), nullable=False),
                    sa.Column('credential_version', sa.String(64), nullable=False), sa.Column('expires', sa.Float(), nullable=False))
    op.create_index('ix_demo_sessions_expires', 'demo_sessions', ['expires'])
    op.create_table('demo_login_limits', sa.Column('key_hash', sa.String(64), primary_key=True),
                    sa.Column('window_start', sa.Float(), nullable=False), sa.Column('attempts', sa.Integer(), nullable=False))
    op.create_index('ix_demo_login_limits_window_start', 'demo_login_limits', ['window_start'])


def downgrade():
    op.drop_table('demo_login_limits')
    op.drop_table('demo_sessions')
