"""Stage 2B Family 4 (read subset): native ledgers, chart of accounts and trial balance.

    GET /api/v1/ledgers                         read   page of Ledger
    GET /api/v1/ledgers/{ledger_id}             read   Ledger (purpose, state EMPTY/ACTIVE, current seq)
    GET /api/v1/ledgers/{ledger_id}/accounts    read   page of Account
    GET /api/v1/ledgers/{ledger_id}/trial-balance  read  ?as_of_seq&as_of_date -> {as_of_seq, lines, total_debit, total_credit}
    (GET /api/v1/ledgers/{ledger_id}/periods lives in ledgervigil.periods.routes and uses the same conventions.)

Every route runs inside the verified-session boundary with permission `read`, in a transaction that is switched to READ ONLY
after the session bookkeeping (auth.boundary.require_read_only). The tenant comes only from the session; row-level security
is the only tenant filter in these queries, so another tenant's ledger is exactly as absent as a missing one (404 NOT_FOUND).

Trial balance (sources: Stage 2A §8.3, §4.2, §7.1, I-04; Stage 2B Part 2 Money/SignedMoney):
* Balance(account, seq <= N) = sum of journal_line.signed_amount with seq <= N. `signed_amount` is the schema's generated
  column (amount for D, -amount for C); there is no balance_checkpoint table yet, so the checkpoint term is zero.
* Balances are signed debit-positive (2A §7.1). A line shows the net balance in exactly one column: debit = balance when
  positive, credit = -balance when negative, both 0.00 when zero. total_debit / total_credit are the column sums, which is
  how the 2A oracle rows state "TB" (e.g. OC-01 end: BANK 49,000.00 + FEE 1,000.00 = REV 50,000.00 = TB 50,000.00).
* Exact decimals only: amounts leave PostgreSQL as text and become the kernel's `Money` (Decimal, fixed context,
  quantised to the currency exponent, which refuses sub-minor-unit or out-of-range values); totals use the kernel's `total`.
* as_of_seq omitted = the counter head read at request time. The lines are ALWAYS bounded by `seq <= as_of_seq`, so the
  lines, totals and the reported as_of_seq describe one committed sequence even if commits land during the request.
  When the requested sequence is the head seen by the aggregate statement, the current-state projection
  (account_balance_current) must agree with the postings or the request fails closed (500, nothing reported).
* as_of_date is contracted but its inclusivity is not specified by any owning source: it is refused explicitly
  (422 CAPABILITY_UNAVAILABLE), never ignored (docs/LEDGER_READS.md, gap RG-1).
"""
import logging
from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ledger_kernel import money as kernel_money
from ledgervigil.auth.boundary import RequestContext, require_read_only
from ledgervigil.auth.problems import ProblemError

from .paging import CursorCodec, check_query, list_context, page, parse_limit
from .representation import render

log = logging.getLogger('ledgervigil.reads')
router = APIRouter(prefix='/api/v1', tags=['ledgers'])
SANDBOX_LABEL = 'SANDBOX — not accounting records'
ACCOUNTING_STATUS = 'UNREVIEWED'
MAX_TRIAL_BALANCE_LINES = 2000
_after_snapshot_resolved = None   # test seam only (like kernel_adapter.loader._after_counter_read); None in production

PROBLEM = {'content': {'application/problem+json': {}}}
READ_ERRORS = {304: {'description': 'Not Modified (If-None-Match matched the current ETag; only after authentication)'},
               400: {'description': 'UNKNOWN_QUERY_PARAMETER; CURSOR_MISMATCH', **PROBLEM},
               401: {'description': 'UNAUTHENTICATED', **PROBLEM},
               403: {'description': 'PERMISSION_DENIED (no read permission, or no tenant selected)', **PROBLEM},
               422: {'description': 'SCHEMA_INVALID (limit, repeated parameter, malformed id)', **PROBLEM}}
NOT_FOUND = {404: {'description': 'NOT_FOUND (absent or another tenant; indistinguishable)', **PROBLEM}}
LIMIT = Annotated[str | None, Query(pattern=r'^[1-9][0-9]{0,2}$', description='Page size 1-200, default 50')]
CURSOR = Annotated[str | None, Query(description='Opaque next_cursor from the previous page of the SAME request (at most 4096 '
                                                  'characters); any cursor that does not verify is 400 CURSOR_MISMATCH')]


# ------------------------------------------------------------------------------------------- schemas
class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Money(StrictModel):
    amount: str = Field(pattern=r'^[0-9]{1,16}(\.[0-9]{1,4})?$', description='Decimal string; scale = currency exponent')
    currency: str = Field(pattern=r'^[A-Z]{3}$')


class SignedMoney(StrictModel):
    amount: str = Field(pattern=r'^-?[0-9]{1,16}(\.[0-9]{1,4})?$', description='Signed decimal string, debit-positive')
    currency: str = Field(pattern=r'^[A-Z]{3}$')


