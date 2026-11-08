"""Exercise an explicitly pinned historical authentication boundary in migration tests."""
from ledgervigil.auth.pool import ConnectionPool
from tools.dev_db import connection


def use_historical_issuer(application, database, revision):
    assert database.startswith('lv_migration_test_')
    with connection(database=database) as c:
        assert c.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == (revision,)
    application.state.issuer_pool = ConnectionPool(lambda: connection('lv_app', database), reuse=False)
