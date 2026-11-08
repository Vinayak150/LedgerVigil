"""A documented-shape MOCK of Intuit's OAuth and QuickBooks Online sandbox query endpoints, for offline contract tests.

NOT recorded from Intuit and NOT live sandbox evidence. Response shapes follow the documented formats cited in
src/ledgervigil/provider/intuit.py (token JSON with access_token/refresh_token/expires_in/x_refresh_token_expires_in;
query responses {"QueryResponse": {"<Entity>": [...], "startPosition", "maxResults"}, "time"}; Fault {"Fault": {"Error":
[{"Message","Detail","code"}], "type"}}). Every value is SYNTHETIC. Pages written by this mock carry provenance RECORDED_MOCK.
"""
import json
import secrets
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ledgervigil.provider import envelope, intuit
from ledgervigil.provider.settings import worker_settings


def write_config(tmp_path, redirect='http://127.0.0.1:3000/connections/quickbooks/callback'):
    """Private configuration exactly as tools/provider_setup.py lays it out (api/ and worker/; synthetic client credentials)."""
    d = tmp_path / 'provider-private'
    d.mkdir(mode=0o700)
    api, worker = d / 'api', d / 'worker'
    for x in (api, worker):
        x.mkdir(mode=0o700)
    private, public = envelope.generate_kek()
    (worker / 'kek-private.pem').write_bytes(private); (worker / 'kek-private.pem').chmod(0o600)
    for x in (api, worker):
        (x / 'kek-public.pem').write_bytes(public)
    (api / 'api.env').write_text(f'LV_QBO_CLIENT_ID=SYNTHETIC-client-id\nLV_QBO_REDIRECT_URI={redirect}\nLV_PROVIDER_KEK_PUBLIC={api / "kek-public.pem"}\n')
    (api / 'api.env').chmod(0o600)
    (worker / 'worker.env').write_text(f'LV_QBO_CLIENT_ID=SYNTHETIC-client-id\nLV_QBO_REDIRECT_URI={redirect}\nLV_PROVIDER_KEK_PUBLIC={worker / "kek-public.pem"}\n'
                                       f'LV_QBO_CLIENT_SECRET=SYNTHETIC-secret-{secrets.token_hex(8)}\nLV_PROVIDER_KEK_PRIVATE={worker / "kek-private.pem"}\n')
    (worker / 'worker.env').chmod(0o600)
    return {'LV_PROVIDER_API_CONFIG': str(api / 'api.env'), 'LV_PROVIDER_WORKER_CONFIG': str(worker / 'worker.env'), 'dir': worker}


def company(realm):
    """A small synthetic sandbox company. Amounts are written as JSON numbers with two decimals, as QuickBooks returns them."""
    meta = lambda i: {'CreateTime': f'2026-10-0{1 + i % 8}T10:00:00-07:00', 'LastUpdatedTime': f'2026-10-0{1 + i % 8}T10:00:00-07:00'}
    return {
        'Preferences': [{'Id': '1', 'SyncToken': '0', 'AccountingInfoPrefs': {'BookCloseDate': '2026-09-30'},
                         'CurrencyPrefs': {'MultiCurrencyEnabled': False, 'HomeCurrency': {'value': 'USD'}}, 'MetaData': meta(0)}],
        'CompanyInfo': [{'Id': realm, 'SyncToken': '3', 'CompanyName': 'SYNTHETIC Sandbox Company', 'Country': 'US', 'MetaData': meta(1)}],
        'Account': [{'Id': str(30 + i), 'SyncToken': '0', 'Name': f'SYNTHETIC account {i}', 'AccountType': 'Bank', 'CurrentBalance': '__N__1000.10',
                     'MetaData': meta(i)} for i in range(3)],
        'Customer': [{'Id': '58', 'SyncToken': '1', 'DisplayName': 'SYNTHETIC Customer', 'Balance': '__N__0.00', 'MetaData': meta(2)}],
        'Invoice': [{'Id': '130', 'SyncToken': '0', 'DocNumber': 'SYN-1001', 'TxnDate': '2026-10-02', 'TotalAmt': '__N__52500.50',
                     'Balance': '__N__0.00', 'CustomerRef': {'value': '58'}, 'MetaData': meta(3)}],
        'Payment': [{'Id': str(200 + i), 'SyncToken': str(i % 2), 'TxnDate': '2026-10-10', 'TotalAmt': '__N__' + ('50000.00', '2500.50', '0.10', '19.99', '7.00')[i],
                     'CustomerRef': {'value': '58'}, 'PrivateNote': 'SYNTHETIC', 'MetaData': meta(i)} for i in range(5)],
        'Deposit': [{'Id': '310', 'SyncToken': '0', 'TxnDate': '2026-10-14', 'TotalAmt': '__N__51500.50', 'PrivateNote': 'payout STL-3001 (SYNTHETIC)',
                     'Line': [{'Amount': '__N__50000.00', 'LinkedTxn': [{'TxnId': '200', 'TxnType': 'Payment'}]}], 'MetaData': meta(4)}],
        'JournalEntry': [], 'CreditMemo': [], 'RefundReceipt': []}