class Ledger(StrictModel):
    id: str
    name: str
    purpose: str = Field(description='BOOKS or SANDBOX (ADR-024)')
    source: str = Field(description='NATIVE or PROVIDER_CONNECTED')
    functional_currency: str
    currency_exponent: int
    profile_version: str
    state: str = Field(description='EMPTY (sequence 0 and no documents, 2A §19.2) or ACTIVE')
    current_seq: int = Field(ge=0, description='Committed ledger sequence when this representation was read')
    created_at: str
    data_label: str | None = Field(description='"SANDBOX — not accounting records" for SANDBOX ledgers (ADR-024), else null')


class LedgerPage(StrictModel):
    items: list[Ledger]
    next_cursor: str | None


class Account(StrictModel):
    version: int = Field(ge=1, description='Database-managed resource version for If-Match')
    id: str
    ledger_id: str
    code: str
    name: str
    type: str = Field(description='ASSET, LIABILITY, EQUITY, REVENUE or EXPENSE')
    subtype: str | None
    is_contra: bool
    normal_side: str = Field(description='DEBIT for ASSET and EXPENSE, CREDIT otherwise, inverted when is_contra (2A §2.2)')
    requires_party: bool
    currency: str
    active: bool
    parent_id: str | None


class AccountPage(StrictModel):
    ledger_id: str
    items: list[Account]
    next_cursor: str | None


class TrialBalanceLine(StrictModel):
    account_id: str
    code: str
    name: str
    type: str
    is_contra: bool
    normal_side: str
    debit: Money = Field(description='Net debit balance, or 0')
    credit: Money = Field(description='Net credit balance, or 0')
    balance: SignedMoney = Field(description='Net balance, signed debit-positive (2A §7.1)')


class TrialBalance(StrictModel):
    ledger_id: str
    purpose: str
    data_label: str | None
    accounting_status: str = Field(description='UNREVIEWED: no credentialed accounting review exists (ADR-013)')
    as_of_seq: int = Field(ge=0, description='The committed sequence every line and total describes')
    currency: str
    lines: list[TrialBalanceLine] = Field(description='One line per account with at least one posting at or before as_of_seq, '
                                                      'ordered by code then id')
    total_debit: Money
    total_credit: Money


# ------------------------------------------------------------------------------------------- helpers
LEDGER_COLUMNS = (
    "l.id::text, l.name, l.purpose::text, l.source_kind::text, l.currency, l.currency_exponent, l.profile_version, "
    "to_char(l.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), coalesce(c.last_seq, 0), "
    "EXISTS (SELECT 1 FROM lv.document d WHERE d.tenant_id = l.tenant_id AND d.ledger_id = l.id)")
LEDGER_FROM = 'FROM lv.ledger l LEFT JOIN lv.ledger_counter c ON (c.tenant_id, c.ledger_id) = (l.tenant_id, l.id)'


def _ledger(row) -> dict:
    lid, name, purpose, source, currency, exponent, profile, created, seq, has_documents = row
    return {'id': lid, 'name': name, 'purpose': purpose, 'source': source, 'functional_currency': currency.strip(),
            'currency_exponent': exponent, 'profile_version': profile,
            'state': 'EMPTY' if seq == 0 and not has_documents else 'ACTIVE', 'current_seq': seq, 'created_at': created,
            'data_label': SANDBOX_LABEL if purpose == 'SANDBOX' else None}


def _read_ledger(ctx: RequestContext, ledger_id: UUID) -> dict:
    """One statement; row-level security is the tenant filter. Absent or another tenant's -> 404."""
    row = ctx.conn.execute(f'SELECT {LEDGER_COLUMNS} {LEDGER_FROM} WHERE l.id = %s', (ledger_id,)).fetchone()
    if row is None:
        raise ProblemError(404, 'NOT_FOUND', 'ledger not found')
    return _ledger(row)


def codec(request: Request) -> CursorCodec:
    return CursorCodec(request.app.state.auth_settings.cursor_key)


def _side(code: str) -> str:
    return {'D': 'DEBIT', 'C': 'CREDIT'}[code]


# ------------------------------------------------------------------------------------------- routes
@router.get('/ledgers', response_model=LedgerPage, operation_id='listLedgers', responses=READ_ERRORS,
            openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'name (bytewise), id'})
