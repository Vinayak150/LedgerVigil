"""Read-only Intuit QuickBooks Online SANDBOX client (Stage 2C §2.3 rules; no write method exists).

Every provider fact used here and where it comes from (checked 7 Nov 2026; see docs/QUICKBOOKS_SANDBOX.md §Sources):
- OAuth endpoints: Intuit's sandbox OpenID discovery document (developer.api.intuit.com/.well-known/openid_sandbox_configuration).
- Request parameters (client_id, response_type=code, scope, redirect_uri, state; grant_type authorization_code/refresh_token
  with HTTP Basic client authentication; revoke with JSON {"token": ...}): Intuit's official oauth-pythonclient.
- Scope com.intuit.quickbooks.accounting only (Stage 2C §11 least privilege).
- Access tokens valid one hour; refresh tokens 100 days, rolling; always store the latest pair (Intuit blog, token management).
- Sandbox API host, 500 requests/minute per company, 10/second per company and app, 429 = wait 60 seconds (Stage 2C §3.1,
  Verified there). Query pagination STARTPOSITION/MAXRESULTS, at most 1000 per response (Intuit blog, queries).
- Minor version 75: requests below 75 are served as 75 since 1 Aug 2025 (Intuit blog, Accounting API changes).
- intuit_tid response header identifies a request for Intuit support (Intuit developer error-handling FAQ).
Behaviour not confirmed by documentation (exact Fault codes per status, whether ORDERBY Id is accepted for every entity) is
handled conservatively and must be confirmed against the live sandbox (docs/QUICKBOOKS_SANDBOX.md §Unverified).
"""
import base64
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote, urlencode, urlsplit

AUTHORIZATION_ENDPOINT = 'https://appcenter.intuit.com/connect/oauth2'
TOKEN_ENDPOINT = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
REVOCATION_ENDPOINT = 'https://developer.api.intuit.com/v2/oauth2/tokens/revoke'
API_BASE = 'https://sandbox-quickbooks.api.intuit.com'
PRODUCTION_API_HOST = 'quickbooks.api.intuit.com'
SCOPE = 'com.intuit.quickbooks.accounting'
MINOR_VERSION = '75'
PAGE_SIZE = 100
MAX_PAGES_PER_ENTITY = 20
SINGLETONS = ('Preferences', 'CompanyInfo')
LISTS = ('Account', 'Customer', 'Invoice', 'Payment', 'Deposit', 'JournalEntry', 'CreditMemo', 'RefundReceipt')
ENTITIES = SINGLETONS + LISTS
_ALLOWED = {('POST', 'oauth.platform.intuit.com', '/oauth2/v1/tokens/bearer'), ('POST', 'developer.api.intuit.com', '/v2/oauth2/tokens/revoke')}


class TransportRefused(Exception):
    """The guard refused a request: wrong host, production host, a write to the accounting API, or a redirect."""


class ProviderError(Exception):
    """A classified provider outcome. outcome: RETRYABLE | THROTTLED | TERMINAL | AUTH_INVALID."""

    def __init__(self, outcome, code, *, status=None, intuit_tid=None, fault_codes=()):
        super().__init__(code)
        self.outcome, self.code, self.status, self.intuit_tid, self.fault_codes = outcome, code, status, intuit_tid, tuple(fault_codes)


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes


class Guard:
    """Wraps any transport (real httpx or a recorded/mock one). Enforces the sandbox-only, read-only boundary in one place."""

    def __init__(self, transport):
        self.transport = transport
        self.log = []   # (method, host, path) only: never query strings, headers or bodies

    def request(self, method, url, *, headers=None, data=None, json_body=None, timeout=10.0) -> Response:
        u = urlsplit(url)
        if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
            raise TransportRefused('only HTTPS to Intuit hosts')
        if u.hostname == PRODUCTION_API_HOST:
            raise TransportRefused('production QuickBooks is disabled')
        if u.hostname == urlsplit(API_BASE).hostname:
            if method != 'GET' or not u.path.startswith('/v3/company/'):
                raise TransportRefused('QuickBooks write capabilities are disabled')
        elif (method, u.hostname, u.path) not in _ALLOWED:
            raise TransportRefused('host or path not allowed')
        self.log.append((method, u.hostname, u.path))
        r = self.transport(method, url, headers=headers or {}, data=data, json_body=json_body, timeout=timeout)
        if 300 <= r.status < 400:
            raise TransportRefused('redirects are not followed')
        return r


