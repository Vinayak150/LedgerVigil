"""Create a NEW, explicitly synthetic local SANDBOX; never touch BOOKS.

Three manual journals exercise posting primitives. They are not invoice/payment/
settlement document operations, policy verification or an approval workflow.
"""
from datetime import datetime
from pathlib import Path
from uuid import uuid7
import json
from tools.dev_db import ROOT,connection
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ledger.seed import seed_manual_journal
from ledgervigil.ledger.replay import replay_events

CHART=[('1000','BANK_OPERATING','ASSET',False,False),('1100','AR_CONTROL','ASSET',False,True),('1150','PROCESSOR_CLEARING','ASSET',False,False),('3000','OPENING_BALANCE_EQUITY','EQUITY',False,False),('4000','REVENUE_DEFAULT','REVENUE',False,False),('4900','SALES_RETURNS','REVENUE',True,False),('6100','PROCESSOR_FEE_EXPENSE','EXPENSE',False,False)]

def main():
    tenant=uuid7();ledger=uuid7();party=uuid7();chart={}
    with connection('lv_migrator') as owner:
        owner.execute('SET LOCAL ROLE lv_owner')
        owner.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenant),))
        owner.execute("INSERT INTO lv.tenant(tenant_id,name) VALUES(%s,'SYNTHETIC native posting demonstration')",(tenant,))
        owner.execute("INSERT INTO lv.ledger(tenant_id,id,name,purpose,source_kind,currency,currency_exponent) VALUES(%s,%s,'Sandbox — not accounting records','SANDBOX','NATIVE','INR',2)",(tenant,ledger))
        owner.execute("INSERT INTO lv.service_principal(tenant_id,name,purpose,database_role) VALUES(%s,'Local developer seed','DEVELOPER_SEED','lv_seed')",(tenant,))
        for code,role,kind,contra,needs_party in CHART:
            aid=uuid7();chart[role]=str(aid)
            owner.execute("INSERT INTO lv.account(tenant_id,ledger_id,id,code,name,type,is_contra,requires_party,currency,currency_exponent) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'INR',2)",(tenant,ledger,aid,code,role,kind,contra,needs_party))
            owner.execute('INSERT INTO lv.account_role_binding VALUES(%s,%s,%s,%s,%s,%s,%s,true)',(tenant,ledger,role,aid,kind,contra,needs_party))
        owner.execute("INSERT INTO lv.party(tenant_id,ledger_id,id,name,external_ref) VALUES(%s,%s,%s,'Synthetic customer','fixture-customer-1')",(tenant,ledger,party))
        owner.execute("INSERT INTO lv.fiscal_period(tenant_id,ledger_id,start_date,end_date) VALUES(%s,%s,'2026-10-01','2026-11-01')",(tenant,ledger))
    def line(role,side,amount):
        v={'account_id':chart[role],'side':side,'amount':amount}
        if role=='AR_CONTROL':v['party_id']=str(party)
        return v
    legs=[
        [line('AR_CONTROL','D','50000.00'),line('REVENUE_DEFAULT','C','50000.00')],
        [line('PROCESSOR_CLEARING','D','50000.00'),line('AR_CONTROL','C','50000.00')],
        [line('BANK_OPERATING','D','49000.00'),line('PROCESSOR_FEE_EXPENSE','D','1000.00'),line('PROCESSOR_CLEARING','C','50000.00')],
    ]
    bodies=[{'source_ref':f'primitive-demo-v1-{i}','effective_date':'2026-10-14','description':f'SYNTHETIC manual journal {i}; fixture native-primitives/1; not approved accounting records','lines':lines} for i,lines in enumerate(legs,1)]
    with connection('lv_seed',autocommit=True) as seed:
        results=[seed_manual_journal(seed,tenant,ledger,body) for body in bodies]
        retry=seed_manual_journal(seed,tenant,ledger,bodies[0])
    with connection('lv_app',autocommit=True) as reader:
        with tenant_transaction(reader,tenant):
            raw=reader.execute('SELECT seq,canonical_content,previous_hash,hash FROM lv.ledger_event WHERE ledger_id=%s ORDER BY seq',(ledger,)).fetchall()
            events=[dict(zip(('seq','canonical_content','previous_hash','hash'),r)) for r in raw]
            projection=reader.execute('SELECT account_id::text,party_id::text,debit_total,credit_total FROM lv.account_balance_current WHERE ledger_id=%s',(ledger,)).fetchall()
            balance_rows=reader.execute('SELECT a.code,a.name,coalesce(sum(p.debit_total-p.credit_total),0) FROM lv.account a LEFT JOIN lv.account_balance_current p ON (p.tenant_id,p.ledger_id,p.account_id)=(a.tenant_id,a.ledger_id,a.id) WHERE a.ledger_id=%s GROUP BY a.code,a.name ORDER BY a.code',(ledger,)).fetchall()
            counter=reader.execute('SELECT last_seq,last_entry_no,event_head FROM lv.ledger_counter WHERE ledger_id=%s',(ledger,)).fetchone()
    replay=replay_events(events)
    assert replay['balances']=={(a,p):[d,c] for a,p,d,c in projection}
    assert counter[0:2]==(3,3) and counter[2]==replay['head'] and retry['replayed']
    result={'fixture':'native-primitives/1','data':'SYNTHETIC','oracle_status':'UNREVIEWED','tenant_id':str(tenant),'ledger_id':str(ledger),'purpose':'SANDBOX','entries':results,'duplicate_retry':retry,'sequence':counter[0],'entry_count':counter[1],'event_chain_head':counter[2],'replay_matches_projection':True,'balances':[{'code':c,'role':r,'signed_amount':str(v)} for c,r,v in balance_rows],'limitations':['Manual journal primitives only; no invoice/payment/settlement workflow','No policy verdict, approval, BOOKS write, external chain anchor or QuickBooks integration']}
    dest=ROOT/'evidence/posting'/datetime.now().strftime('demo-%Y%m%d-%H%M%S');dest.mkdir()
    (dest/'result.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    (dest/'events.json').write_text(json.dumps(events,indent=2,ensure_ascii=False)+'\n')
    (dest/'requests.json').write_text(json.dumps(bodies,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'evidence':str(dest),'tenant_id':str(tenant),'ledger_id':str(ledger),'entries':3,'replay_matches_projection':True,'balances':result['balances']},indent=2))

if __name__=='__main__':main()
