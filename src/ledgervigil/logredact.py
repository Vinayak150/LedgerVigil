"""Redaction at the log entry points (request lines, error messages, HTTP client logs).

The OAuth callback carries a single-use authorization code and state in its query string. The browser page removes them from
the address bar, but every server that receives the GET can log the request line first, so redaction must happen where the
log record is made. This filter is installed on the API's access/error loggers and on the HTTP client loggers used by the
worker; deployment proxies get the equivalent rule (deploy/proxy/Caddyfile). Query strings are dropped from every logged
request line (no endpoint here needs them for diagnosis), and known secret shapes are masked anywhere in a message.
"""
import logging
import re
import traceback

_SECRET_PARAM = re.compile(r'(?i)\b(code|state|realmId|realm_id|access_token|refresh_token|id_token|token|password|client_secret|csrf_token)=([^&\s"\']+)')
_BEARER = re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}')
_JSON_SECRET = re.compile(r'(?i)"(access_token|refresh_token|id_token|client_secret|code|state|password|csrf_token|token)"\s*:\s*"[^"]*"')
# Python repr / dict form: 'refresh_token': 'RT-...'
_REPR_SECRET = re.compile(r"(?i)'(access_token|refresh_token|id_token|client_secret|code|state|password|csrf_token|token)'\s*:\s*'[^']*'")
# Unambiguous names in 'name: value' form (headers, ad-hoc messages); generic words like code/state are not matched here.
_COLON_SECRET = re.compile(r'(?i)\b(access_token|refresh_token|id_token|client_secret|password|x-csrf-token|authorization|cookie|set-cookie)\s*:\s*([^\s,;]+)')
LOGGERS = ('uvicorn.access', 'uvicorn.error', 'uvicorn', 'httpx', 'httpcore', 'ledgervigil', 'celery', 'celery.app.trace')


def redact(text: str) -> str:
    text = _SECRET_PARAM.sub(lambda m: m.group(1) + '=[REDACTED]', text)
    text = _BEARER.sub(lambda m: m.group(1) + ' [REDACTED]', text)
    text = _JSON_SECRET.sub(lambda m: '"' + m.group(1) + '": "[REDACTED]"', text)
    text = _REPR_SECRET.sub(lambda m: "'" + m.group(1) + "': '[REDACTED]'", text)
    return _COLON_SECRET.sub(lambda m: m.group(1) + ': [REDACTED]', text)


def strip_query(path: str) -> str:
    return path.split('?', 1)[0] + ('?[REDACTED]' if '?' in path else '')


def _clean(record: logging.LogRecord, force: bool = False) -> None:
    # The factory cleans each record when it is created; handler/logger filters clean again (force) because records built
    # with logging.makeLogRecord() get their message only after the factory ran (review N6). redact() is idempotent.
    if getattr(record, '_lv_redacted', False) and not force:
        return
    record._lv_redacted = True
    if record.name == 'uvicorn.access' and isinstance(record.args, tuple) and len(record.args) >= 3:
        args = list(record.args)
        args[2] = strip_query(str(args[2]))   # (client, method, full_path, http_version, status)
        record.args = tuple(args)   # uvicorn's AccessFormatter unpacks these five arguments, so they stay a tuple
        return
    try:
        message = record.getMessage()
    except Exception:
        return
    cleaned = redact(message)
    if cleaned != message or record.args:
        record.msg, record.args = cleaned, None
    if record.exc_info and record.exc_info[1] is not None:   # tracebacks are kept for diagnosis, with secrets masked
        record.exc_text = redact(''.join(traceback.format_exception(*record.exc_info)).rstrip())
        record.exc_info = None


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        _clean(record, force=True)
        return True


_FILTER = RedactingFilter()
_BASE_FACTORY = None


def install(names=LOGGERS):
    """Idempotent. Every log record created in this process is redacted when it is created (record factory), so loggers and
    handlers configured later (uvicorn, Celery) are covered too; the filter is also attached to the named loggers and to the
    root handlers as a second layer."""
    global _BASE_FACTORY
    if _BASE_FACTORY is None:
        _BASE_FACTORY = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = _BASE_FACTORY(*args, **kwargs)
            _clean(record)
            return record
        logging.setLogRecordFactory(factory)
    for name in names:
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
    for handler in logging.getLogger().handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(max(logging.getLogger(name).level, logging.WARNING))
    return _FILTER
