"""Pure evidence checks: ingested statement observations compared with the settlement request and the
kernel's predicted postings (no database or network access).

Every verdict is derived here from the staged statement read in the adapter's snapshot. Before any
comparison, that statement is re-derived from its stored source-file bytes, so each check traces to a
source record and file SHA-256. No caller-supplied "verified" flag exists or is accepted. Statement values
are OBSERVATIONS, kept apart from authoritative ledger facts.

Verdicts:
- PASS: agreement established from the evidence.
- FAIL: disagreement, ambiguity or integrity failure.
- INSUFFICIENT_EVIDENCE: no staged statement for this payout identity.
- UNRESOLVED: agreement cannot be established because a required link does not exist.
- NOT_ESTABLISHED: authoritative facts are absent, so the check cannot be evaluated.
"""
import hashlib
from collections import Counter

from ledger_kernel.money import Currency, DomainError, Money

from ledgervigil.ingestion import statement_format as fmt

ITEM_LINK_GAP = ('The requested items are legacy manual-journal fixtures (source_ref), not typed Payment documents, so a statement '
                 'item (e.g. "PMT-2001") cannot be linked to them. Request typed Payment document ids to resolve identity.')
DUP_GAP = ('Legacy manual-journal items cannot be included in an authoritative settlement, so duplicate settlement is not '
           'established for them. Request typed Payment document ids instead.')
DUP_SCOPE = ('PREVIEW only. Read from authoritative state committed at snapshot seq {seq} of this native SANDBOX ledger '
             '(payout identity, active inclusions, payment versions). It reserves nothing: a commit must re-check under the '
             'ledger counter lock (the developer seed path does), and normal execution would have to revalidate at execution '
             'time against the then-current state (not implemented). Provider-side duplicates are out of scope for native ledgers.')


def _check(id, verdict, reason, trace, **values):
    return {'id': id, 'verdict': verdict, 'reason': reason, 'trace': trace, **({'values': values} if values else {})}


def _money(text, currency):
    return Money.parse(text, currency)


