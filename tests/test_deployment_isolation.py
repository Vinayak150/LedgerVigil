"""Static checks of the deployment artifacts (deploy/): process identities, secret mounts and log redaction. These check
the configuration as written; they do not start containers (no container runtime is assumed by the test suite)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / 'deploy' / 'compose.yaml').read_text()
WORKER_ONLY = {'kek_private', 'qbo_worker_env', 'kek_public_worker'}


def services():
    body = COMPOSE.split('\nservices:\n', 1)[1].split('\nsecrets:\n', 1)[0]
    blocks, name = {}, None
    for line in body.splitlines():
        m = re.match(r'^  ([a-z-]+):', line)
        if m:
            name = m.group(1); blocks[name] = []
        elif name:
            blocks[name].append(line)
    return {k: '\n'.join(v) for k, v in blocks.items()}


def field(block, key):
    m = re.search(rf'^    {key}: (.*)$', block, re.M)
    return m.group(1).strip() if m else None


def secrets_of(block):
    value = field(block, 'secrets')
    return set(re.findall(r'[a-z_]+', value)) if value else set()


def test_every_application_process_has_its_own_non_root_identity():
    s = services()
    users = {n: field(b, 'user') for n, b in s.items() if 'deploy/Dockerfile' in b}
    assert set(users) == {'migrate', 'api', 'web', 'maintenance', 'provider-worker', 'scheduler'}
    assert all(u and not u.startswith('"0') for u in users.values())
    assert len(set(users.values())) == len(users)   # distinct uids: no two processes share an OS identity


def test_only_the_provider_worker_receives_quickbooks_decryption_material():
    s = services()
    for name, block in s.items():
        mounted = secrets_of(block)
        if name == 'provider-worker':
            assert WORKER_ONLY <= mounted
        else:
            assert not (mounted & WORKER_ONLY), name
    assert 'qbo_worker_env' not in s['api'] and 'kek_private' not in s['api'] and 'LV_PROVIDER_WORKER_CONFIG' not in s['api']


def test_database_logins_are_split_by_process():
    s = services()
    assert secrets_of(s['api']) & {'app_dsn'} and not secrets_of(s['api']) & {'worker_dsn', 'migrator_dsn', 'pg_superuser_password'}
    for name in ('maintenance', 'provider-worker', 'scheduler'):
        assert 'worker_dsn' in secrets_of(s[name]) and not secrets_of(s[name]) & {'app_dsn', 'migrator_dsn', 'pg_superuser_password'}
    assert secrets_of(s['web']) == set()


def test_gates_are_off_by_default_and_database_is_internal():
    assert re.search(r'^\s+LV_PROVIDER_SANDBOX_ENABLED:', COMPOSE, re.M) is None   # only commented-out lines
    assert 'LV_DETECTION_ENABLED: "1"' not in COMPOSE
    assert 'backend: {internal: true}' in COMPOSE and 'queue: {internal: true}' in COMPOSE
    assert 'queue' not in field(services()['api'], 'networks') and 'queue' in field(services()['broker'], 'networks')
    assert './secrets/' not in COMPOSE and '${LV_SECRETS_DIR:?' in COMPOSE   # secret files live outside the repository
    assert 'ports' not in services()['db'] and 'ports' not in services()['api']


def test_proxy_access_log_drops_oauth_query_and_credentials():
    caddy = (ROOT / 'deploy' / 'proxy' / 'Caddyfile').read_text()
    for item in ('delete code', 'delete state', 'delete realmId', 'request>headers>Cookie delete', 'request>headers>Authorization delete'):
        assert item in caddy


def test_application_containers_drop_privileges():
    s = services()
    for name in ('api', 'web', 'maintenance', 'provider-worker', 'scheduler', 'migrate'):
        assert field(s[name], 'read_only') == 'true' and 'no-new-privileges:true' in s[name] and field(s[name], 'cap_drop') == '[ALL]', name
