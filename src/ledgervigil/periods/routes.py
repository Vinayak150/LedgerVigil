"""Stage 2B Family 4 (subset): `GET /ledgers/{id}/periods` and `POST /periods/{id}/close`.

Close contract (Family 4 row; Part 2; Part 4; Part 8.1; Part 12):
- permission `administer` (2A §12.2 "administer (... period close)"); session tenant only; other-tenant ids are 404;
- `Idempotency-Key` required (400 IDEMPOTENCY_KEY_REQUIRED); same key + same request replays the stored response with
  `Idempotent-Replayed: true`; same key + different request is 422 IDEMPOTENCY_KEY_REUSED; a concurrent duplicate is
  409 REQUEST_IN_PROGRESS with Retry-After; 4xx outcomes are stored and replayed; a 5xx stores nothing;
- `If-Match: "v<version>"` required (428 VERSION_REQUIRED); mismatch 412 VERSION_MISMATCH with current_version;
- 409 INVALID_STATE_TRANSITION when the period is already closed or an earlier period is still open;
- 200 `Period` (CLOSED) with `ETag`, after the commit batch, ledger event, counter, period CAS, STALENESS_SCAN work item and
  outbox row, audit event and stored response have COMMITTED together.

The route commits BEFORE it builds the response (session_transaction used as a context manager), so a failure at commit
time is a 500, never a reported success.
"""
import logging
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ledgervigil.auth.boundary import RequestContext, require_read_only, session_transaction, source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.reads.ledgers import CURSOR, LIMIT, NOT_FOUND, READ_ERRORS, codec
from ledgervigil.reads.paging import check_query, list_context, page, parse_limit
from ledgervigil.reads.representation import render

log = logging.getLogger('ledgervigil.periods')
router = APIRouter(prefix='/api/v1', tags=['periods'])
IDEMPOTENCY_KEY = re.compile(r'^[!-~]{1,64}$')
PERIOD_DATE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')


class StalenessJob(BaseModel):
    id: str
    kind: str = Field(description='STALENESS_SCAN')
    state: str = Field(description='QUEUED when the close commits; no worker consumes it yet (see milestone limitations)')


class Period(BaseModel):
    id: str
    ledger_id: str
    start_date: str = Field(description='First day of the period (inclusive)')
    end_date: str = Field(description='First day AFTER the period (exclusive): the period is [start_date, end_date), as the schema '
                                      'constraint daterange(start_date, end_date, \'[)\') and the posting guards define it')
    status: str = Field(description='OPEN or CLOSED')
    version: int = Field(description='Resource version; ETag is "v<version>"')
    closed_at: str | None
    closed_by: str | None = Field(description='User id of the administrator whose session closed the period')
    close_seq: str | None = Field(description='Ledger sequence of the PERIOD_CLOSE commit')
    close_provenance: str | None = Field(description='AUDITED_COMMIT, or LEGACY_UNAUDITED for closes made before migration 0009')


class ClosedPeriod(Period):
    staleness_job: StalenessJob


class PeriodList(BaseModel):
    ledger_id: str
    items: list[Period]
    next_cursor: str | None


PROBLEM = {'content': {'application/problem+json': {}}}


@router.get('/ledgers/{ledger_id}/periods', response_model=PeriodList, operation_id='listLedgerPeriods',
            responses={**READ_ERRORS, **NOT_FOUND},
            openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'start_date, id'})
