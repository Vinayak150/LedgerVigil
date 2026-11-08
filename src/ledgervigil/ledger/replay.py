"""Independent Python replay of the constrained synthetic event format.

No claim of proof-pack export, external chain anchoring or accounting approval.
"""
import hashlib
import json
from collections import defaultdict
from decimal import Decimal, localcontext

def canonical(value):
    def validate(item):
        if item is None or isinstance(item,str): return
        if isinstance(item,list):
            for v in item: validate(v)
            return
        if isinstance(item,dict):
            import re
            if any(not isinstance(k,str) or not re.fullmatch('[a-z_]+',k) for k in item):
                raise ValueError('unsupported canonical key')
            for v in item.values(): validate(v)
            return
        raise ValueError('only strings, null, objects and arrays are supported')
    validate(value)
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

PERIOD_CLOSE_FIELDS={'tenant_id','ledger_id','seq','kind','created_by_user','session_id','request_id','period_id','start_date',
                     'end_date','period_version_before','period_version_after','staleness_work_item_id'}

def replay_events(events):
    """Return chain head and (account,party) debit/credit totals; reject gaps/tampering.

    Since 0009 a ledger's chain may contain PERIOD_CLOSE events: they advance the sequence and the hash chain but carry no
    journal entry, so entry numbers count posting events only.
    """
    previous='0'*64
    totals=defaultdict(lambda:[Decimal(0),Decimal(0)])
    entry_ids=set();document_ids=set();scope=None;ledger_scope=None;closed_periods=[]
    with localcontext() as context:
        context.prec=80
        for expected_seq,event in enumerate(events,1):
            payload=json.loads(event['canonical_content'])
            if canonical(payload)!=event['canonical_content']:
                raise ValueError('noncanonical event')
            if ledger_scope is None: ledger_scope=(payload['tenant_id'],payload['ledger_id'])
            if (payload['tenant_id'],payload['ledger_id'])!=ledger_scope or event['seq']!=expected_seq or payload['seq']!=str(expected_seq):
                raise ValueError('sequence or scope mismatch')
            if payload.get('kind')=='PERIOD_CLOSE':
                if set(payload)!=PERIOD_CLOSE_FIELDS or payload['period_id'] in closed_periods:
                    raise ValueError('invalid period-close event')
                digest=hashlib.sha256((event['canonical_content']+previous).encode()).hexdigest()
                if event['previous_hash']!=previous or event['hash']!=digest:
                    raise ValueError('chain mismatch')
                closed_periods.append(payload['period_id']);previous=digest
                continue
            scope_value=(payload['tenant_id'],payload['ledger_id'],payload['financial_fields']['currency'])
            if scope is None: scope=scope_value
            if scope_value!=scope or payload['entry_no']!=str(len(entry_ids)+1):
                raise ValueError('sequence or scope mismatch')
            if payload['kind'] not in ('SEED','EXECUTION') or payload['provenance']!='SYNTHETIC' or payload['document_version']!='1':
                raise ValueError('unsupported event')
            if payload['kind']=='EXECUTION' and (not payload.get('created_by_user') or payload.get('created_by') is not None):
                raise ValueError('execution event lacks a human actor')
            if payload['entry_id'] in entry_ids or payload['document_id'] in document_ids:
                raise ValueError('duplicate event identity')
            entry_ids.add(payload['entry_id']);document_ids.add(payload['document_id'])
            digest=hashlib.sha256((event['canonical_content']+previous).encode()).hexdigest()
            if event['previous_hash']!=previous or event['hash']!=digest:
                raise ValueError('chain mismatch')
            debit=credit=Decimal(0)
            # Typed-document events carry derived postings separately from the document's financial fields.
            lines=(payload['postings'] if 'postings' in payload else payload['financial_fields'])['lines']
            if len(lines)<2: raise ValueError('too few journal lines')
            for index,line in enumerate(lines,1):
                import re
                if not isinstance(line['amount'],str) or not re.fullmatch(r'[0-9]{1,16}\.[0-9]{4}',line['amount']):
                    raise ValueError('invalid stored amount format')
                amount=Decimal(line['amount'])
                if not amount.is_finite() or amount<=0 or line['side'] not in ('D','C') or line['line_no']!=str(index):
                    raise ValueError('invalid event line')
                key=(line['account_id'],line['party_id'])
                side=0 if line['side']=='D' else 1
                totals[key][side]+=amount
                if side==0: debit+=amount
                else: credit+=amount
            if debit!=credit: raise ValueError('unbalanced event')
            previous=digest
    return {'head':previous,'entries':len(entry_ids),'balances':dict(totals),'closed_periods':closed_periods}
