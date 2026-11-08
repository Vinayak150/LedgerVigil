"""List and query conventions shared by the native financial read routes (Stage 2B Part 2 and Part 4).

* Only the query parameters listed for an endpoint are accepted. Anything else is 400 `UNKNOWN_QUERY_PARAMETER`; a
  parameter given more than once is 422 `SCHEMA_INVALID` (it is not silently resolved to one of the values).
* `?limit=` is 1-200, default 50; anything else is 422 `SCHEMA_INVALID` with a field error.
* `?cursor=` is opaque and integrity protected: HMAC-SHA256 under the server cursor key over a context (operation, tenant,
  ledger, filters, order) plus the keyset position. A cursor that fails verification for ANY reason (malformed, tampered,
  from another operation, tenant, ledger, filter or order) is 400 `CURSOR_MISMATCH`, one indistinguishable answer.
  The context is MACed, not written into the cursor, so the cursor shows nothing about tenant or ledger.
* Ordering is always an allow-listed field plus `id` as a unique tie-breaker; pages are fetched with a keyset condition
  `(field, id) > (last field, last id)` and `LIMIT limit + 1`, so no page scans or returns more than limit + 1 rows.

These checks run after the session boundary (the route dependency), so an unauthenticated caller learns nothing from them.
"""
import base64
import binascii
import hashlib
import hmac
import json
import re
from uuid import UUID

from fastapi import Request

from ledgervigil.auth.problems import ProblemError

DEFAULT_LIMIT, MAX_LIMIT = 50, 200
MAX_CURSOR_LENGTH = 4096
CURSOR_VERSION = 1
_DOMAIN = b'ledgervigil/list-cursor/v1\n'
_LIMIT = re.compile(r'^[1-9][0-9]{0,2}$')


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _unb64(text: str) -> bytes:
    if not re.fullmatch(r'[A-Za-z0-9_-]+', text):
        raise ValueError('alphabet')
    decoded=base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    if _b64(decoded)!=text:
        raise ValueError('noncanonical encoding')
    return decoded


def check_query(request: Request, allowed: tuple[str, ...]):
    """Unknown parameters -> 400 UNKNOWN_QUERY_PARAMETER; a repeated parameter -> 422 SCHEMA_INVALID."""
    names = [k for k, _ in request.query_params.multi_items()]
    unknown = sorted({n for n in names if n not in allowed})
    if unknown:
        raise ProblemError(400, 'UNKNOWN_QUERY_PARAMETER', 'unknown query parameter(s); accepted: ' + (', '.join(allowed) or 'none'),
                           errors=[{'pointer': f'/query/{n[:64]}', 'code': 'unknown'} for n in unknown[:20]])
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise ProblemError(422, 'SCHEMA_INVALID', 'a query parameter was given more than once',
                           errors=[{'pointer': f'/query/{n}', 'code': 'repeated'} for n in repeated])


def parse_limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    if not _LIMIT.match(raw) or int(raw) > MAX_LIMIT:
        raise ProblemError(422, 'SCHEMA_INVALID', f'limit must be an integer from 1 to {MAX_LIMIT}',
                           errors=[{'pointer': '/query/limit', 'code': 'range'}])
    return int(raw)


class CursorCodec:
    """Signs and verifies keyset cursors bound to one list context."""

    def __init__(self, key: bytes):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError('cursor key must be at least 32 bytes')
        self._key = key

    def _mac(self, context: dict, payload: bytes) -> bytes:
        return hmac.new(self._key, _DOMAIN + _canonical(context) + b'\n' + payload, hashlib.sha256).digest()

    def encode(self, context: dict, position: list[str]) -> str:
        payload = _canonical({'v': CURSOR_VERSION, 'k': position})
        return _b64(payload) + '.' + _b64(self._mac(context, payload))

    def decode(self, context: dict, cursor: str, arity: int) -> list[str]:
        """The keyset position, or 400 CURSOR_MISMATCH. The last element is always a canonical UUID (the tie-breaker)."""
        try:
            if not cursor or len(cursor) > MAX_CURSOR_LENGTH or cursor.count('.') != 1:
                raise ValueError('shape')
            encoded_payload, encoded_mac = cursor.split('.')
            payload, mac = _unb64(encoded_payload), _unb64(encoded_mac)
            if not hmac.compare_digest(mac, self._mac(context, payload)):
                raise ValueError('mac')
            body = json.loads(payload)
            if set(body) != {'v', 'k'} or body['v'] != CURSOR_VERSION:
                raise ValueError('version')
            position = body['k']
            if type(position) is not list or len(position) != arity or any(type(p) is not str for p in position):
                raise ValueError('position')
            if str(UUID(position[-1])) != position[-1]:
                raise ValueError('tie-breaker')
            return position
        except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
            raise ProblemError(400, 'CURSOR_MISMATCH', 'cursor is not valid for this request; restart from the first page',
                               errors=[{'pointer': '/query/cursor', 'code': 'invalid'}]) from None


def list_context(operation: str, tenant_id: str, ledger_id: str | None, order: str, filters: dict | None = None) -> dict:
    """Everything a cursor is bound to. A cursor issued under one context never verifies under another."""
    return {'operation': operation, 'tenant': str(tenant_id), 'ledger': '' if ledger_id is None else str(ledger_id),
            'order': order, 'filters': filters or {}}


def page(rows: list, limit: int, codec: CursorCodec, context: dict, position_of) -> tuple[list, str | None]:
    """rows were fetched with LIMIT limit + 1. Returns the page and the next cursor (None on the last page)."""
    if len(rows) <= limit:
        return rows, None
    kept = rows[:limit]
    return kept, codec.encode(context, position_of(kept[-1]))
