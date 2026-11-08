"""Ingest one synthetic processor-statement fixture into a SANDBOX ledger (local, internal command).

    .venv/bin/python -m tools.ingest_statement --tenant <uuid> --ledger <uuid> \
        --format ledgervigil.synthetic-processor-statement --version 1 path/to/statement.json

The command connects as the restricted lv_ingest login. It prints the recorded outcome as JSON:
STAGED, REJECTED with a code, DUPLICATE, or a repeat of an existing file. It writes no documents,
postings, projections, counters or history. Exit code: 0 STAGED or repeated/duplicate; 2 REJECTED;
3 refused before recording.
"""
import argparse
import json
from pathlib import Path
from uuid import UUID

from tools.dev_db import connection
from ledgervigil.ingestion.store import IngestionRefused, ingest_statement

HARD_LIMIT = 16 * 1024 * 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tenant', type=UUID, required=True)
    parser.add_argument('--ledger', type=UUID, required=True)
    parser.add_argument('--format', dest='declared_format', required=True)
    parser.add_argument('--version', dest='declared_version', required=True)
    parser.add_argument('file', type=Path)
    args = parser.parse_args()
    if args.file.stat().st_size > HARD_LIMIT:   # not even hashed or recorded
        print(json.dumps({'refused': 'FILE_EXCEEDS_HARD_LIMIT'}))
        raise SystemExit(3)
    raw = args.file.read_bytes()               # files over 256 KiB are recorded by hash and size, then rejected
    try:
        with connection('lv_ingest', autocommit=True) as c:
            result = ingest_statement(c, args.tenant, args.ledger, raw, declared_format=args.declared_format,
                                      declared_version=args.declared_version)
    except IngestionRefused as error:
        print(json.dumps({'refused': error.code}))
        raise SystemExit(3)
    print(json.dumps(result, indent=2))
    raise SystemExit(2 if result['status'] == 'REJECTED' and not result['repeated_file'] else 0)


if __name__ == '__main__':
    main()