def evaluate(snap, request, kernel_snapshot, payload) -> tuple[dict | None, list]:
    """Return (statement observation or None, evidence checks)."""
    currency = kernel_snapshot.currency
    ev = snap.statement
    checks = []
    if ev is None:
        reason = 'No VALID staged statement exists in this ledger for the requested processor/payout identity.'
        for id in ('E-EVD-01', 'E-SRC-01', 'E-CUR-01', 'E-PAY-01', 'E-AMT-GROSS', 'E-AMT-FEE', 'E-AMT-NET', 'E-FEE-POST', 'E-ITEM-01'):
            checks.append(_check(id, 'INSUFFICIENT_EVIDENCE', reason, None))
        checks.append(duplicate_check(snap, request))
        return None, checks

    trace = {'staged_document_id': ev.staged_document_id, 'source_record_id': ev.source_record_id,
             'import_batch_id': ev.import_batch_id, 'source_file_id': ev.source_file_id, 'source_file_sha256': ev.source_file_sha256,
             'source_record_hash': ev.record_hash, 'format': f'{ev.declared_format}/{ev.declared_version}'}

    # E-EVD-01: exactly one unambiguous statement for this payout identity.
    if snap.statement_conflicts:
        checks.append(_check('E-EVD-01', 'FAIL', 'Ambiguous evidence: other source files claimed this payout identity with different content.',
                             trace, conflicting_files=[c.source_file_sha256 for c in snap.statement_conflicts]))
    else:
        checks.append(_check('E-EVD-01', 'PASS', 'One staged statement for this processor/payout identity, ingested from a source file.', trace))

    # E-SRC-01: the stored bytes still hash to the recorded SHA-256 and re-parse to the same staged observation.
    reparsed = fmt.parse(ev.source_file_content, tenant_id=snap.tenant_id, ledger_id=snap.ledger_id,
                         currency=Currency(snap.currency, snap.exponent), declared_format=ev.declared_format, declared_version=ev.declared_version)
    p = reparsed.parsed
    intact = (hashlib.sha256(ev.source_file_content).hexdigest() == ev.source_file_sha256 and p is not None
              and (p.record_content, p.record_hash, p.financial_fingerprint) == (ev.record_content, ev.record_hash, ev.financial_fingerprint)
              and (p.processor, p.payout, p.date, p.gross, p.fee, p.net, p.items)
              == (ev.processor, ev.payout, ev.payout_date, ev.gross, ev.fee, ev.net, tuple(tuple(i) for i in ev.items)))
    checks.append(_check('E-SRC-01', 'PASS' if intact else 'FAIL',
                         'Source file bytes match their SHA-256 and re-parse to the identical staged statement.', trace))

    same_currency = (ev.currency, ev.exponent) == (currency.code, currency.exponent)
    checks.append(_check('E-CUR-01', 'PASS' if same_currency else 'FAIL', 'Statement currency equals the ledger functional currency.',
                         trace, statement=f'{ev.currency}/{ev.exponent}', ledger=f'{currency.code}/{currency.exponent}'))
    same_payout = (ev.processor, ev.payout, ev.payout_date) == (request['processor'], request['payout'], request['date'])
    checks.append(_check('E-PAY-01', 'PASS' if same_payout else 'FAIL', 'Statement processor, payout id and payout date equal the settlement request.',
                         trace, statement=[ev.processor, ev.payout, ev.payout_date], request=[request['processor'], request['payout'], request['date']]))
    if not same_currency:
        for id in ('E-AMT-GROSS', 'E-AMT-FEE', 'E-AMT-NET', 'E-FEE-POST', 'E-ITEM-01'):
            checks.append(_check(id, 'FAIL', 'Amounts are not comparable across currencies.', trace))
    else:
        try:
            s_amount = {k: _money(getattr(ev, k), currency) for k in ('gross', 'fee', 'net')}
            r_amount = {k: _money(request[k], currency) for k in ('gross', 'fee', 'net')}
        except DomainError as error:
            s_amount = r_amount = None
            for id in ('E-AMT-GROSS', 'E-AMT-FEE', 'E-AMT-NET', 'E-FEE-POST'):
                checks.append(_check(id, 'FAIL', f'Amount not exact at the ledger exponent ({error.code}).', trace))
        if s_amount:
            for k in ('gross', 'fee', 'net'):
                checks.append(_check(f'E-AMT-{k.upper()}', 'PASS' if s_amount[k] == r_amount[k] else 'FAIL',
                                     f'Statement {k} equals the settlement {k}.', trace, statement=s_amount[k].text(), request=r_amount[k].text()))
            fee_role = [l for l in payload['entry'] if kernel_snapshot.by_id(l['account']).role == 'FEE']
            posted_fee = _money(fee_role[0]['amount']['amount'], currency) if fee_role else Money.parse('0', currency)
            statement_fee = s_amount['gross'] - s_amount['net']
            checks.append(_check('E-FEE-POST', 'PASS' if posted_fee == statement_fee else 'FAIL',
                                 'Predicted fee posting equals statement gross minus statement net (P-FEE-01 rule; DC-18: fee from the statement).',
                                 trace, statement_gross_minus_net=statement_fee.text(), predicted_fee_posting=posted_fee.text()))
        if 'document_id' in request['items'][0]:
            checks.append(_typed_item_check(snap, request, ev, currency, trace))
        else:
            checks.append(_legacy_item_check(ev, kernel_snapshot, currency, trace))
    checks.append(duplicate_check(snap, request))
    observation = {'label': 'OBSERVATION from a staged synthetic source record; not an authoritative ledger fact',
                   'provenance': ev.provenance, 'processor': ev.processor, 'payout': ev.payout, 'date': ev.payout_date,
                   'currency': ev.currency, 'gross': ev.gross, 'fee': ev.fee, 'net': ev.net,
                   'items': [[ref, kind, amount] for _, ref, kind, amount in ev.items], 'trace': trace}
    return observation, checks


