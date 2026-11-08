"""Independent native review: ambient Decimal state must not alter deterministic replay (2A §3, §10.2)."""
from decimal import Inexact, Rounded, ROUND_DOWN, getcontext, localcontext
import pytest
from ledgervigil.simulation import engine
from ledgervigil.simulation.canonical import canonical
from test_proposal_drafts import seed, world, signed_in  # noqa: F401
from test_simulation import api, new_draft, simulate, _bundle  # noqa: F401


def context_state():
    c = getcontext()
    return (c.prec, c.rounding, c.Emin, c.Emax, c.capitals, c.clamp, dict(c.traps), dict(c.flags))


@pytest.mark.parametrize('precision', [2, 4, 5, 28, 50])
def test_replay_is_independent_of_ambient_decimal_context(database, api, world, precision):
    analyst = signed_in(api, world, 'analyst')
    result = simulate(analyst, new_draft(analyst, world))
    assert result.status_code == 200, result.text
    bundle = _bundle(database, result.json()['id'])
    expected = canonical(engine.evaluate(bundle))
    with localcontext() as ambient:
        ambient.prec = precision
        ambient.rounding = ROUND_DOWN
        ambient.traps[Inexact] = True
        ambient.traps[Rounded] = True
        ambient.clear_flags()
        before = context_state()
        actual = canonical(engine.evaluate(bundle))
        assert actual == expected
        assert context_state() == before, 'simulation leaked Decimal flags or settings into its caller'


def test_decimal_context_restored_after_version_refusal():
    with localcontext() as ambient:
        ambient.prec = 4
        ambient.rounding = ROUND_DOWN
        ambient.traps[Inexact] = True
        ambient.clear_flags()
        before = context_state()
        with pytest.raises(engine.VersionUnavailable):
            engine.evaluate({'versions': {'simulator': 'not-installed'}})
        assert context_state() == before
