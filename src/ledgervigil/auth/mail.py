"""Delivery of verification, reset and invitation messages.

Links carry the token in the URL FRAGMENT (`/auth/reset#token=...`): browsers never send fragments to servers, so the token
cannot reach proxy, frontend-server or analytics logs. The frontend page reads it once, clears it from the address bar and
POSTs it to the API.

Adapters (LV_MAIL_ADAPTER):
- `disabled` (default): nothing is sent; requests are still recorded and answered identically (202). Real users cannot
  verify or reset until an operator configures delivery.
- `capture`: development and automated tests. Each message is written as one JSON file (mode 0600) in LV_MAIL_CAPTURE_DIR
  (a directory this process owns, 0700). Refused when LV_ENVIRONMENT=production.
- `smtp`: LV_SMTP_HOST, LV_SMTP_PORT (587), LV_SMTP_USERNAME, LV_SMTP_PASSWORD_FILE, LV_MAIL_FROM. STARTTLS is required and
  the server certificate is verified. Not exercised against a real mail service in this repository.
Nothing here logs a token, a link or a message body.
"""
import json
import logging
import os
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger('ledgervigil.mail')
SUBJECTS = {
    'VERIFY_EMAIL': 'Confirm your email address for LedgerVigil',
    'RESET_PASSWORD': 'Reset your LedgerVigil password',
    'INVITATION': 'You have been invited to LedgerVigil',
    'ADDED': 'You were added to an organization in LedgerVigil',
}
PATHS = {'VERIFY_EMAIL': '/auth/verify', 'RESET_PASSWORD': '/auth/reset', 'INVITATION': '/auth/invitation'}


class MailUnavailable(Exception):
    pass


def public_origin():
    origin = os.environ.get('LV_PUBLIC_ORIGIN', 'http://127.0.0.1:3000').rstrip('/')
    if not origin.startswith(('https://', 'http://127.0.0.1:', 'http://localhost:')) or '#' in origin or '?' in origin:
        raise MailUnavailable('LV_PUBLIC_ORIGIN must be https://... (or a loopback http origin in development)')
    return origin


def link(kind, token):
    return f'{public_origin()}{PATHS[kind]}#token={token}'


def body(kind, display_name, token, expires_in_seconds):
    greeting = f'Hello {display_name},' if display_name else 'Hello,'
    if kind == 'ADDED':
        return f'{greeting}\n\nAn administrator added you to an organization in LedgerVigil. Sign in at {public_origin()} to see it.\n'
    minutes = int(expires_in_seconds) // 60
    action = {'VERIFY_EMAIL': 'confirm your email address', 'RESET_PASSWORD': 'choose a new password',
              'INVITATION': 'accept the invitation and choose a password'}[kind]
    return (f'{greeting}\n\nOpen this link to {action}. It works once and expires in {minutes} minutes:\n\n{link(kind, token)}\n\n'
            'If you did not ask for this, ignore this message; nothing changes.\n')


class Disabled:
    name = 'disabled'

    def send(self, kind, to, display_name, token, expires_in_seconds):
        log.warning('mail delivery is not configured; a %s message was not sent', kind)


class Capture:
    name = 'capture'

    def __init__(self, directory):
        if os.environ.get('LV_ENVIRONMENT', 'production') == 'production':
            raise MailUnavailable('the capture adapter is for development and tests only')
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise MailUnavailable('the capture directory must be private (0700)')

    def send(self, kind, to, display_name, token, expires_in_seconds):
        record = {'kind': kind, 'to': to, 'subject': SUBJECTS[kind], 'body': body(kind, display_name, token, expires_in_seconds),
                  'link': link(kind, token) if token else None, 'captured_at': time.time()}
        path = self.directory / f'{time.time_ns()}-{uuid.uuid4().hex}.json'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(record, f)


class Smtp:
    name = 'smtp'

    def __init__(self):
        self.host = os.environ.get('LV_SMTP_HOST')
        self.port = int(os.environ.get('LV_SMTP_PORT', '587'))
        self.username = os.environ.get('LV_SMTP_USERNAME')
        password_file = os.environ.get('LV_SMTP_PASSWORD_FILE')
        self.password = Path(password_file).read_text().strip() if password_file else None
        self.sender = os.environ.get('LV_MAIL_FROM')
        if not self.host or not self.sender:
            raise MailUnavailable('LV_SMTP_HOST and LV_MAIL_FROM are required for smtp delivery')

    def send(self, kind, to, display_name, token, expires_in_seconds):
        message = EmailMessage()
        message['From'], message['To'], message['Subject'] = self.sender, to, SUBJECTS[kind]
        message.set_content(body(kind, display_name, token, expires_in_seconds))
        with smtplib.SMTP(self.host, self.port, timeout=10) as client:
            client.starttls(context=ssl.create_default_context())
            if self.username:
                client.login(self.username, self.password or '')
            client.send_message(message)


def adapter():
    kind = os.environ.get('LV_MAIL_ADAPTER', 'disabled')
    if kind == 'capture':
        return Capture(os.environ.get('LV_MAIL_CAPTURE_DIR', ''))
    if kind == 'smtp':
        return Smtp()
    if kind != 'disabled':
        raise MailUnavailable('LV_MAIL_ADAPTER must be disabled, capture or smtp')
    return Disabled()


def deliver(kind, to, display_name, token, expires_in_seconds):
    """Background delivery: failures are logged by class only and never change the HTTP answer already given."""
    try:
        adapter().send(kind, to, display_name, token, expires_in_seconds)
    except Exception as exc:
        log.error('mail delivery failed kind=%s class=%s', kind, type(exc).__name__)
