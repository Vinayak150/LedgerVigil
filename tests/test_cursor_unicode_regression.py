"""Regression: every legal 120-character ledger name must produce a usable cursor."""
import pytest
from ledgervigil.reads.paging import CursorCodec, list_context

@pytest.mark.parametrize('name', ['A'*120, '界'*120, '😀'*120])
def test_legal_ledger_names_round_trip_cursor(name):
    codec = CursorCodec(b'x'*32)
    context = list_context('listLedgers', 'tenant', None, 'name:C,id')
    position = [name, '00000000-0000-0000-0000-000000000001']
    assert codec.decode(context, codec.encode(context, position), 2) == position
