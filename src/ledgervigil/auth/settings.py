"""Deployment settings for the identity boundary. Secure cookies are the default; local HTTP must be chosen explicitly."""
import os
import re
import secrets
from dataclasses import dataclass, field

SECURE, LOCAL_HTTP = 'secure', 'local-http'
LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '[::1]'})


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthSettings:
    cookie_mode: str = SECURE                 # 'secure' (default, production) or 'local-http' (development only)
    environment: str = 'production'
    allowed_origins: frozenset = frozenset()  # empty: only the request's own origin is accepted on unsafe requests
    # HMAC key for opaque list cursors (Stage 2B Part 2: "opaque, signed"). From LV_CURSOR_KEY when set; otherwise a random
    # per-process key, so cursors stop verifying after a restart and are not shared between processes (see docs/LEDGER_READS.md).
    cursor_key: bytes = field(default=b'', repr=False, compare=False)

    def __post_init__(self):
        if not self.cursor_key:
            object.__setattr__(self, 'cursor_key', secrets.token_bytes(32))
        if not isinstance(self.cursor_key, bytes) or len(self.cursor_key) < 32:
            raise SettingsError('the cursor key must be at least 32 bytes')
        if self.cookie_mode not in (SECURE, LOCAL_HTTP):
            raise SettingsError('LV_COOKIE_MODE must be "secure" or "local-http"')
        if self.cookie_mode == LOCAL_HTTP and self.environment != 'development':
            raise SettingsError('local-http cookies require LV_ENVIRONMENT=development; they are never a production configuration')

    @property
    def cookie_name(self) -> str:
        # The __Host- prefix makes browsers require Secure, Path=/ and no Domain attribute.
        return '__Host-lv_session' if self.cookie_mode == SECURE else 'lv_session_local'

    @property
    def cookie_secure(self) -> bool:
        return self.cookie_mode == SECURE


def load_settings(env=None) -> AuthSettings:
    env = os.environ if env is None else env
    origins = frozenset(o.strip() for o in env.get('LV_ALLOWED_ORIGINS', '').split(',') if o.strip())
    raw_key = env.get('LV_CURSOR_KEY', '').strip()
    if raw_key and not re.fullmatch(r'(?:[0-9a-fA-F]{2}){32,}', raw_key):
        raise SettingsError('LV_CURSOR_KEY must be at least 64 hexadecimal characters')
    return AuthSettings(env.get('LV_COOKIE_MODE', SECURE), env.get('LV_ENVIRONMENT', 'production'), origins,
                        bytes.fromhex(raw_key) if raw_key else b'')
