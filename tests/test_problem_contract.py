"""D1 regression: the owning OpenAPI must carry the accepted Stage 2B Part 3 Problem schema.

Stage 2B (accepted copy 20261102-132232-claude, Part 3 components) defines `Problem` with required
[type, title, status, code, class, retryable, request_id], a closed `class` enum and `errors[]` items of
{pointer, code}; Part 4 and the UI contract tell clients to map by `code` and show `errors[]`. Before the
correction every `application/problem+json` response in the generated document had no schema at all, so the
generated TypeScript client could not type any error body. These tests fail against 0029 and pass after D1.
"""
import json
from pathlib import Path

from tools.dev_db import connection
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings

ROOT = Path(__file__).resolve().parents[1]
CLASSES = ['VALIDATION', 'AUTHENTICATION', 'AUTHORIZATION', 'NOT_FOUND', 'CONFLICT', 'STALE', 'PRECONDITION', 'UNSUPPORTED',
           'RATE_LIMITED', 'INFRASTRUCTURE']
REQUIRED = ['type', 'title', 'status', 'code', 'class', 'retryable', 'request_id']


def _problem_contents(spec):
    for path, ops in spec['paths'].items():
        for method, op in ops.items():
            for status, response in op.get('responses', {}).items():
                content = response.get('content', {}).get('application/problem+json')
                if content is not None:
                    yield f'{method.upper()} {path} {status}', content


def test_every_problem_response_references_the_accepted_problem_schema():
    spec = create_app(AuthSettings()).openapi()
    contents = list(_problem_contents(spec))
    assert contents, 'no problem responses found'
    untyped = [name for name, content in contents if content.get('schema') != {'$ref': '#/components/schemas/Problem'}]
    assert untyped == [], f'{len(untyped)} of {len(contents)} problem responses have no Problem schema, e.g. {untyped[:3]}'


def test_problem_component_matches_stage_2b_part_3():
    schema = create_app(AuthSettings()).openapi().get('components', {}).get('schemas', {}).get('Problem')
    assert schema is not None, 'components.schemas.Problem is missing'
    assert schema['type'] == 'object' and sorted(schema['required']) == sorted(REQUIRED)
    props = schema['properties']
    assert props['class']['enum'] == CLASSES
    assert props['errors']['items']['properties'] == {'pointer': {'type': 'string'}, 'code': {'type': 'string'}}
    assert props['status'] == {'type': 'integer'} and props['retryable'] == {'type': 'boolean'}
    assert 'additionalProperties' not in schema  # documented extensions (state, reason) stay permitted


def test_committed_contract_and_frontend_snapshot_carry_the_schema():
    committed = json.loads((ROOT / 'api/openapi.json').read_text())
    assert 'Problem' in committed.get('components', {}).get('schemas', {})
    assert (ROOT / 'web/openapi.json').read_bytes() == (ROOT / 'api/openapi.json').read_bytes()
    generated_ts = (ROOT / 'web/src/lib/api.generated.d.ts').read_text()
    assert 'Problem: {' in generated_ts and 'retryable: boolean' in generated_ts


def _conforms(body):
    missing = [k for k in REQUIRED if k not in body]
    assert not missing, (missing, body)
    assert isinstance(body['status'], int) and isinstance(body['retryable'], bool) and isinstance(body['request_id'], str)
    assert body['class'] in CLASSES and isinstance(body['code'], str) and isinstance(body['title'], str)
    assert body['type'].startswith('urn:ledgervigil:problem:')
    for e in body.get('errors', []):
        assert set(e) <= {'pointer', 'code'} and all(isinstance(v, str) for v in e.values()), e


def test_runtime_problem_bodies_conform_to_the_schema(database):
    from fastapi.testclient import TestClient
    app = create_app(AuthSettings(), connect=lambda: connection('lv_app', database))
    try:
        c = TestClient(app, base_url='http://127.0.0.1')
        samples = [c.get('/api/v1/me'), c.get('/api/v1/ledgers'), c.get('/api/v1/nowhere'),
                   c.post('/api/v1/auth/login', json={'email': 1})]
        for r in samples:
            assert r.headers['content-type'].startswith('application/problem+json'), r.text
            _conforms(r.json())
        assert any('errors' in r.json() for r in samples)
    finally:
        app.state.pool.close()
