"""Recovery checks fail closed; real populated database replay and privilege tests."""
from uuid import uuid4
import pytest
from tools import verify_recovery as recovery
from tools.dev_db import connection
from test_proposal_drafts import world,seed  # noqa:F401


def test_restore_target_never_allows_live_or_arbitrary_database():
 for name in ['ledgervigil_dev','postgres','template1','production','lv_migration_test_bad']:
  with pytest.raises(ValueError):recovery.restore_target(name)
 recovery.restore_target('lv_migration_test_'+uuid4().hex)

@pytest.mark.parametrize('other',[{}, {'x':{'rows':2,'sha256':'a'}},{'x':{'rows':1,'sha256':'b'}}])
def test_data_difference_cannot_pass_recovery(other):
 with pytest.raises(ValueError):recovery.assert_match({'x':{'rows':1,'sha256':'a'}},other)


def test_existing_output_is_never_overwritten(tmp_path):
 target=tmp_path/'existing';target.mkdir();(target/'accepted').write_text('keep')
 with pytest.raises(FileExistsError):recovery.run('ledgervigil_dev',target)
 assert(target/'accepted').read_text()=='keep'


def test_populated_ledger_independently_replays(database,world):
 with connection(database=database) as c:
  result=recovery.replay(c);assert result['events']>0 and result['posting_entries']>0 and result['sequence_heads_and_balances_match']


def test_changed_balance_is_detected_by_replay(database,world):
 with connection(database=database) as c:
  c.execute('UPDATE lv.account_balance_current SET debit_total=debit_total+1 WHERE ledger_id=%s',(world['l1']['ledger'],))
  with pytest.raises(ValueError,match='BALANCE_REPLAY'):recovery.replay(c)
  c.rollback()


def test_recovered_security_probes_cover_real_tenants(database,world):
 result=recovery.security(database);assert result['tenant_contexts_available']>=2 and result['runtime_tenant_boundary']=='PASS'


def test_disabled_rls_is_detected(database,world):
 with connection(database=database,autocommit=True) as c:c.execute('ALTER TABLE lv.proposal NO FORCE ROW LEVEL SECURITY')
 try:
  with pytest.raises(ValueError,match='RLS_DISABLED'):recovery.security(database)
 finally:
  with connection(database=database) as c:c.execute('ALTER TABLE lv.proposal FORCE ROW LEVEL SECURITY')


def test_constraint_normalizer_preserves_allowed_values():
 a="CHECK (x = ANY ((ARRAY['ACTIVE'::character varying, 'REVOKED'::character varying])::text[]))"
 b="CHECK (x = ANY (ARRAY[('ACTIVE'::character varying)::text, ('REVOKED'::character varying)::text]))"
 assert recovery.normalize_constraint(a)==recovery.normalize_constraint(b)
 assert recovery.normalize_constraint(a)!=recovery.normalize_constraint(b.replace('REVOKED','DISABLED'))


def test_semantic_constraint_change_is_detected(database):
 with connection(database=database) as c:
  before=recovery.schema_digest(c);c.execute("ALTER TABLE lv.ledger ADD CONSTRAINT recovery_probe CHECK(name<>'not allowed')")
  assert before!=recovery.schema_digest(c);c.rollback()