def httpx_transport():
    """The real transport: no redirects, no hidden retries (Stage 2C §2.3 rule 4), bounded time."""
    import httpx
    client = httpx.Client(follow_redirects=False, timeout=httpx.Timeout(10.0, connect=5.0), transport=httpx.HTTPTransport(retries=0))

    def send(method, url, *, headers, data, json_body, timeout):
        try:
            r = client.request(method, url, headers=headers, data=data, json=json_body, timeout=timeout)
        except httpx.TimeoutException:
            raise ProviderError('RETRYABLE', 'PROVIDER_TIMEOUT') from None
        except httpx.HTTPError:
            raise ProviderError('RETRYABLE', 'PROVIDER_UNREACHABLE') from None
        return Response(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content)
    return send


def authorization_url(client_id, redirect_uri, state):
    return AUTHORIZATION_ENDPOINT + '?' + urlencode({'client_id': client_id, 'response_type': 'code', 'scope': SCOPE,
                                                     'redirect_uri': redirect_uri, 'state': state}, quote_via=quote)


def _basic(client_id, client_secret):
    return 'Basic ' + base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()


def _json(body: bytes):
    try:
        return json.loads(body.decode('utf-8'), parse_float=Decimal)
    except (UnicodeDecodeError, ValueError):
        return None


def _token_result(r: Response, *, exchange: bool):
    doc = _json(r.body) if r.body else None
    if r.status == 200 and isinstance(doc, dict) and isinstance(doc.get('access_token'), str) and isinstance(doc.get('refresh_token'), str):
        now = int(time.time())
        expires_in = doc.get('expires_in', 3600)
        expires_in = int(expires_in) if isinstance(expires_in, (int, Decimal)) and 0 < expires_in < 10**7 else 3600   # malformed: assume 1 hour
        refresh_in = doc.get('x_refresh_token_expires_in')
        refresh_in = int(refresh_in) if isinstance(refresh_in, (int, Decimal)) and 0 < refresh_in < 10**9 else None
        return {'access_token': doc['access_token'], 'refresh_token': doc['refresh_token'], 'access_expires_at': now + max(60, min(expires_in, 86400)),
                'refresh_expires_at': now + refresh_in if refresh_in else None}
    error = doc.get('error') if isinstance(doc, dict) else None
    if r.status == 429:
        raise ProviderError('THROTTLED', 'PROVIDER_THROTTLED', status=429)
    if r.status >= 500 or (r.status == 200 and doc is None):
        raise ProviderError('RETRYABLE', 'PROVIDER_UNAVAILABLE', status=r.status)
    if error == 'invalid_grant':
        raise ProviderError('TERMINAL' if exchange else 'AUTH_INVALID', 'AUTHORIZATION_CODE_INVALID' if exchange else 'REFRESH_TOKEN_INVALID', status=r.status)
    if error == 'invalid_client' or r.status == 401:
        raise ProviderError('TERMINAL', 'CLIENT_CREDENTIALS_INVALID', status=r.status)
    raise ProviderError('TERMINAL', 'TOKEN_REQUEST_REJECTED', status=r.status)


