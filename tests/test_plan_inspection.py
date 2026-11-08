"""Real public-data index through authenticated HTTP and real PostgreSQL identity/RLS."""
from pathlib import Path
from uuid import uuid4
import json,os,sqlite3
import pytest
from ledgervigil.ingestion import index_routes
from ledgervigil.ingestion.plan_index import IndexReader,IndexUnavailable,page_uuid,MANIFEST
from ledgervigil.evaluation.retail import file_hash
from tools.dev_db import connection
from test_source_registry import world,seed,api,client,signed_in,gbp,post,BASE  # noqa:F401
from test_proposal_drafts import fingerprint,counters,_ledger

@pytest.fixture(scope='session')
def indexed():
 from ledgervigil.ingestion.index_publication import INDEX_DIRECTORY,INDEX_MANIFEST_SHA256
 path=Path(os.environ.get('LV_PUBLIC_INDEX',Path(__file__).resolve().parents[2]/'compact-index'))
 if not path.exists():path=INDEX_DIRECTORY
 return IndexReader(path,INDEX_MANIFEST_SHA256)
@pytest.fixture
def opened(api,world,gbp,indexed):
 api.state.source_plan_index=indexed;a=signed_in(api,world,'analyst');r=post(a,gbp);assert r.status_code==200,r.text
 return a,r.json()['id']

def group_id(indexed,invoice='489435'):
 with indexed.connect() as c:return c.execute("SELECT group_id FROM groups WHERE json_extract(metadata,'$.source_identity[1]')=?",(invoice,)).fetchone()[0]


def test_real_hold_and_original_price_lexemes(opened,indexed,database):
 a,rid=opened;path=BASE+'/'+rid+'/groups';gid=group_id(indexed)
 before=fingerprint(database,{'lv_identity.audit_event','lv_identity.audit_chain_head'});ctr=counters(database)
 r=a.get(path+'/'+gid);assert r.status_code==200,r.text
 assert r.json()['source_identity']==['invoice','489435'] and r.json()['status']=='HOLD' and r.json()['amount'] is None and r.json()['source_line_count']==4
 assert a.get(path+'/'+gid,headers={'If-None-Match':r.headers['etag']}).status_code==304
 rows=a.get(path+'/'+gid+'/rows').json()['items'];assert len(rows)==4
 assert rows[0]['raw']['values'][5]=='2.5499999999999998' and type(rows[0]['raw']['values'][5]) is str
 assert rows[0]['row']==10 and rows[0]['sheet']=='Year 2009-2010'
 assert fingerprint(database,{'lv_identity.audit_event','lv_identity.audit_chain_head'})==before and counters(database)==ctr


def test_group_paging_filter_and_context_binding(opened,indexed,api,world,app,seed,database):
 a,rid=opened;path=BASE+'/'+rid+'/groups'
 first=a.get(path,params={'limit':'2','status':'HOLD'});assert first.status_code==200,first.text;p=first.json();assert len(p['items'])==2 and p['next_cursor'] and all(g['status']=='HOLD' for g in p['items'])
 second=a.get(path,params={'limit':'2','status':'HOLD','cursor':p['next_cursor']});assert second.status_code==200 and not {r['group_id'] for r in p['items']} & {r['group_id'] for r in second.json()['items']}
 assert a.get(path,params={'cursor':p['next_cursor'],'status':'CANDIDATE_REVIEW'}).status_code==400
 other=_ledger(app,database,seed,world['A'],currency='GBP')['ledger'];other_id=post(a,other).json()['id']
 assert a.get(BASE+'/'+other_id+'/groups',params={'status':'HOLD','cursor':p['next_cursor']}).status_code==400


def test_row_pages_cover_exactly_four_source_lines(opened,indexed):
 a,rid=opened;gid=group_id(indexed);path=BASE+'/'+rid+'/groups/'+gid+'/rows';seen=[];cursor=None
 for _ in range(4):
  r=a.get(path,params={'limit':'1',**({'cursor':cursor} if cursor else {})});assert r.status_code==200,r.text;p=r.json();seen.extend(p['items']);cursor=p['next_cursor']
 assert cursor is None and [row['row'] for row in seen]==[10,11,12,13] and len({row['sha256'] for row in seen})==4
 other=group_id(indexed,'489434');first=a.get(path,params={'limit':'1'}).json()
 assert a.get(BASE+'/'+rid+'/groups/'+other+'/rows',params={'cursor':first['next_cursor']}).status_code==400


