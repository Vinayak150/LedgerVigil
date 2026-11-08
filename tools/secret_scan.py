"""Pre-publication and CI secret scan. Exit status 1 when any finding exists; prints file:line and the rule, never the match.

    python -m tools.secret_scan            scan the files git tracks (or, outside git, every file under the repository)
    python -m tools.secret_scan --all PATH scan every file under PATH (for packages and archives before delivery)

Rules: private keys, cloud/API key shapes, OAuth bearer/refresh token shapes, connection strings with an embedded password,
assignments of secret-named variables to literal values, and file names that must never be published (.env, keys, dumps,
SQLite databases, backups). Synthetic test values are allowed only when they carry the SYNTHETIC marker or are clearly
built at runtime (secrets.token_hex)."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = re.compile(r'(^|/)(\.env(\..*)?|\.env\.roles|.*\.pem|.*\.key|.*\.p12|.*\.pfx|.*\.dump|.*\.backup|.*\.sqlite3?|id_rsa.*|.*\.kdbx)$')
ALLOWED_NAMES = re.compile(r'(^|/)(deploy/env/.*\.example)$')
RULES = [
    ('private-key', re.compile(r'-----BEGIN (RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')),
    ('aws-access-key', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('github-token', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{36,}\b')),
    ('slack-token', re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}\b')),
    ('google-api-key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('intuit-token', re.compile(r'\b(AB11|RT1-)[0-9A-Za-z._-]{30,}\b')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')),
    ('dsn-with-password', re.compile(r'postgres(ql)?(\+psycopg)?://[^:\s/@]+:(?!\$\{|<|\*|password@|PASSWORD|\{)[^@\s]{6,}@')),
    ('secret-assignment', re.compile(r'(?i)\b(POSTGRES_PASSWORD|[A-Z_]*_PASSWORD|LV_QBO_CLIENT_SECRET|CLIENT_SECRET|LV_CURSOR_KEY)\s*=\s*[\'"]?(?![\'"]?\s*$)(?!\$|<|\{|os\.|env|secrets\.|SYNTHETIC|\.\.\.|changeme|required|None|\'\s*\+|f\'|f")[A-Za-z0-9+/=_-]{12,}')),
]
SKIP_DIRS = {'.git', '.venv', 'node_modules', '.next', '__pycache__', '.pytest_cache'}


def files(root, use_git):
    if use_git:
        try:
            out = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, check=True).stdout
            return [root / p for p in out.decode().split('\0') if p]
        except Exception:
            pass
    return [p for p in root.rglob('*') if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(root).parts)]


def scan(root, use_git=True):
    findings = []
    for path in files(root, use_git):
        rel = path.relative_to(root).as_posix()
        if FORBIDDEN_NAMES.search(rel) and not ALLOWED_NAMES.search(rel):
            findings.append((rel, 0, 'forbidden-file'))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b'\0' in data[:4096]:
            continue
        for n, line in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
            if 'secret-scan: allow' in line:
                continue
            for name, rule in RULES:
                if rule.search(line):
                    findings.append((rel, n, name))
    return findings


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root, use_git = ROOT, True
    if argv[:1] == ['--all']:
        root, use_git = Path(argv[1]).resolve(), False
    found = scan(root, use_git)
    for rel, n, name in found:
        print(f'{rel}:{n}: {name}')
    print(f'secret scan: {len(found)} finding(s) in {root.name}', file=sys.stderr)
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
