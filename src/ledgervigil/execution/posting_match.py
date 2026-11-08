"""E-7 exact single-entry posting comparison. Pure; never authorizes or writes.

The future commit service must independently derive observed lines from its NEW
versions, then call this inside its transaction. This module cannot establish
that provenance, approval, freshness, durability or all financial invariants.
"""
import hashlib
import json
import re
from dataclasses import dataclass

class PostingMismatch(ValueError):
    pass

@dataclass(frozen=True)
class PostingScope:
    tenant_id: str
    ledger_id: str
    proposal_id: str
    revision: int
    simulation_id: str
    currency: str
    exponent: int
    effective_date: str
    posting_rule: str
    posting_rule_version: str


def normalized(scope: PostingScope, lines: list[dict]) -> list[list]:
    if type(scope) is not PostingScope or type(scope.revision) is not int or scope.revision < 1 or type(scope.exponent) is not int or not 0 <= scope.exponent <= 4:
        raise PostingMismatch('INVALID_SCOPE')
    if any(type(v) is not str or not v for k,v in vars(scope).items() if k not in ('revision','exponent')):
        raise PostingMismatch('INVALID_SCOPE')
    if type(lines) is not list or not lines:
        raise PostingMismatch('EMPTY_OR_INVALID_POSTINGS')
    result=[];seen=set();balance=0
    for line in lines:
        if type(line) is not dict or set(line)!={'account_id','party_id','side','amount','currency'}:
            raise PostingMismatch('INVALID_LINE')
        account,party,side,amount,currency=(line[k] for k in ('account_id','party_id','side','amount','currency'))
        if type(account) is not str or not account or (party is not None and (type(party) is not str or not party)) or side not in ('D','C') or currency!=scope.currency or type(amount) is not str:
            raise PostingMismatch('INVALID_LINE')
        pattern=r'(?:0|[1-9][0-9]{0,15})'+(r'\.[0-9]{'+str(scope.exponent)+'}' if scope.exponent else '')
        if not re.fullmatch(pattern,amount):
            raise PostingMismatch('NON_EXACT_MONEY')
        units=int(amount.replace('.',''))
        if units<=0:raise PostingMismatch('ZERO_LINE')
        key=(account,party,side,currency)
        if key in seen:raise PostingMismatch('DUPLICATE_LINE')
        seen.add(key);balance+=units if side=='D' else -units
        result.append([account,party,side,amount,currency])
    if balance:raise PostingMismatch('UNBALANCED')
    return sorted(result,key=lambda x:(x[0],x[1] or '',x[2],x[4]))


def verify_postings(expected_scope: PostingScope, predicted: list[dict], observed_scope: PostingScope, derived: list[dict]) -> dict:
    """Return matching hashes or fail closed. No boolean success input is trusted."""
    expected=normalized(expected_scope,predicted);actual=normalized(observed_scope,derived)
    if expected_scope!=observed_scope:raise PostingMismatch('SCOPE_OR_RULE_CHANGED')
    def digest(lines):
        body=json.dumps({'format':'lv-native-posting-match/1','scope':vars(expected_scope),'lines':lines},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
        return hashlib.sha256(body).hexdigest()
    predicted_hash,derived_hash=digest(expected),digest(actual)
    if expected!=actual:raise PostingMismatch('DERIVED_POSTINGS_DIFFER')
    return {'format':'lv-native-posting-match/1','comparison':'MATCHED','predicted_hash':predicted_hash,'derived_hash':derived_hash,'line_count':len(expected),'accounting_status':'UNREVIEWED','authorizes_execution':False}
