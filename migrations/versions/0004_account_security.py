"""Password rotation, administrator roles and supervised recovery requests."""
from alembic import op
import sqlalchemy as sa

revision = '0004_account_security'
down_revision = '0003_registration'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('registered_users', sa.Column('role', sa.String(16), nullable=False, server_default='member'))
    op.create_table('account_security', sa.Column('username', sa.String(64), primary_key=True),
                    sa.Column('bootstrap_version', sa.String(64), nullable=True),
                    sa.Column('password_hash', sa.String(256), nullable=True),
                    sa.Column('password_changed', sa.Float(), nullable=True))
    op.create_table('password_recovery_requests', sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('username', sa.String(64), nullable=False), sa.Column('contact', sa.String(200), nullable=False),
                    sa.Column('message', sa.String(500), nullable=False), sa.Column('status', sa.String(16), nullable=False),
                    sa.Column('created', sa.Float(), nullable=False), sa.Column('updated', sa.Float(), nullable=False),
                    sa.Column('handled_by', sa.String(64)), sa.Column('token_hash', sa.String(64), unique=True),
                    sa.Column('expires', sa.Float()), sa.Column('credential_version', sa.String(64)))
    op.create_index('ix_recovery_username_status', 'password_recovery_requests', ['username', 'status'])
    op.create_table('account_security_events', sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('actor', sa.String(64)), sa.Column('target', sa.String(64), nullable=False),
                    sa.Column('action', sa.String(32), nullable=False), sa.Column('created', sa.Float(), nullable=False),
                    sa.Column('request_id', sa.String(32)))


def downgrade():
    raise RuntimeError('Keep credential and recovery data; roll back only to a compatible application image.')