def list_ledgers(request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                 limit: LIMIT = None, cursor: CURSOR = None):
    check_query(request, ('limit', 'cursor'))
    size = parse_limit(limit)
    context = list_context('listLedgers', ctx.tenant_id, None, 'name:C,id')
    after = codec(request).decode(context, cursor, 2) if cursor is not None else None
    where, params = ('WHERE (l.name COLLATE "C", l.id) > (%s COLLATE "C", %s::uuid)', after) if after else ('', [])
    rows = ctx.conn.execute(f'SELECT {LEDGER_COLUMNS} {LEDGER_FROM} {where} ORDER BY l.name COLLATE "C", l.id LIMIT %s',
                            (*params, size + 1)).fetchall()
    items, next_cursor = page([_ledger(r) for r in rows], size, codec(request), context, lambda x: [x['name'], x['id']])
    return render(request, LedgerPage, {'items': items, 'next_cursor': next_cursor})


@router.get('/ledgers/{ledger_id}', response_model=Ledger, operation_id='getLedger', responses={**READ_ERRORS, **NOT_FOUND},
            openapi_extra={'x-lv-permission': 'read'})
def get_ledger(ledger_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    return render(request, Ledger, _read_ledger(ctx, ledger_id))


@router.get('/ledgers/{ledger_id}/accounts', response_model=AccountPage, operation_id='listLedgerAccounts',
            responses={**READ_ERRORS, **NOT_FOUND}, openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'code (bytewise), id'})
def list_accounts(ledger_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                  limit: LIMIT = None, cursor: CURSOR = None):
    check_query(request, ('limit', 'cursor'))
    size = parse_limit(limit)
    _read_ledger(ctx, ledger_id)
    context = list_context('listLedgerAccounts', ctx.tenant_id, ledger_id, 'code:C,id')
    after = codec(request).decode(context, cursor, 2) if cursor is not None else None
    where, params = ('AND (code COLLATE "C", id) > (%s COLLATE "C", %s::uuid)', after) if after else ('', [])
    rows = ctx.conn.execute(
        'SELECT id::text, ledger_id::text, code, name, type::text, subtype, is_contra, normal_side, requires_party, currency, active, '
        f'parent_id::text, version FROM lv.account WHERE ledger_id = %s {where} ORDER BY code COLLATE "C", id LIMIT %s',
        (ledger_id, *params, size + 1)).fetchall()
    fields = ('id', 'ledger_id', 'code', 'name', 'type', 'subtype', 'is_contra', 'normal_side', 'requires_party', 'currency',
              'active', 'parent_id', 'version')
    items = []
    for r in rows:
        item = dict(zip(fields, r))
        item['normal_side'], item['currency'] = _side(item['normal_side']), item['currency'].strip()
        items.append(item)
    items, next_cursor = page(items, size, codec(request), context, lambda x: [x['code'], x['id']])
    return render(request, AccountPage, {'ledger_id': str(ledger_id), 'items': items, 'next_cursor': next_cursor})


@router.get('/ledgers/{ledger_id}/trial-balance', response_model=TrialBalance, operation_id='getTrialBalance',
            responses={**READ_ERRORS, **NOT_FOUND,
                       422: {'description': 'SCHEMA_INVALID (as_of_seq malformed or beyond the committed sequence; as_of_date '
                                            'malformed); CAPABILITY_UNAVAILABLE (as_of_date semantics undecided; ledger is not '
                                            'native SANDBOX; more than 2000 lines; an amount outside the Money range)', **PROBLEM},
                       500: {'description': 'INTERNAL (postings and current-state projection disagree; nothing is reported)', **PROBLEM}},
            openapi_extra={'x-lv-permission': 'read', 'x-lv-accounting-status': ACCOUNTING_STATUS})