def test_other_tenant_absent_ids_and_anonymous_never_reach_index(opened,indexed,api,world,monkeypatch):
 a,rid=opened;gid=group_id(indexed);b=signed_in(api,world,'analystB')
 def forbidden(*_):raise AssertionError('unauthorized index access')
 monkeypatch.setattr(indexed,'groups',forbidden);monkeypatch.setattr(indexed,'group',forbidden)
 for suffix in ['/groups','/groups/'+gid,'/groups/'+gid+'/rows']:
  assert client(api).get(BASE+'/'+rid+suffix).status_code==401
  assert b.get(BASE+'/'+rid+suffix).status_code==404
  assert b.get(BASE+'/'+str(uuid4())+suffix).status_code==404


def test_invalid_filters_unknown_queries_cursor_and_missing_groups(opened):
 a,rid=opened;path=BASE+'/'+rid+'/groups'
 assert a.get(path,params={'status':'READY'}).status_code==422
 assert a.get(path,params={'search':'ignored'}).status_code==400
 assert a.get(path,params=[('limit','1'),('limit','2')]).status_code==422
 assert a.get(path,params={'limit':'201'}).status_code==422
 assert a.get(path,params={'cursor':'invalid'}).status_code==400
 assert a.get(path+'/'+'0'*64).status_code==404
 assert a.get(path+'/'+'0'*64+'/rows').status_code==404


def test_missing_published_index_is_unavailable_not_empty(opened,api,monkeypatch):
 a,rid=opened;api.state.source_plan_index=None
 def missing():raise IndexUnavailable('unpublished')
 monkeypatch.setattr(index_routes,'published_index',missing)
 r=a.get(BASE+'/'+rid+'/groups');assert r.status_code==503 and r.json()['code']=='SOURCE_INDEX_UNAVAILABLE'


def test_revoked_read_permission_blocks_cached_index(opened,api,world,database):
 a,rid=opened;assert a.get(BASE+'/'+rid+'/groups').status_code==200
 with connection(database=database) as c:c.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['analyst']['id']))
 assert a.get(BASE+'/'+rid+'/groups').status_code==403


def test_http_database_boundary_is_read_only(opened,monkeypatch):
 a,rid=opened;original=index_routes.registration
 def check(ctx,pid):assert ctx.conn.execute('SHOW transaction_read_only').fetchone()==('on',);return original(ctx,pid)
 monkeypatch.setattr(index_routes,'registration',check);assert a.get(BASE+'/'+rid+'/groups').status_code==200


def test_index_reader_denies_writes_and_uses_range_indexes(indexed):
 with indexed.connect() as c:
  with pytest.raises(sqlite3.OperationalError):c.execute('DELETE FROM groups')
  query,args=indexed.group_query('HOLD',(0,'00000000-0000-0000-0000-000000000000'),3)
  plans=c.execute('EXPLAIN QUERY PLAN '+query,args).fetchall()
  assert any('SEARCH' in row[3] and 'group_status_page' in row[3] for row in plans)
  plans=c.execute('EXPLAIN QUERY PLAN SELECT ordinal,payload FROM rows WHERE group_ordinal=(SELECT ordinal FROM groups WHERE group_id=?) AND ordinal>? ORDER BY ordinal LIMIT 3',(group_id(indexed),0)).fetchall()
  assert any('SEARCH rows USING PRIMARY KEY' in row[3] for row in plans)


def test_unknown_manifest_anchor_and_mutated_file_are_refused(indexed,tmp_path):
 with pytest.raises(IndexUnavailable,match='MANIFEST_MISMATCH'):IndexReader(indexed.directory,'0'*64)
 # A changed fingerprint is rejected before opening SQLite, without modifying the real index.
 clone=object.__new__(IndexReader);clone.path=tmp_path/'changed';clone.path.write_bytes(b'bad');clone.identity=(0,0,0,0)
 with pytest.raises(IndexUnavailable,match='INDEX_CHANGED'):
  with clone.connect():pass


def test_raw_page_budget_preserves_a_resume_cursor():
 codec=index_routes.CursorCodec(b'x'*32);context={'test':True};records=[(n,page_uuid(str(n)),json.dumps({'raw':'é'*200000},ensure_ascii=False)) for n in (1,2,3)]
 values,cursor=index_routes.finish(records,3,codec,context);assert len(values)==2 and cursor and codec.decode(context,cursor,2)[0]=='2'


def test_interactive_reads_do_not_rehash_the_complete_index(opened,monkeypatch):
 from ledgervigil.ingestion import plan_index
 a,rid=opened
 def forbidden(*_):raise AssertionError('interactive full-file hashing')
 monkeypatch.setattr(plan_index,'file_hash',forbidden)
 assert a.get(BASE+'/'+rid+'/groups',params={'limit':'2'}).status_code==200


def test_index_builder_never_overwrites_existing_publication(tmp_path):
 from ledgervigil.ingestion.plan_index import build_index
 target=tmp_path/'accepted';target.mkdir();(target/'keep').write_text('immutable review')
 with pytest.raises(FileExistsError):build_index(tmp_path/'not-needed',target)
 assert (target/'keep').read_text()=='immutable review'
