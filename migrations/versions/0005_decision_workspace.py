"""Evidence tasks, pilot outcomes and per-account first-login instructions."""
from alembic import op
import sqlalchemy as sa

revision='0005_decision_workspace'
down_revision='0004_account_security'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('evidence_actions',sa.Column('model_id',sa.String(),sa.ForeignKey('models.id'),primary_key=True),
        sa.Column('task_key',sa.String(80),primary_key=True),sa.Column('status',sa.String(16),nullable=False),
        sa.Column('owner',sa.String(120),nullable=False),sa.Column('note',sa.Text(),nullable=False),
        sa.Column('source_urls',sa.Text(),nullable=False),sa.Column('version',sa.Integer(),nullable=False),sa.Column('updated',sa.Float(),nullable=False))
    op.create_table('pilots',sa.Column('model_id',sa.String(),sa.ForeignKey('models.id'),primary_key=True),
        sa.Column('data',sa.Text(),nullable=False),sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('created',sa.Float(),nullable=False),sa.Column('updated',sa.Float(),nullable=False))
    op.create_table('user_preferences',sa.Column('username',sa.String(64),primary_key=True),
        sa.Column('onboarding_enabled',sa.Integer(),nullable=False,server_default='1'),
        sa.Column('onboarding_seen_version',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('updated',sa.Float(),nullable=False))
    # Retain the gateway's existing restricted role; it still cannot read research.
    connection=op.get_bind()
    if connection.dialect.name=='postgresql':
        roles=connection.execute(sa.text("SELECT DISTINCT grantee FROM information_schema.role_table_grants WHERE table_schema=current_schema() AND table_name='demo_sessions' AND privilege_type='INSERT' AND grantee NOT IN (current_user,'PUBLIC')")).scalars()
        for role in roles:
            quoted=connection.dialect.identifier_preparer.quote(role)
            connection.execute(sa.text('GRANT SELECT,INSERT,UPDATE ON user_preferences TO '+quoted))


def downgrade():
    raise RuntimeError('Preserve experiments, evidence and account preferences; use a compatible rollback.')