def duplicate_check(snap, request):
    """E-DUP-01 from authoritative native state in the adapter's snapshot; caller assertions are never consulted.

    PASS: the requested payout identity is unused in this ledger, and every requested payment is RECEIVED with no active
    inclusion. FAIL: the payout identity already has a recorded settlement, or a requested payment is SETTLED / actively
    included (I-08). Legacy manual-journal requests: NOT_ESTABLISHED.
    """
    trace = {'snapshot_seq': str(snap.seq), 'batch_head': snap.batch_head, 'isolation': snap.isolation,
             'sources': ['lv.processor_settlement_identity', 'lv.settlement_current', 'lv.settlement_inclusion_current', 'lv.payment_current']}
    if 'document_id' not in request['items'][0]:
        return _check('E-DUP-01', 'NOT_ESTABLISHED', DUP_GAP, trace)
    requested = [i['document_id'] for i in request['items']]
    payout = snap.payout_settlement
    included = [i for i in snap.active_inclusions if i.item_document_id in requested]
    included_ids = {i.item_document_id for i in included}
    settled = sorted(p.document_id for p in snap.payments if p.document_id in requested and p.status != 'RECEIVED' and p.document_id not in included_ids)
    values = {'payout_identity': [request['processor'], request['payout']],
              'recorded_settlement_for_payout': None if payout is None else {
                  'settlement_document_id': payout.document_id, 'version': str(payout.version), 'status': payout.status, 'seq': str(payout.seq),
                  'payout_date': payout.payout_date, 'gross': f'{payout.gross:.{snap.exponent}f}', 'fee': f'{payout.fee:.{snap.exponent}f}',
                  'net': f'{payout.net:.{snap.exponent}f}'},
              'requested_payments_in_active_settlements': [
                  {'payment_document_id': i.item_document_id, 'settlement_document_id': i.settlement_document_id,
                   'settlement_version': str(i.settlement_version), 'payout_identity': [i.processor_ref, i.payout_id], 'since_seq': str(i.last_seq)}
                  for i in included],
              'requested_payments_not_received_without_inclusion': settled,
              'scope': DUP_SCOPE.format(seq=snap.seq)}
    if payout is not None or included or settled:
        reasons = []
        if payout is not None:
            reasons.append(f'payout {request["processor"]}/{request["payout"]} already has a recorded settlement ({payout.document_id})')
        if included:
            reasons.append('requested payments are already in an active settlement: ' + ', '.join(
                f'{i.item_document_id} in {i.processor_ref}/{i.payout_id}' for i in included))
        if settled:
            reasons.append('requested payments are not RECEIVED: ' + ', '.join(settled))
        return _check('E-DUP-01', 'FAIL', 'Duplicate settlement: ' + '; '.join(reasons) + '.', trace, **values)
    return _check('E-DUP-01', 'PASS', f'At snapshot seq {snap.seq} the payout identity is unused in this ledger and no requested payment '
                  'is in an active settlement. Preview scope only (see values.scope).', trace, **values)


