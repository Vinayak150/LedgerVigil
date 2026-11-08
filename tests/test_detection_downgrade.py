"""0033/0032 downgrade: refused while any tenant holds a detection run (the validating CHECK ignores RLS); head and rows are kept.
Uses its own disposable database (detection_support.database), so it never touches the shared session database."""
import pytest

from tools.dev_db import connection, migrate
from test_proposal_drafts import world, seed, signed_in  # noqa: F401
from detection_support import database, dapi, run  # noqa: F401


def test_populated_downgrade_is_refused_for_a_run_in_another_tenant(database, dapi, world):
    # The run belongs to tenant B only; the migration role cannot see it, yet the downgrade must still be refused.
    assert run(signed_in(dapi, world, 'analystB'), world['lb']['ledger']).status_code == 201
    with pytest.raises(Exception) as refused:
        migrate(database, '0031_semantic_revalidation', downgrade=True, disposable=True)
    assert 'no_detection_v11_downgrade' in str(refused.value)   # 0033's guard is reached first (a 1.1.0 run exists)
    with connection(database=database) as db:
        assert db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0037_restore_interlock',)
        assert db.execute('SELECT count(*) FROM lv.detection_run WHERE tenant_id = %s', (world['B'],)).fetchone()[0] == 1
        assert db.execute("SELECT to_regclass('lv.detection_finding') IS NOT NULL").fetchone()[0]
