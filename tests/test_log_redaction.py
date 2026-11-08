"""Secrets and the OAuth callback query never reach log output at the entry points (API access/error logs, HTTP client
logs, worker logs). SYNTHETIC values."""
import io
import logging
from pathlib import Path

from ledgervigil import logredact

CODE, STATE = 'SYNTHETICCODEab12cd34ef56', 'S' * 43


def capture(name, fmt, *args, exc=None):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    logredact.install()
    try:
        if exc:
            try:
                raise exc
            except Exception:
                logger.exception(fmt, *args)
        else:
            logger.error(fmt, *args)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def test_access_log_drops_callback_query_string():
    out = capture('uvicorn.access', '%s - "%s %s HTTP/%s" %d', '127.0.0.1:5', 'GET',
                  f'/connections/quickbooks/callback?code={CODE}&state={STATE}&realmId=9341', '1.1', 200)
    assert CODE not in out and STATE not in out and '9341' not in out and '/connections/quickbooks/callback?[REDACTED]' in out


def test_messages_mask_tokens_codes_and_authorization_headers():
    out = capture('ledgervigil.test', 'retry %s %s %s', f'https://x/?code={CODE}&state={STATE}', 'Authorization: Bearer AT-0123456789abcdef',
                  '{"refresh_token": "RT-0123456789abcdef", "access_token": "AT-x"}')
    for secret in (CODE, STATE, 'AT-0123456789abcdef', 'RT-0123456789abcdef'):
        assert secret not in out


def test_exception_text_is_withheld():
    out = capture('uvicorn.error', 'failed', exc=ValueError(f'bad code={CODE} refresh_token=RT-abcdef0123456789'))
    assert CODE not in out and 'RT-abcdef0123456789' not in out and 'ValueError' in out and 'Traceback' in out


def test_http_client_loggers_do_not_log_requests_at_info():
    logredact.install()
    assert logging.getLogger('httpx').getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger('httpcore').getEffectiveLevel() >= logging.WARNING


def test_frontend_server_does_not_log_incoming_requests():
    config = (Path(__file__).resolve().parents[1] / 'web' / 'next.config.ts').read_text()
    assert 'logging: { incomingRequests: false }' in config


def test_uvicorn_access_formatter_still_works():
    from uvicorn.logging import AccessFormatter
    logredact.install()
    record = logging.getLogger('uvicorn.access').makeRecord('uvicorn.access', logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                                            ('127.0.0.1:5', 'GET', f'/connections/quickbooks/callback?code={CODE}', '1.1', 200), None)
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(record)
    assert CODE not in line and '/connections/quickbooks/callback?[REDACTED]' in line and '200' in line


def test_repr_colon_forms_and_make_log_record_are_redacted():
    out = capture('ledgervigil.test', 'tokens %s headers %s', {'refresh_token': 'RT-abcdef0123456789', 'state': STATE}, 'Authorization: Basic c2VjcmV0OnNlY3JldA==')
    assert 'RT-abcdef0123456789' not in out and STATE not in out and 'c2VjcmV0OnNlY3JldA' not in out
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logredact.install()
    handler.addFilter(logredact._FILTER)
    record = logging.makeLogRecord({'name': 'x', 'levelno': logging.ERROR, 'levelname': 'ERROR', 'msg': f'callback ?code={CODE}&state={STATE}', 'args': None})
    handler.handle(record)
    assert CODE not in stream.getvalue() and STATE not in stream.getvalue()