def trial_balance(ledger_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                  as_of_seq: Annotated[str | None, Query(pattern=r'^(0|[1-9][0-9]{0,17})$', description=
                      'Committed ledger sequence to report at; omitted = latest at request time. Beyond the head: 422.')] = None,
                  as_of_date: Annotated[str | None, Query(pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$', description=
                      'Contracted, NOT yet available: its date inclusivity is undecided (gap RG-1). A well-formed value is refused '
                      'with 422 CAPABILITY_UNAVAILABLE; it is never ignored.')] = None):
    check_query(request, ('as_of_seq', 'as_of_date'))
    ledger = _read_ledger(ctx, ledger_id)
    if (ledger['purpose'], ledger['source']) != ('SANDBOX', 'NATIVE'):
        raise ProblemError(422, 'CAPABILITY_UNAVAILABLE', 'trial balances are available for native SANDBOX ledgers only')
    if as_of_date is not None:
        try:
            date.fromisoformat(as_of_date)
        except ValueError:
            raise ProblemError(422, 'SCHEMA_INVALID', 'as_of_date must be a calendar date YYYY-MM-DD',
                               errors=[{'pointer': '/query/as_of_date', 'code': 'date'}]) from None
        raise ProblemError(422, 'CAPABILITY_UNAVAILABLE', 'as_of_date is not available yet: whether the date is inclusive is not '
                           'decided by the owning contracts (gap RG-1). Use as_of_seq; no current figures are substituted.')
    head = ledger['current_seq']
    seq = head if as_of_seq is None else int(as_of_seq)
    if seq > head:
        raise ProblemError(422, 'SCHEMA_INVALID', 'as_of_seq is beyond the ledger\'s committed sequence',
                           errors=[{'pointer': '/query/as_of_seq', 'code': 'beyond_committed_sequence'}])
    if _after_snapshot_resolved is not None:
        _after_snapshot_resolved()
    currency = kernel_money.Currency(ledger['functional_currency'], ledger['currency_exponent'])
    rows = ctx.conn.execute(
        'WITH head AS (SELECT coalesce((SELECT last_seq FROM lv.ledger_counter WHERE ledger_id = %(l)s), 0) AS seq), '
        'bal AS (SELECT account_id, sum(signed_amount) AS balance FROM lv.journal_line '
        '        WHERE ledger_id = %(l)s AND seq <= %(n)s GROUP BY account_id), '
        'proj AS (SELECT account_id, sum(debit_total - credit_total) AS balance FROM lv.account_balance_current '
        '         WHERE ledger_id = %(l)s GROUP BY account_id), '
        'chk AS (SELECT count(*) AS mismatches FROM bal FULL JOIN proj USING (account_id) '
        '        WHERE coalesce(bal.balance, 0) <> coalesce(proj.balance, 0)), '
        'acc AS (SELECT a.id, a.code, a.name, a.type, a.is_contra, a.normal_side, a.currency, a.currency_exponent, b.balance '
        '        FROM bal b JOIN lv.account a ON a.ledger_id = %(l)s AND a.id = b.account_id) '
        'SELECT head.seq, chk.mismatches, acc.id::text, acc.code, acc.name, acc.type::text, acc.is_contra, acc.normal_side, '
        '       acc.currency, acc.currency_exponent, acc.balance::text '
        'FROM head CROSS JOIN chk LEFT JOIN acc ON true ORDER BY acc.code COLLATE "C", acc.id LIMIT %(cap)s',
        {'l': ledger_id, 'n': seq, 'cap': MAX_TRIAL_BALANCE_LINES + 1}).fetchall()
    statement_head, mismatches = rows[0][0], rows[0][1]
    if statement_head == seq and mismatches:
        # The projection is current exactly at this sequence, so it must agree with the postings (2A §11.3).
        log.error('trial balance refused: projection disagrees with postings ledger_id=%s seq=%s request_id=%s',
                  ledger_id, seq, request.state.request_id)
        raise ProblemError(500, 'INTERNAL', 'ledger projection does not match its postings; no figures are reported')
    accounts = [r for r in rows if r[2] is not None]
    if len(accounts) > MAX_TRIAL_BALANCE_LINES:
        raise ProblemError(422, 'CAPABILITY_UNAVAILABLE', f'trial balance has more than {MAX_TRIAL_BALANCE_LINES} lines; '
                           'paging of trial balances is not contracted')
    lines, debits, credits = [], [], []
    try:
        for _, _, aid, code, name, kind, contra, normal, acc_currency, acc_exponent, text in accounts:
            if (acc_currency.strip(), acc_exponent) != (currency.code, currency.exponent):
                raise ProblemError(500, 'INTERNAL', 'account currency differs from the ledger currency')
            balance = kernel_money.Money(Decimal(text), currency)
            debit = balance if balance.value > 0 else kernel_money.zero(currency)
            credit = -balance if balance.value < 0 else kernel_money.zero(currency)
            debits.append(debit)
            credits.append(credit)
            lines.append({'account_id': aid, 'code': code, 'name': name, 'type': kind, 'is_contra': contra,
                          'normal_side': _side(normal), 'debit': debit.wire(), 'credit': credit.wire(), 'balance': balance.wire()})
        total_debit, total_credit = kernel_money.total(debits, currency), kernel_money.total(credits, currency)
    except kernel_money.DomainError as error:
        raise ProblemError(422, 'CAPABILITY_UNAVAILABLE', f'an amount cannot be represented as contract Money ({error.code})') from None
    if total_debit != total_credit:
        # I-04: debits equal credits at every sequence. A difference is corruption, not a figure to display.
        log.error('trial balance refused: unbalanced ledger_id=%s seq=%s request_id=%s', ledger_id, seq, request.state.request_id)
        raise ProblemError(500, 'INTERNAL', 'trial balance does not balance; no figures are reported')
    body = {'ledger_id': str(ledger_id), 'purpose': ledger['purpose'], 'data_label': ledger['data_label'],
            'accounting_status': ACCOUNTING_STATUS, 'as_of_seq': seq, 'currency': currency.code, 'lines': lines,
            'total_debit': total_debit.wire(), 'total_credit': total_credit.wire()}
    return render(request, TrialBalance, body)
