"""Statement-to-ledger settlement reconciliation: the deterministic detector (PROPOSED DET-1, docs/AI_DETECTION.md).

Pure function over the frozen evidence set that the database builds (`lv.detection_inputs`). It compares a staged synthetic
processor statement (an OBSERVATION) with authoritative native ledger state and reports exact differences. It never infers
identity from amounts, dates or text, never estimates a probability, and never writes. All arithmetic uses the kernel's
exact `Money` at the ledger exponent. The database independently re-derives the same result (`lv.detection_expected`) and
refuses to record anything that differs, so this module cannot persist a finding on its own authority.
"""
import hashlib
from datetime import date

from ledger_kernel.money import Currency, Money

from ledgervigil.ingestion import statement_format as fmt
from ledgervigil.kernel_adapter import loader

DETECTOR = 'lv-settlement-statement-reconciliation'
VERSION = '1.1.0'
CONFIG = {'version': '1.1.0', 'inputs': 'lv-detection-inputs/2',
          'rules': ['SETTLEMENT_NOT_RECORDED', 'SETTLEMENT_AMOUNTS_DIFFER', 'SETTLEMENT_ITEMS_DIFFER', 'STATEMENT_ITEM_SETTLED_ELSEWHERE',
                    'STATEMENT_ITEM_AMOUNT_DIFFERS', 'STATEMENT_ITEM_UNRESOLVED', 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', 'PAYOUT_DATE_DIFFERS'],
          'date_comparison': 'statement payout date vs recorded settlement payout date; calendar dates as recorded; no timezone, business-day tolerance or cutoff',
          'overlap': 'same tenant, ledger and processor; distinct payout ids; both unrecorded; payment RECEIVED and unsettled; exact identity only',
          'identity': 'processor_ref+processor_item_id (statement processor namespace only)',
          'amount_comparison': 'exact decimal at the ledger exponent', 'max_statements': 200, 'model': 'none',
          'abstain': ['AMBIGUOUS_STATEMENT', 'REFUND_ITEMS_NOT_SUPPORTED', 'SOURCE_INTEGRITY_FAILED']}
KINDS = ('SETTLEMENT_NOT_RECORDED', 'SETTLEMENT_AMOUNTS_DIFFER', 'SETTLEMENT_ITEMS_DIFFER', 'STATEMENT_ITEM_SETTLED_ELSEWHERE',
         'STATEMENT_ITEM_AMOUNT_DIFFERS', 'STATEMENT_ITEM_UNRESOLVED', 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', 'PAYOUT_DATE_DIFFERS')
CLASSES = ('DISCREPANCY', 'EVIDENCE_GAP', 'POTENTIAL_CONFLICT')


def detect(inputs: dict) -> dict:
    """Return {'abstention': code | None, 'findings': [{kind, class, computed}, ...]} sorted by kind."""
    if inputs['conflicting_import_batches']:
        return {'abstention': 'AMBIGUOUS_STATEMENT', 'findings': []}
    if any(item['kind'] != 'PAYMENT' for item in inputs['items']):
        return {'abstention': 'REFUND_ITEMS_NOT_SUPPORTED', 'findings': []}
    currency = Currency(inputs['currency'], inputs['exponent'])
    money = lambda text: Money.parse(text, currency)
    zero = money('0')
    settlement = inputs['settlement']
    unresolved, differs, elsewhere, received, resolved = [], [], [], [], []
    received_total = unresolved_total = zero
    for item in sorted(inputs['items'], key=lambda i: i['line_no']):
        payment = item['payment']
        if len(item['links']) != 1 or payment is None:
            reason = ('AMBIGUOUS_LINK' if len(item['links']) > 1 else 'OTHER_NAMESPACE_ONLY' if item['other_namespaces'] else 'NO_IDENTITY_LINK')
            unresolved.append({'line_no': item['line_no'], 'item_ref': item['item_ref'], 'statement_amount': money(item['amount']).text(),
                               'reason': reason, 'other_namespaces': item['other_namespaces']})
            unresolved_total = unresolved_total + money(item['amount'])
            continue
        resolved.append(payment['document_id'])
        observed, ledger_amount = money(item['amount']), money(payment['amount'])
        if observed != ledger_amount:
            differs.append({'line_no': item['line_no'], 'item_ref': item['item_ref'], 'payment_document_id': payment['document_id'],
                            'payment_version': payment['version'], 'statement_amount': observed.text(), 'ledger_amount': ledger_amount.text(),
                            'difference': (observed - ledger_amount).text()})
        settled_in = payment['settlement_document_id']
        if settled_in is not None and (settlement is None or settled_in != settlement['document_id']):
            elsewhere.append({'line_no': item['line_no'], 'item_ref': item['item_ref'], 'payment_document_id': payment['document_id'],
                              'settled_in_document_id': settled_in})
        if payment['status'] == 'RECEIVED':
            received.append(payment['document_id'])
            received_total = received_total + ledger_amount
    findings = []
    if settlement is None:
        findings.append({'kind': 'SETTLEMENT_NOT_RECORDED', 'class': 'DISCREPANCY', 'computed': {
            'payout': [inputs['processor_ref'], inputs['payout_id']],
            'statement_gross': money(inputs['gross']).text(), 'statement_fee': money(inputs['fee']).text(),
            'statement_net': money(inputs['net']).text(), 'received_payment_document_ids': sorted(received),
            'received_total': received_total.text(), 'resolved_item_count': len(resolved), 'unresolved_item_count': len(unresolved)}})
    else:
        fields = [{'field': name, 'statement': money(inputs[name]).text(), 'ledger': money(settlement[name]).text(),
                   'difference': (money(inputs[name]) - money(settlement[name])).text()}
                  for name in ('gross', 'fee', 'net') if money(inputs[name]) != money(settlement[name])]
        if fields:
            findings.append({'kind': 'SETTLEMENT_AMOUNTS_DIFFER', 'class': 'DISCREPANCY', 'computed': {
                'settlement_document_id': settlement['document_id'], 'settlement_version': settlement['version'], 'fields': fields}})
        included = set(settlement['included_payment_document_ids'])
        missing, extra = sorted(set(resolved) - included), sorted(included - set(resolved))
        if missing or extra:
            findings.append({'kind': 'SETTLEMENT_ITEMS_DIFFER', 'class': 'DISCREPANCY', 'computed': {
                'settlement_document_id': settlement['document_id'], 'settlement_version': settlement['version'],
                'on_statement_not_in_settlement': missing, 'in_settlement_not_on_statement': extra}})
    if elsewhere:
        findings.append({'kind': 'STATEMENT_ITEM_SETTLED_ELSEWHERE', 'class': 'DISCREPANCY', 'computed': {'items': elsewhere}})
    if differs:
        findings.append({'kind': 'STATEMENT_ITEM_AMOUNT_DIFFERS', 'class': 'DISCREPANCY', 'computed': {'items': differs}})
    if unresolved:
        findings.append({'kind': 'STATEMENT_ITEM_UNRESOLVED', 'class': 'EVIDENCE_GAP',
                         'computed': {'items': unresolved, 'unresolved_total': unresolved_total.text()}})
    if inputs.get('schema') == 'lv-detection-inputs/2':
        findings += payout_date(inputs, settlement) + overlapping_claims(inputs, settlement, money)
    return {'abstention': None, 'findings': sorted(findings, key=lambda f: f['kind'])}


def payout_date(inputs, settlement):
    """PAYOUT_DATE_DIFFERS (PROPOSED DET-10): the statement's payout date against the recorded settlement's payout date, the only
    pair with the same documented meaning. Calendar dates exactly as recorded: no timezone, business-day tolerance or cutoff.
    A difference is reported as observed, never as an accounting error; ingestion time and posting dates are not compared."""
    if settlement is None or not inputs.get('payout_date') or not settlement.get('payout_date'):
        return []
    stated, recorded = date.fromisoformat(inputs['payout_date']), date.fromisoformat(settlement['payout_date'])
    if stated == recorded:
        return []
    return [{'kind': 'PAYOUT_DATE_DIFFERS', 'class': 'DISCREPANCY', 'computed': {
        'settlement_document_id': settlement['document_id'], 'settlement_version': settlement['version'],
        'statement_payout_date': inputs['payout_date'], 'ledger_payout_date': settlement['payout_date'],
        'calendar_days_statement_minus_ledger': (stated - recorded).days,
        'date_semantics': 'payout date as stated by the source and as recorded; calendar dates; no timezone, business-day or cutoff rule'}}]


def overlapping_claims(inputs, settlement, money):
    """PAYMENT_CLAIMED_BY_OTHER_PAYOUT (PROPOSED DET-9): this payout is unrecorded and one of its items resolves, by exact identity,
    to a RECEIVED unsettled payment that another staged statement for a DIFFERENT, also unrecorded payout of the same processor
    lists. Repeated imports of the same payout never reach here (ingestion keeps one statement per payout identity); a second
    file for the same payout is AMBIGUOUS_STATEMENT. Claims from ambiguous or unsupported statements, or from a payout already
    recorded, are listed as excluded and never raise the finding on their own."""
    if settlement is not None:
        return []
    claims = []
    for item in sorted(inputs['items'], key=lambda i: i['line_no']):
        payment = item['payment']
        if len(item['links']) != 1 or payment is None or payment['status'] != 'RECEIVED' or payment['settlement_document_id'] is not None:
            continue
        others = [c for c in inputs.get('other_claims', []) if c['item_ref'] == item['item_ref']]
        order = lambda c: (c['payout_id'], c['staged_document_id'])
        valid = [{'staged_document_id': c['staged_document_id'], 'payout': [inputs['processor_ref'], c['payout_id']], 'line_no': c['line_no'],
                  'statement_amount': c['statement_amount'], 'source_record_hash': c['source_record_hash']}
                 for c in sorted(others, key=order) if not (c['conflicting'] or c['unsupported'] or c['payout_recorded'])]
        excluded = [{'staged_document_id': c['staged_document_id'], 'payout': [inputs['processor_ref'], c['payout_id']], 'line_no': c['line_no'],
                     'reason': 'OTHER_PAYOUT_RECORDED' if c['payout_recorded'] else 'OTHER_STATEMENT_AMBIGUOUS' if c['conflicting'] else 'OTHER_STATEMENT_UNSUPPORTED'}
                    for c in sorted(others, key=order) if c['conflicting'] or c['unsupported'] or c['payout_recorded']]
        if valid:
            claims.append({'line_no': item['line_no'], 'item_ref': item['item_ref'], 'payment_document_id': payment['document_id'],
                           'payment_version': payment['version'], 'payment_amount': payment['amount'],
                           'statement_amount': money(item['amount']).text(), 'other_claims': valid, 'excluded_claims': excluded})
    if not claims:
        return []
    return [{'kind': 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', 'class': 'POTENTIAL_CONFLICT', 'computed': {
        'statement_payout': [inputs['processor_ref'], inputs['payout_id']], 'claims': claims,
        'basis': 'exact payment identity listed by two distinct, unrecorded payouts; not a conclusion about cause'}}]


def source_integrity(conn, tenant_id, ledger_id, inputs) -> str:
    """'VERIFIED' when the stored source bytes still hash to their SHA-256 and re-parse to the identical staged statement the
    evidence set describes (the kernel adapter's E-SRC-01 rule); otherwise 'FAILED', and the statement is abstained on."""
    ev, _ = loader._statement(conn, {'t': tenant_id, 'l': ledger_id}, (inputs['processor_ref'], inputs['payout_id']))
    if ev is None or ev.staged_document_id != inputs['staged_document_id']:
        return 'FAILED'
    parsed = fmt.parse(ev.source_file_content, tenant_id=str(tenant_id), ledger_id=str(ledger_id),
                       currency=Currency(inputs['currency'], inputs['exponent']), declared_format=ev.declared_format,
                       declared_version=ev.declared_version).parsed
    intact = (hashlib.sha256(ev.source_file_content).hexdigest() == ev.source_file_sha256 == inputs['source_file_sha256']
              and parsed is not None
              and (parsed.record_content, parsed.record_hash, parsed.financial_fingerprint) == (ev.record_content, ev.record_hash, ev.financial_fingerprint)
              and ev.record_hash == inputs['source_record_hash'] and ev.financial_fingerprint == inputs['financial_fingerprint']
              and (parsed.processor, parsed.payout, parsed.date, parsed.gross, parsed.fee, parsed.net, parsed.items)
              == (ev.processor, ev.payout, ev.payout_date, ev.gross, ev.fee, ev.net, tuple(tuple(i) for i in ev.items)))
    return 'VERIFIED' if intact else 'FAILED'


def run(conn, tenant_id, ledger_id, scope) -> list:
    """Evaluate every staged statement in scope, in scope order, inside the caller's locked transaction."""
    results = []
    for staged_id in scope:
        inputs, fingerprint = conn.execute('SELECT i, lv.detection_fingerprint(i) FROM lv.detection_inputs_v2(%s, %s, %s) i',
                                           (tenant_id, ledger_id, staged_id)).fetchone()
        integrity = source_integrity(conn, tenant_id, ledger_id, inputs)
        outcome = detect(inputs) if integrity == 'VERIFIED' else {'abstention': 'SOURCE_INTEGRITY_FAILED', 'findings': []}
        results.append({'staged_document_id': staged_id, 'input_fingerprint': fingerprint, 'integrity': integrity, **outcome})
    return results
