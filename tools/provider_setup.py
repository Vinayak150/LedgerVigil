"""One-time private setup for the QuickBooks SANDBOX connection. Run it yourself in Terminal; never paste the values in chat.

    python -m tools.provider_setup <private directory, outside app/>
    python -m tools.provider_setup --container <directory>    same files, with paths as mounted by deploy/compose.yaml

Asks for the Intuit Development (sandbox) client id and client secret with hidden input, generates a new 3072-bit RSA
key-encryption key pair, and writes two separate directories under the target (target 0700):
    api/     0700  api.env (client id, redirect URI, public key path; no secret)  kek-public.pem   -> LV_PROVIDER_API_CONFIG
    worker/  0700  worker.env (adds client secret, private key path)              kek-private.pem  -> LV_PROVIDER_WORKER_CONFIG
api.env never names anything in worker/. On a single-user Mac both processes still run as the same OS user, so the split is
by configuration, not by the OS; for any shared deployment run the worker as its own user and hand it worker/ only.
Refuses to overwrite an existing directory. Prints only file paths, never a secret.
"""
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ledgervigil.provider import envelope  # noqa: E402

DEFAULT_REDIRECT = 'http://127.0.0.1:3000/connections/quickbooks/callback'


def write(path: Path, data: bytes, mode: int):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    os.chmod(path, mode)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    container = bool(argv) and argv[0] == '--container'
    argv = argv[1:] if container else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(argv[0]).expanduser().resolve()
    app = Path(__file__).resolve().parents[1]
    if target == app or app in target.parents:
        print('Refused: keep the private directory outside app/ so no package or copy can include it.', file=sys.stderr)
        return 2
    if target.exists():
        print(f'Refused: {target} already exists. Choose a new directory (old keys must be kept until their tokens are revoked).', file=sys.stderr)
        return 2
    client_id = getpass.getpass('Intuit Development client ID (input hidden): ').strip()
    client_secret = getpass.getpass('Intuit Development client secret (input hidden): ').strip()
    redirect = input(f'Redirect URI registered in the Intuit app [{DEFAULT_REDIRECT}]: ').strip() or DEFAULT_REDIRECT
    if not client_id or not client_secret or any(c.isspace() or c == '=' for c in client_id + client_secret):
        print('Refused: empty or malformed client id/secret.', file=sys.stderr)
        return 2
    if not redirect.startswith(('http://127.0.0.1:', 'http://localhost:', 'https://')) or '?' in redirect or '#' in redirect:
        print('Refused: the redirect URI must be http://127.0.0.1:<port>/..., http://localhost:<port>/... or https://...', file=sys.stderr)
        return 2
    target.mkdir(mode=0o700, parents=False)
    os.chmod(target, 0o700)
    api_dir, worker_dir = target / 'api', target / 'worker'
    for d in (api_dir, worker_dir):
        d.mkdir(mode=0o700); os.chmod(d, 0o700)
    private, public = envelope.generate_kek()
    write(worker_dir / 'kek-private.pem', private, 0o600)
    write(api_dir / 'kek-public.pem', public, 0o644)
    write(worker_dir / 'kek-public.pem', public, 0o644)
    # --container: the paths each process sees inside its container (deploy/compose.yaml secret mounts)
    api_public = '/run/secrets/kek_public' if container else api_dir / 'kek-public.pem'
    worker_public = '/run/secrets/kek_public_worker' if container else worker_dir / 'kek-public.pem'
    worker_private = '/run/secrets/kek_private' if container else worker_dir / 'kek-private.pem'
    api = f'LV_QBO_CLIENT_ID={client_id}\nLV_QBO_REDIRECT_URI={redirect}\nLV_PROVIDER_KEK_PUBLIC={api_public}\n'
    worker = (f'LV_QBO_CLIENT_ID={client_id}\nLV_QBO_REDIRECT_URI={redirect}\nLV_PROVIDER_KEK_PUBLIC={worker_public}\n'
              f'LV_QBO_CLIENT_SECRET={client_secret}\nLV_PROVIDER_KEK_PRIVATE={worker_private}\n')
    write(api_dir / 'api.env', ('# QuickBooks SANDBOX, API process. No secret here.\n' + api).encode(), 0o600)
    write(worker_dir / 'worker.env', ('# QuickBooks SANDBOX, integration worker only. Contains the client secret.\n' + worker).encode(), 0o600)
    del client_secret, worker
    print('Written (no secret printed):')
    for path in (api_dir / 'api.env', api_dir / 'kek-public.pem', worker_dir / 'worker.env', worker_dir / 'kek-private.pem', worker_dir / 'kek-public.pem'):
        print(f'  {path}  mode {oct(path.stat().st_mode & 0o777)}')
    print(f'API process:        LV_PROVIDER_SANDBOX_ENABLED=1 LV_PROVIDER_API_CONFIG={api_dir / "api.env"}')
    print(f'Integration worker: LV_PROVIDER_WORKER_CONFIG={worker_dir / "worker.env"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
