"""Independent input-shape and same-count reference-corruption regressions."""
from copy import deepcopy
import pytest
from psycopg import sql
from tools.dev_db import connection
from test_proposal_drafts import world, api, seed, signed_in, draft, create, fingerprint, proposal_rows

@pytest.mark.parametrize('pointer,value',[
 ('operation_type', []),('operation_type', {}),('payment_id', []),('payment_id', {}),
 ('evidence_kind', []),('evidence_kind', {}),
])
def test_wrong_json_field_types_are_validation_errors_not_server_errors(api,world,pointer,value):
    body=draft(world)
    if pointer=='operation_type':body['operations'][0]['type']=value
    elif pointer=='payment_id':body['operations'][0]['items'][0]['document_id']=value
    else:body['evidence_refs'][0]['kind']=value
    r=create(signed_in(api,world,'analyst'),body)
    assert r.status_code==422,r.text
    assert r.json()['code']=='SCHEMA_INVALID'


def test_same_count_reference_substitution_cannot_commit(database,api,world):
    """A wrong reference of the same kind/ledger must fail even when counts and FKs still agree."""
    before=proposal_rows(database,world)
    with connection(database=database) as c:
        c.execute(sql.SQL('''CREATE FUNCTION public.lv_test_redirect_reference() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF NEW.kind = 'SETTLEMENT_ITEM_PAYMENT' THEN NEW.document_id := {}::uuid; END IF; RETURN NEW; END $$''').format(sql.Literal(str(world['p2']))))
        c.execute('CREATE TRIGGER lv_test_redirect BEFORE INSERT ON lv.proposal_reference FOR EACH ROW EXECUTE FUNCTION public.lv_test_redirect_reference()')
    try:
        r=create(signed_in(api,world,'analyst'),draft(world))
        assert r.status_code==500,r.text
        assert proposal_rows(database,world)==before
    finally:
        with connection(database=database) as c:
            c.execute('DROP TRIGGER lv_test_redirect ON lv.proposal_reference; DROP FUNCTION public.lv_test_redirect_reference()')

@pytest.mark.parametrize('value',[{'amount':'1.00','currency':None},{'amount':None,'currency':'INR'},[],1,None])
def test_database_money_backstop_rejects_wrong_types_as_false(database,value):
    import json
    with connection(database=database) as c:
        result=c.execute('SELECT lv.proposal_money_ok(%s::jsonb,%s,%s::smallint)',(json.dumps(value),'INR',2)).fetchone()[0]
    assert result is False