def _dumps(value):
    """JSON with '__N__1.10' strings emitted as bare numbers, so the bytes carry exact decimal text like a real response."""
    import re
    return re.sub(r'"__N__(-?[0-9]+\.[0-9]+)"', r'\1', json.dumps(value)).encode()


class FakeIntuit:
    provenance = 'RECORDED_MOCK'

    def __init__(self, realm='9341450000000001', *, access_ttl=3600):
        self.realm, self.access_ttl = realm, access_ttl
        self.data = company(realm)
        self.codes, self.access, self.refresh, self.revoked = {}, {}, set(), []
        self.calls, self.faults = [], []       # faults: list of (predicate(method, path, query), status, body)
        self.lock = threading.Lock()
        self.refresh_calls = 0
        self.refresh_gate = None                # threading.Event: block refresh responses until set

    def issue_code(self):
        code = 'SYNTHETICCODE' + secrets.token_hex(12)
        self.codes[code] = self.realm
        return code

    def _tokens(self):
        a, r = 'AT-' + secrets.token_hex(16), 'RT-' + secrets.token_hex(16)
        self.access[a] = self.realm
        self.refresh.add(r)
        return {'token_type': 'bearer', 'access_token': a, 'refresh_token': r, 'expires_in': self.access_ttl, 'x_refresh_token_expires_in': 8640000}

    def __call__(self, method, url, *, headers, data, json_body, timeout):
        u = urlsplit(url)
        q = parse_qs(u.query)
        with self.lock:
            self.calls.append((method, u.hostname, u.path, q.get('query', [None])[0]))
            for i, (pred, status, body) in enumerate(self.faults):
                if pred(method, u.path, q):
                    self.faults.pop(i)
                    return intuit.Response(status, {'intuit_tid': 'SYN-tid-fault'}, body)
        if u.path == '/oauth2/v1/tokens/bearer':
            if headers.get('Authorization', '').split(' ')[0] != 'Basic':
                return intuit.Response(401, {}, b'{"error":"invalid_client"}')
            if data['grant_type'] == 'authorization_code':
                if self.codes.pop(data['code'], None) is None:
                    return intuit.Response(400, {}, b'{"error":"invalid_grant"}')
                return intuit.Response(200, {}, json.dumps(self._tokens()).encode())
            with self.lock:
                self.refresh_calls += 1
            if self.refresh_gate is not None:
                self.refresh_gate.wait(10)
            with self.lock:
                if data['refresh_token'] not in self.refresh:
                    return intuit.Response(400, {}, b'{"error":"invalid_grant"}')
                self.refresh.discard(data['refresh_token'])        # rotation: the old refresh token stops working
                return intuit.Response(200, {}, json.dumps(self._tokens()).encode())
        if u.path == '/v2/oauth2/tokens/revoke':
            self.revoked.append(json_body['token'])
            self.refresh.discard(json_body['token'])
            return intuit.Response(200, {}, b'')
        if u.path.startswith('/v3/company/'):
            realm = u.path.split('/')[3]
            token = headers.get('Authorization', '')[7:]
            if self.access.get(token) != realm:
                return intuit.Response(401, {'intuit_tid': 'SYN-tid-401'}, json.dumps({'Fault': {'Error': [{'Message': 'message=AuthenticationFailed; errorCode=003200; statusCode=401', 'code': '3200'}], 'type': 'AUTHENTICATION'}}).encode())
            text = q['query'][0]
            entity = text.split()[3]
            items = self.data[entity]
            start, size = 1, 1000
            if 'STARTPOSITION' in text:
                parts = text.split()
                start, size = int(parts[parts.index('STARTPOSITION') + 1]), int(parts[parts.index('MAXRESULTS') + 1])
            chunk = items[start - 1:start - 1 + size]
            body = {'QueryResponse': ({entity: chunk, 'startPosition': start, 'maxResults': len(chunk)} if chunk else {}), 'time': '2026-11-07T10:00:00.000-08:00'}
            return intuit.Response(200, {'intuit_tid': 'SYN-tid-' + secrets.token_hex(4)}, _dumps(body))
        return intuit.Response(404, {}, b'')


def settings(cfg):
    return worker_settings({'LV_PROVIDER_WORKER_CONFIG': cfg['LV_PROVIDER_WORKER_CONFIG']})