def _typed_item_check(snap, request, ev, currency, trace):
    """E-ITEM-01 (typed): resolve each statement item by exact identity, then compare currency, amount and state.

    Identity is the persisted (processor_ref, processor_item_id) pair, scoped to this tenant and ledger; the statement's
    processor is the namespace. Only an identity link can resolve an item: an equal amount never does. A missing link is
    UNRESOLVED. A resolved link whose currency, amount or state disagrees, a link to a payment that was not requested, a
    requested payment that the statement does not list, or any ambiguity is FAIL.
    """
    payments = {}
    for p in snap.payments:
        payments.setdefault(p.document_id, []).append(p)
    requested = [i['document_id'] for i in request['items']]
    per_item, failures, unresolved, linked = [], [], [], []
    refs = [ref for _, ref, _, _ in ev.items]
    duplicates = sorted({r for r in refs if refs.count(r) > 1})
    for line_no, ref, kind, amount in ev.items:
        row = {'statement_line': str(line_no), 'item_ref': ref, 'kind': kind, 'amount': amount}
        same_ns = [i for i in snap.statement_identities if i.processor_item_id == ref and i.processor_ref == ev.processor]
        other_ns = sorted(i.processor_ref for i in snap.statement_identities if i.processor_item_id == ref and i.processor_ref != ev.processor)
        if kind != 'PAYMENT':
            verdict, reason = 'FAIL', 'Refund items are not supported (no native refund document or inclusion model).'
        elif ref in duplicates or len(same_ns) > 1:
            verdict, reason = 'FAIL', 'Ambiguous: the item identity occurs more than once.'
        elif not same_ns:
            verdict = 'UNRESOLVED'
            reason = f'No typed Payment carries identity {ev.processor}/{ref} in this tenant and ledger.'
            if other_ns:
                reason += f' The item id exists only under another processor namespace ({", ".join(other_ns)}); namespaces are not interchangeable.'
                row['other_namespaces'] = other_ns
        else:
            link = same_ns[0]
            found = payments.get(link.document_id, [])
            if len(found) != 1:
                verdict, reason = 'FAIL', 'The linked payment document has no single consistent current version, projection and posting in this snapshot.'
            else:
                pay = found[0]
                row['link'] = {'processor_ref': pay.processor_ref, 'processor_item_id': pay.processor_item_id, 'document_id': pay.document_id,
                               'document_type': 'PAYMENT', 'version': str(pay.version), 'document_version_id': pay.version_id,
                               'financial_fingerprint': pay.financial_fingerprint, 'posting_entry_id': pay.entry_id,
                               'identity_seq': str(pay.identity_seq), 'currency': pay.currency, 'amount': f'{pay.amount:.{pay.exponent}f}', 'status': pay.status}
                problems = []
                if (pay.currency, pay.exponent) != (ev.currency, ev.exponent):
                    problems.append(f'currency {ev.currency} vs payment {pay.currency}')
                elif _money(amount, currency) != Money(pay.amount, currency):
                    problems.append(f'amount {amount} vs payment {Money(pay.amount, currency).text()}')
                if pay.status != 'RECEIVED':
                    problems.append(f'payment status {pay.status}')
                if pay.document_id not in requested:
                    problems.append('linked payment is not among the requested settlement items')
                verdict = 'FAIL' if problems else 'PASS'
                reason = '; '.join(problems) if problems else 'Identity, currency and amount agree with the typed Payment version.'
                if not problems:
                    linked.append(pay.document_id)
        row['verdict'], row['reason'] = verdict, reason
        (failures if verdict == 'FAIL' else unresolved if verdict == 'UNRESOLVED' else []).append(ref)
        per_item.append(row)
    not_on_statement = [d for d in requested if d not in linked]
    values = {'statement_processor': ev.processor, 'items': per_item, 'requested_payment_documents': requested,
              'requested_not_matched_on_statement': not_on_statement}
    if failures:
        return _check('E-ITEM-01', 'FAIL', f'Statement items disagree with typed Payments: {", ".join(failures)}.', trace, **values)
    if unresolved:
        return _check('E-ITEM-01', 'UNRESOLVED', f'No identity link for statement items: {", ".join(unresolved)}. '
                      'Equal amounts are not used to establish identity.', trace, **values)
    if not_on_statement:
        return _check('E-ITEM-01', 'FAIL', 'Requested payments are not listed on the statement.', trace, **values)
    return _check('E-ITEM-01', 'PASS', 'Every statement item resolves by processor identity to exactly one requested typed Payment '
                  'version with equal currency and amount, and every requested payment is listed.', trace, **values)


def _legacy_item_check(ev, kernel_snapshot, currency, trace):
    """E-ITEM-01 (legacy manual-journal items): amounts can disagree, but agreement cannot prove identity."""
    statement_items = [(ref, kind, _money(amount, currency)) for _, ref, kind, amount in ev.items]
    ledger_amounts = Counter(i.amount.value for i in kernel_snapshot.items)
    refunds = [ref for ref, kind, _ in statement_items if kind == 'REFUND']
    observed = {'statement_items': [[ref, kind, m.text()] for ref, kind, m in statement_items],
                'requested_ledger_receipts': [[i.id, i.amount.text()] for i in kernel_snapshot.items]}
    if refunds:
        return _check('E-ITEM-01', 'FAIL', 'Statement includes refund items; ledger refund items are not supported by this adapter.', trace, **observed)
    if Counter(m.value for _, _, m in statement_items) != ledger_amounts:
        return _check('E-ITEM-01', 'FAIL', 'Statement item count or amounts disagree with the requested ledger receipts.', trace, **observed)
    return _check('E-ITEM-01', 'UNRESOLVED', 'Item amounts agree, but identity is not established. ' + ITEM_LINK_GAP, trace, **observed)
