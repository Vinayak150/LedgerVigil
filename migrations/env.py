"""Migrations run as lv_owner through a separate restricted login."""
import os
from alembic import context
from sqlalchemy import create_engine, pool

url = context.config.attributes.get('connection_url') or os.environ['LEDGERVIGIL_MIGRATION_URL']
if context.is_offline_mode():
    raise RuntimeError('Offline SQL generation is not supported by this initial migration')
engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    with connection.begin():
        connection.exec_driver_sql('SET LOCAL ROLE lv_owner')
        current = connection.exec_driver_sql("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user").one()
        if current != ('lv_owner', False, False):
            raise RuntimeError('Migrations require restricted lv_owner role')
        context.configure(connection=connection, version_table_schema='lv_meta')
        with context.begin_transaction():
            context.run_migrations()
