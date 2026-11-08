"""Simulation service: verified boundary -> consistent capture -> pure evaluation -> atomic, immutable record; and replay.

`simulate(request, proposal_id, revision=None)` is the foundation the atomic submission milestone will call. It is NOT exposed
as an HTTP route in this milestone (no /submit, no lifecycle change; docs/SIMULATION.md SM-C1).

1. Boundary transaction (READ COMMITTED, `propose`): verified session, tenant from the session, the proposal must be visible
   under row-level security (absent and other-tenant ids are the same 404) and DRAFT; the revision must exist.
2. Capture (second pooled lv_app connection, REPEATABLE READ READ ONLY, same session tenant): one snapshot (bundle.capture).
3. Pure evaluation of the bundle (engine.evaluate). Integrity failures raise and record nothing.
4. Record: lv.simulation_record(token hash, ...) inside the boundary transaction re-binds the session, re-checks `propose`,
   DRAFT and the revision, and inserts one immutable simulation_run row; the boundary transaction then commits. Any failure
   before COMMIT rolls the whole thing back: no row, and nothing else is ever written.

`replay(ctx, run_id)` recomputes from the stored canonical bundle only (never reloads current values), refuses with 409 if
any pinned version is not installed, and compares hashes.
"""
import json
import logging
from uuid import UUID

from ledgervigil.auth.boundary import RequestContext, session_transaction, source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError

from . import bundle as capture_module
from . import engine
from .canonical import canonical, digest

log = logging.getLogger('ledgervigil.simulation')
_before_record = None      # test seam only (None in production): runs after evaluation, before the record call


def simulate(request, proposal_id: UUID, revision: int | None = None) -> dict:
    with session_transaction(request, permission='propose') as ctx:
        head = ctx.conn.execute('SELECT revision, state::text FROM lv.proposal WHERE id = %s', (proposal_id,)).fetchall()
        if len(head) != 1:
            raise ProblemError(404, 'NOT_FOUND', 'proposal not found')
        rev = head[0][0] if revision is None else revision
        if type(rev) is not int or rev < 1 or not ctx.conn.execute(
                'SELECT 1 FROM lv.proposal_revision WHERE proposal_id = %s AND revision = %s', (proposal_id, rev)).fetchone():
            raise ProblemError(404, 'NOT_FOUND', 'proposal revision not found')
        if head[0][1] != 'DRAFT':
            raise ProblemError(409, 'INVALID_STATE_TRANSITION', 'only DRAFT revisions are simulated here', extra={'state': head[0][1]})
        try:
            captured = capture_module.capture(request.app.state.pool, ctx.tenant_id, proposal_id, rev)
        except capture_module.CaptureRefused as refusal:
            raise ProblemError(refusal.status, refusal.code, refusal.detail) from None
        try:
            outcome = engine.evaluate(captured)
        except engine.IntegrityFailure as failure:
            log.error('simulation refused: captured facts inconsistent proposal_id=%s request_id=%s detail=%s', proposal_id,
                      request.state.request_id, failure)
            raise ProblemError(500, 'INTERNAL', 'captured ledger facts are inconsistent; nothing was recorded') from None
        bundle_text, outcome_text = canonical(captured), canonical(outcome)
        if _before_record is not None:
            _before_record()
        result = ctx.conn.execute(
            'SELECT lv.simulation_record(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (token_hash(ctx.session_token), proposal_id, rev, bundle_text, digest(bundle_text), outcome_text, digest(outcome_text),
             outcome['status'], captured['versions']['simulator'], int(captured['snapshot']['seq']), source_key(request),
             request.state.request_id)).fetchone()[0]
        if result['status'] != 201:
            raise ProblemError(result['status'], result['code'], result.get('detail'), extra={k: result[k] for k in ('state',) if k in result})
        run_id = result['id']
    log.info('simulation recorded request_id=%s run_id=%s status=%s seq=%s', request.state.request_id, run_id, outcome['status'],
             captured['snapshot']['seq'])
    return {'id': run_id, 'status': outcome['status'], 'input_hash': digest(bundle_text), 'result_hash': digest(outcome_text)}


RUN_COLUMNS = ('id::text, ledger_id::text, proposal_id::text, revision, status, simulator_version, snapshot_seq, input_canonical, input_hash, '
               "outcome_canonical, result_hash, created_by::text, to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")


def load_run(ctx: RequestContext, run_id: UUID):
    rows = ctx.conn.execute(f'SELECT {RUN_COLUMNS} FROM lv.simulation_run WHERE id = %s', (run_id,)).fetchall()
    if len(rows) != 1:
        raise ProblemError(404, 'NOT_FOUND', 'simulation not found')
    return rows[0]


def _verified_bundle(row) -> dict:
    input_text, input_hash = row[7], row[8].strip()
    captured = json.loads(input_text)
    if digest(input_text) != input_hash or canonical(captured) != input_text:
        log.error('stored simulation bundle is not canonical or does not match its hash run_id=%s', row[0])
        raise ProblemError(500, 'INTERNAL', 'stored simulation input does not verify')
    return captured


def replay(ctx: RequestContext, run_id: UUID) -> dict:
    row = load_run(ctx, run_id)
    captured = _verified_bundle(row)
    try:
        outcome = engine.evaluate(captured)
    except engine.VersionUnavailable as missing:
        raise ProblemError(409, 'SIMULATION_VERSION_UNAVAILABLE', 'a pinned version is not installed; the run cannot be reproduced '
                           'here: ' + ', '.join(f"{d['name']} pinned {d['pinned']!r} installed {d['installed']!r}" for d in missing.differences)) from None
    except engine.IntegrityFailure:
        raise ProblemError(500, 'INTERNAL', 'stored simulation input could not be evaluated') from None
    replay_hash = digest(canonical(outcome))
    original = row[10].strip()
    return {'simulation_id': row[0], 'input_hash': row[8].strip(), 'original_hash': original, 'replay_hash': replay_hash,
            'identical': replay_hash == original, 'versions': captured['versions'], 'reloaded_current_state': False}