def exchange_code(guard, settings, code):
    r = guard.request('POST', TOKEN_ENDPOINT, headers={'Authorization': _basic(settings.client_id, settings.client_secret),
                      'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'},
                      data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': settings.redirect_uri})
    return _token_result(r, exchange=True)


def refresh(guard, settings, refresh_token):
    r = guard.request('POST', TOKEN_ENDPOINT, headers={'Authorization': _basic(settings.client_id, settings.client_secret),
                      'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'},
                      data={'grant_type': 'refresh_token', 'refresh_token': refresh_token})
    return _token_result(r, exchange=False)


def revoke(guard, settings, token) -> str:
    """PROVIDER_CONFIRMED on 200; PROVIDER_UNCONFIRMED when Intuit answers but does not confirm; raises RETRYABLE otherwise."""
    r = guard.request('POST', REVOCATION_ENDPOINT, headers={'Authorization': _basic(settings.client_id, settings.client_secret),
                      'Accept': 'application/json', 'Content-Type': 'application/json'}, json_body={'token': token})
    if r.status == 200:
        return 'PROVIDER_CONFIRMED'
    if r.status == 429 or r.status >= 500:
        raise ProviderError('RETRYABLE', 'PROVIDER_UNAVAILABLE', status=r.status)
    if r.status == 401:   # our client credentials were refused: retry (an operator can fix them) rather than give up on revocation
        raise ProviderError('RETRYABLE', 'CLIENT_CREDENTIALS_INVALID', status=401)
    return 'PROVIDER_UNCONFIRMED'


def query_text(entity, start, size):
    if entity not in ENTITIES:
        raise ValueError('entity outside the accepted read scope')
    if entity in SINGLETONS:
        return f'select * from {entity}'
    return f'select * from {entity} ORDERBY Id STARTPOSITION {int(start)} MAXRESULTS {int(size)}'


def query(guard, realm_id, access_token, entity, start, size):
    """One read page. Returns (body bytes, object count, intuit_tid). Classifies every non-success (Stage 2C §2.3)."""
    if not realm_id.isdigit():
        raise ValueError('invalid realm')
    url = f'{API_BASE}/v3/company/{realm_id}/query?' + urlencode({'query': query_text(entity, start, size), 'minorversion': MINOR_VERSION}, quote_via=quote)
    r = guard.request('GET', url, headers={'Authorization': 'Bearer ' + access_token, 'Accept': 'application/json'})
    tid = r.headers.get('intuit_tid')
    tid = tid if tid and len(tid) <= 128 and tid.isprintable() and ' ' not in tid else None
    doc = _json(r.body) if r.body else None
    faults = []
    if isinstance(doc, dict) and isinstance(doc.get('Fault'), dict):
        faults = [str(e.get('code'))[:16] for e in doc['Fault'].get('Error', []) if isinstance(e, dict)][:5]
    if r.status == 200 and isinstance(doc, dict) and isinstance(doc.get('QueryResponse'), dict) and not faults:
        items = doc['QueryResponse'].get(entity, [])
        if not isinstance(items, list):
            raise ProviderError('TERMINAL', 'PROVIDER_RESPONSE_INVALID', status=200, intuit_tid=tid)
        return r.body, len(items), tid
    if r.status == 401:
        raise ProviderError('AUTH_INVALID', 'PROVIDER_AUTH_REJECTED', status=401, intuit_tid=tid, fault_codes=faults)
    if r.status == 429:
        raise ProviderError('THROTTLED', 'PROVIDER_THROTTLED', status=429, intuit_tid=tid, fault_codes=faults)
    if r.status >= 500:
        raise ProviderError('RETRYABLE', 'PROVIDER_UNAVAILABLE', status=r.status, intuit_tid=tid, fault_codes=faults)
    if r.status == 403:
        raise ProviderError('TERMINAL', 'PROVIDER_FORBIDDEN', status=403, intuit_tid=tid, fault_codes=faults)
    if r.status == 200:
        raise ProviderError('TERMINAL', 'PROVIDER_RESPONSE_INVALID', status=200, intuit_tid=tid, fault_codes=faults)
    raise ProviderError('TERMINAL', 'PROVIDER_REQUEST_REJECTED', status=r.status, intuit_tid=tid, fault_codes=faults)
