"""Compare restored data against independent committed anchors, never against a copied DB claim."""
import json
from .integrity import digest,canonical,chain,ZERO,IntegrityError,financial_checkpoint

def check_database(conn,store):
 store.validate()
 heads=store.heads();head_by_tenant={str(h['tenant_id']):h for h in heads}
 # Compare both directions: a repository prefix alone cannot attest DB-recorded later anchors.
 packs=conn.execute("SELECT tenant_id::text,id::text,state,chain_seq,content_hash,previous_chain_head,chain_hash,anchor_commit,bundle_hash FROM lv.proof_pack WHERE state IN ('SEALED','ANCHORED','ANCHOR_FAILED') ORDER BY tenant_id,chain_seq").fetchall()
 for tenant,pid,state,seq,ch,prev,chain_hash,commit,bundle_hash in packs:
  if tenant not in head_by_tenant:raise IntegrityError('Database proof tenant is missing from the independent repository')
  if state=='ANCHORED':
   record,oid=store.find(tenant,pid)
   if seq>head_by_tenant[tenant]['chain_seq'] or (record.get('chain_seq'),record.get('content_hash'),record.get('previous_chain_head'),record.get('chain_hash'),record.get('bundle_hash'),oid)!=(seq,ch,prev,chain_hash,bundle_hash,commit):raise IntegrityError('Database anchor differs from committed repository history')
 records=[]
 for head in heads:
  tenant=head['tenant_id']
  if head is None:continue
  rows=conn.execute('SELECT id::text,content_canonical,content_hash,previous_chain_head,chain_hash,chain_seq FROM lv.proof_pack WHERE tenant_id=%s AND chain_seq<=%s ORDER BY chain_seq',(tenant,head['chain_seq'])).fetchall()
  prev=ZERO
  for number,(pid,text,ch,ph,h,seq) in enumerate(rows,1):
   content=json.loads(text)
   if seq!=number or ph!=prev or digest(canonical(content))!=ch or chain(ch,ph)!=h:raise IntegrityError('Restored proof chain diverges from anchor')
   prev=h;lid=content['ledger']['id'];snapshot,batch_head,event_head=financial_checkpoint(content);expected=(batch_head,event_head)
   row=conn.execute('SELECT b.hash,e.hash FROM lv.commit_batch b JOIN lv.ledger_event e USING(tenant_id,ledger_id,seq) WHERE b.tenant_id=%s AND b.ledger_id=%s AND b.seq=%s',(tenant,lid,int(snapshot))).fetchone()
   if int(snapshot)==0:row=('0'*64,'0'*64)
   if row!=expected:raise IntegrityError('Anchored financial history is missing or rewritten')
  if len(rows)!=head['chain_seq'] or prev!=head['chain_hash']:raise IntegrityError('Restored database is older than its external anchor')
  records.append({'tenant_id':tenant,'verified_packs':len(rows),'chain_hash':prev})
 if not records:raise IntegrityError('No external anchor records are available')
 return {'status':'ANCHORED_PREFIX_VERIFIED','tenants':records,'unanchored_tail':'NOT_ATTESTED','off_site_restore':'NOT_ATTESTED'}
