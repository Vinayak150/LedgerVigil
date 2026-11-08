"""Integration worker entry point without a broker:
    python -m ledgervigil.provider.worker run-due      claim and run due QuickBooks SANDBOX jobs
    python -m ledgervigil.provider.worker rotate-kek   rewrap stored tokens under the current key-encryption key

Claims due QuickBooks SANDBOX jobs with the same fenced protocol as the Celery task, so running both is safe. Needs
LV_WORKER_DATABASE_URL (restricted lv_worker login) and LV_PROVIDER_WORKER_CONFIG (private file from tools/provider_setup.py).
Prints one line per job: ids, outcome class and error class only.
"""
import json
import sys

from .runtime import rotate_kek, run_due


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    from ledgervigil import logredact
    logredact.install()
    if argv == ['rotate-kek']:
        print(json.dumps(rotate_kek()))
        return 0
    if argv != ['run-due']:
        print('usage: python -m ledgervigil.provider.worker run-due | rotate-kek', file=sys.stderr)
        return 2
    for r in run_due():
        print(json.dumps({k: r.get(k) for k in ('job_id', 'status', 'outcome', 'error_class')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
