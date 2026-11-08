"""RG-02 authority and commit-before-release, with real PostgreSQL."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import pytest
from tools.dev_db import connection
from test_proposal_drafts import world,seed,signed_in,fingerprint,counters
from test_simulation import api
from test_native_execution import approved,post
from test_native_approval import ready
from test_execution_proof import proof
from ledgervigil.exports import routes as proof_export

@pytest.mark.parametrize('role',['analyst','admin','analystB'])
def test_read_or_audit_read_alone_never_releases_proof(api,world,proof,role):
 *_,url,r=proof;c=signed_in(api,world,role)
 for headers in [{},{'If-None-Match':r.headers['X-Proof-SHA256']}]:
  response=c.get(url,headers=headers);assert response.status_code==403 and 'artifacts' not in response.json()


def test_every_release_and_304_has_digest_bound_audit(database,world,proof):
 c,_,_,url,original=proof
 with connection(database=database) as db:before=db.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='NATIVE_EXPORT_DOWNLOADED' AND tenant_id=%s",(world['A'],)).fetchone()[0]
 a=c.get(url);b=c.get(url,headers={'If-None-Match':original.headers['X-Proof-SHA256']});assert a.status_code==200 and b.status_code==200
 assert a.headers['X-Export-Authorization']!=b.headers['X-Export-Authorization']
 with connection(database=database) as db:
  rows=db.execute("SELECT detail,user_id::text FROM lv_identity.audit_event WHERE event_type='NATIVE_EXPORT_DOWNLOADED' AND tenant_id=%s ORDER BY seq",(world['A'],)).fetchall();assert len(rows)==before+2
  for response in [a,b]:
   detail,actor=next(row for row in rows if row[0]['release_id']==response.headers['X-Export-Authorization'])
   assert detail['content_hash']==original.headers['X-Proof-SHA256'] and detail['bytes']==str(len(original.content)) and actor==world['users']['controller']['id']

@pytest.mark.parametrize('change',['membership','role','session'])
def test_revocation_during_generation_releases_no_bytes(database,world,proof,monkeypatch,change):
 c,_,_,url,_=proof
 def revoke():
  with connection(database=database) as db:
   if change=='membership':db.execute("SELECT lv_identity.admin_revoke_membership(%s,%s)",(world['A'],world['users']['controller']['id']))
   elif change=='role':db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
   else:db.execute("UPDATE lv_identity.session SET revoked_at=clock_timestamp(),revoke_reason='ADMIN_REVOKED' WHERE user_id=%s AND revoked_at IS NULL",(world['users']['controller']['id'],))
 with monkeypatch.context() as m:
  m.setattr(proof_export,'_before_download',revoke);r=c.get(url);assert r.status_code==(403 if change=='role' else 401),r.text
 assert 'artifacts' not in r.json() and 'X-Proof-SHA256' not in r.headers


def test_failed_audit_prevents_release_and_rolls_back(database,world,proof):
 c,_,_,url,_=proof
 signature='lv_identity.audit_append(text,text,uuid,uuid,uuid,text,text,jsonb)'
 with connection(database=database) as db:original=db.execute('SELECT pg_get_functiondef(%s::regprocedure)',(signature,)).fetchone()[0]
 before=fingerprint(database,set());seq=counters(database)
 with connection(database=database) as db:db.execute("CREATE OR REPLACE FUNCTION lv_identity.audit_append(p_event text,p_outcome text,p_user uuid,p_tenant uuid,p_session uuid,p_source text,p_request text,p_detail jsonb) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN RETURN; END $$")
 try:r=c.get(url);assert r.status_code==500 and 'artifacts' not in r.json() and 'X-Proof-SHA256' not in r.headers
 finally:
  with connection(database=database) as db:db.execute(original)
 assert fingerprint(database,set())==before and counters(database)==seq


def test_twelve_concurrent_downloads_are_separately_audited_without_financial_effect(database,world,proof):
 c,_,_,url,r=proof;before=fingerprint(database,{'lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'});seq=counters(database)
 with ThreadPoolExecutor(12) as pool:rs=list(pool.map(lambda _:c.get(url),range(12)))
 assert all(response.status_code==200 and response.content==r.content for response in rs)
 ids={response.headers['X-Export-Authorization'] for response in rs};assert len(ids)==12
 with connection(database=database) as db:assert db.execute("SELECT count(*) FROM lv_identity.audit_event WHERE detail->>'release_id'=ANY(%s)",(list(ids),)).fetchone()==(12,)
 assert fingerprint(database,{'lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'})==before and counters(database)==seq
