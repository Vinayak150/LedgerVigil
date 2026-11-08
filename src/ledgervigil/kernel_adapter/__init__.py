"""Read-only adapter from the native ledger to the existing accounting kernel (internal, unstable).

No HTTP endpoint, proposal, approval, execution or provider contract. Calculation code
(`calculation`, `types`) is pure; persistence loading lives in `loader`.
"""
from .loader import inspect_settlement, load_snapshot, preview_settlement
from .types import AdapterError, DuplicateSettlementRefused

__all__ = ['AdapterError', 'DuplicateSettlementRefused', 'inspect_settlement', 'load_snapshot', 'preview_settlement']
