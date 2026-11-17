from alembic import context
from backend.db import engine
from sqlalchemy import text

def run(connection):
    def apply():
        if connection.dialect.name=='postgresql':connection.execute(text('SELECT pg_advisory_xact_lock(862319002)'))
        context.configure(connection=connection,transaction_per_migration=False)
        context.run_migrations()
    if connection.in_transaction():apply()
    else:
        with connection.begin():apply()

provided=context.config.attributes.get('connection')
if provided is not None:run(provided)
else:
    with engine.connect() as connection:run(connection)