def list_periods(ledger_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                 limit: LIMIT = None, cursor: CURSOR = None):
    """Cursor-paginated (Stage 2B Part 2), ordered by start_date then id. Periods are half-open: [start_date, end_date)."""
    check_query(request, ('limit', 'cursor'))
    size = parse_limit(limit)
    # Row-level security limits every read to the session's tenant; another tenant's ledger is indistinguishable from none.
    if ctx.conn.execute('SELECT 1 FROM lv.ledger WHERE id = %s', (ledger_id,)).fetchone() is None:
        raise ProblemError(404, 'NOT_FOUND', 'ledger not found')
    context = list_context('listLedgerPeriods', ctx.tenant_id, ledger_id, 'start_date,id')
    after = codec(request).decode(context, cursor, 2) if cursor is not None else None
    if after is not None and not PERIOD_DATE.match(after[0]):
        raise ProblemError(400, 'CURSOR_MISMATCH', 'cursor is not valid for this request; restart from the first page',
                           errors=[{'pointer': '/query/cursor', 'code': 'invalid'}])
    where, params = ('AND (start_date, id) > (%s::date, %s::uuid)', after) if after else ('', [])
    rows = ctx.conn.execute(
        "SELECT id::text, ledger_id::text, start_date::text, end_date::text, status::text, version, "
        "to_char(closed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), closed_by::text, closed_seq::text, close_provenance "
        f"FROM lv.fiscal_period WHERE ledger_id = %s {where} ORDER BY start_date, id LIMIT %s", (ledger_id, *params, size + 1)).fetchall()
    fields = ('id', 'ledger_id', 'start_date', 'end_date', 'status', 'version', 'closed_at', 'closed_by', 'close_seq', 'close_provenance')
    items, next_cursor = page([dict(zip(fields, r)) for r in rows], size, codec(request), context, lambda x: [x['start_date'], x['id']])
    return render(request, PeriodList, {'ledger_id': str(ledger_id), 'items': items, 'next_cursor': next_cursor})


@router.post('/periods/{period_id}/close', response_model=ClosedPeriod, operation_id='closePeriod',
             responses={400: {'description': 'IDEMPOTENCY_KEY_REQUIRED', **PROBLEM},
                        401: {'description': 'UNAUTHENTICATED', **PROBLEM},
                        403: {'description': 'PERMISSION_DENIED (permission, CSRF or origin)', **PROBLEM},
                        404: {'description': 'NOT_FOUND (absent or another tenant)', **PROBLEM},
                        409: {'description': 'INVALID_STATE_TRANSITION (already closed, or an earlier period is still open); '
                                             'REQUEST_IN_PROGRESS (same Idempotency-Key still running)', **PROBLEM},
                        412: {'description': 'VERSION_MISMATCH (body has current_version)', **PROBLEM},
                        422: {'description': 'IDEMPOTENCY_KEY_REUSED; CAPABILITY_UNAVAILABLE (not a native SANDBOX ledger); SCHEMA_INVALID', **PROBLEM},
                        428: {'description': 'VERSION_REQUIRED (If-Match missing)', **PROBLEM}},
             openapi_extra={'x-lv-permission': 'administer', 'x-lv-idempotency': 'required', 'x-lv-jobs': ['STALENESS_SCAN']})
def close_period(period_id: UUID, request: Request,
                 idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=64, description=
                     'Required (Stage 2B Part 8.1). Absent: 400 IDEMPOTENCY_KEY_REQUIRED. 1-64 printable ASCII characters.')] = None,
                 if_match: Annotated[str | None, Header(alias='If-Match', max_length=64, description=
                     'Required: the period ETag, "v<version>". Absent: 428 VERSION_REQUIRED; different: 412 VERSION_MISMATCH.')] = None):
    if idempotency_key is None:
        raise ProblemError(400, 'IDEMPOTENCY_KEY_REQUIRED', 'POST /periods/{id}/close requires an Idempotency-Key header')
    if not IDEMPOTENCY_KEY.match(idempotency_key):
        raise ProblemError(422, 'SCHEMA_INVALID', 'Idempotency-Key must be 1-64 printable ASCII characters',
                           errors=[{'pointer': '/headers/Idempotency-Key', 'code': 'pattern'}])
    request_id = request.state.request_id
    with session_transaction(request, permission='administer') as ctx:
        result = ctx.conn.execute('SELECT lv.close_period(%s, %s, %s, %s, %s, %s)',
                                  (token_hash(ctx.session_token), period_id, if_match, idempotency_key,
                                   source_key(request), request_id)).fetchone()[0]
    # The transaction has COMMITTED here (or raised). Only now is the outcome reported.
    status = result['status']
    headers = {'Idempotent-Replayed': 'true'} if result.get('replayed') else {}
    log.info('period close request_id=%s user_id=%s status=%s replayed=%s', request_id, ctx.user_id, status, bool(result.get('replayed')))
    if status == 200:
        headers['ETag'] = f'"{result["etag"]}"'
        return JSONResponse(result['body'], status_code=200, headers=headers)
    if result.get('retry_after'):
        headers['Retry-After'] = str(result['retry_after'])
    raise ProblemError(status, result['code'], result.get('detail'), headers=headers,
                       extra={k: result[k] for k in ('current_version', 'state') if k in result})
