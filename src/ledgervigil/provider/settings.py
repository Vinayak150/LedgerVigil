"""QuickBooks sandbox configuration. Two private files, written by tools/provider_setup.py, never by hand-typed chat secrets:

- LV_PROVIDER_API_CONFIG: client id, redirect URI and the path of the KEK PUBLIC key. Read by the API process.
- LV_PROVIDER_WORKER_CONFIG: the same plus the client secret and the path of the KEK PRIVATE key. Read only by the
  integration worker. It must be a regular file owned by this user with no group/other permission bits.

Nothing here logs or returns a secret. A missing or unsafe configuration makes the feature report NOT_CONFIGURED.
"""
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import envelope

API_KEYS = ('LV_QBO_CLIENT_ID', 'LV_QBO_REDIRECT_URI', 'LV_PROVIDER_KEK_PUBLIC')
WORKER_KEYS = API_KEYS + ('LV_QBO_CLIENT_SECRET', 'LV_PROVIDER_KEK_PRIVATE')
# Optional, worker only: comma-separated paths of PREVIOUS private keys kept during a key rotation (open only, never seal).
PREVIOUS_KEY = 'LV_PROVIDER_KEK_PRIVATE_PREVIOUS'


class ConfigurationError(Exception):
    def __init__(self, code: str, missing=()):
        super().__init__(code)
        self.code, self.missing = code, tuple(missing)


def _read_private(path: Path) -> bytes:
    """Read through one descriptor opened without following symlinks; the file must be a regular file owned by this user with
    no group/other bits, in a directory owned by this user (or root) that others cannot write."""
    parent = os.stat(Path(path).parent)
    # The directory must be this user's (local setup) or root's (a container secret mount such as /run/secrets), and nobody
    # else may write to it.
    if parent.st_uid not in (os.getuid(), 0) or parent.st_mode & 0o022:
        raise ConfigurationError('PROVIDER_CONFIG_UNSAFE')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ConfigurationError('PROVIDER_CONFIG_UNSAFE') from None
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ConfigurationError('PROVIDER_CONFIG_UNSAFE')
        return f.read()


def _parse(path: Path, private: bool) -> dict:
    text = _read_private(path).decode() if private else Path(path).read_text()
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, val = line.split('=', 1)
        values[k.strip()] = val.strip()
    return values


def _redirect_ok(uri: str) -> bool:
    # Intuit permits plain HTTP only for localhost testing; anything else must be HTTPS.
    return uri.startswith(('http://127.0.0.1:', 'http://localhost:', 'https://')) and '#' not in uri and '?' not in uri and len(uri) <= 512


@dataclass(frozen=True)
class ApiSettings:
    client_id: str
    redirect_uri: str
    public_key: object

    @property
    def kek_id(self):
        return envelope.kek_id(self.public_key)


@dataclass(frozen=True)
class WorkerSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    private_key: object
    previous_keys: tuple = ()

    def __repr__(self):   # never print the secret
        return f'WorkerSettings(client_id=<set>, redirect_uri={self.redirect_uri!r}, client_secret=<redacted>, previous_keys={len(self.previous_keys)})'

    @property
    def keyring(self):
        ring = {envelope.kek_id(k.public_key()): k for k in self.previous_keys}
        ring[self.kek_id] = self.private_key
        return ring

    @property
    def kek_id(self):
        return envelope.kek_id(self.private_key.public_key())


def api_settings(environ=os.environ) -> ApiSettings:
    path = environ.get('LV_PROVIDER_API_CONFIG')
    if not path or not Path(path).is_file():
        raise ConfigurationError('PROVIDER_NOT_CONFIGURED', API_KEYS)
    values = _parse(Path(path), private=False)
    missing = [k for k in API_KEYS if not values.get(k)]
    if 'LV_QBO_CLIENT_SECRET' in values or 'LV_PROVIDER_KEK_PRIVATE' in values:
        raise ConfigurationError('PROVIDER_CONFIG_UNSAFE')   # the API process must never be handed worker secrets
    if missing:
        raise ConfigurationError('PROVIDER_NOT_CONFIGURED', missing)
    if not _redirect_ok(values['LV_QBO_REDIRECT_URI']):
        raise ConfigurationError('PROVIDER_REDIRECT_URI_INVALID')
    return ApiSettings(values['LV_QBO_CLIENT_ID'], values['LV_QBO_REDIRECT_URI'], envelope.load_public(Path(values['LV_PROVIDER_KEK_PUBLIC']).read_bytes()))


def worker_settings(environ=os.environ) -> WorkerSettings:
    path = environ.get('LV_PROVIDER_WORKER_CONFIG')
    if not path or not Path(path).is_file():
        raise ConfigurationError('PROVIDER_NOT_CONFIGURED', WORKER_KEYS)
    values = _parse(Path(path), private=True)
    missing = [k for k in WORKER_KEYS if not values.get(k)]
    if missing:
        raise ConfigurationError('PROVIDER_NOT_CONFIGURED', missing)
    if not _redirect_ok(values['LV_QBO_REDIRECT_URI']):
        raise ConfigurationError('PROVIDER_REDIRECT_URI_INVALID')
    private = envelope.load_private(_read_private(Path(values['LV_PROVIDER_KEK_PRIVATE'])))
    public = envelope.load_public(Path(values['LV_PROVIDER_KEK_PUBLIC']).read_bytes())
    if envelope.kek_id(public) != envelope.kek_id(private.public_key()):
        raise ConfigurationError('PROVIDER_KEK_MISMATCH')
    previous = tuple(envelope.load_private(_read_private(Path(x.strip()))) for x in values.get(PREVIOUS_KEY, '').split(',') if x.strip())
    return WorkerSettings(values['LV_QBO_CLIENT_ID'], values['LV_QBO_CLIENT_SECRET'], values['LV_QBO_REDIRECT_URI'], private, previous)
