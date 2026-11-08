"""Canonical JSON for simulation bundles and outcomes.

Sorted keys, no insignificant whitespace, ASCII output (non-ASCII escaped as \\uXXXX, so source text of any script hashes
identically on every machine), and a restricted value space: str, int, bool, None, list, dict with str keys. Floats are
refused: money and dates are always strings. This is deliberately not a general RFC 8785 implementation (the kernel's own
canonical form is ASCII-only and refuses non-ASCII text; source descriptions here may contain any Unicode).
"""
import hashlib
import json


class CanonicalError(ValueError):
    pass


def _check(node, depth=0):
    if depth > 64:
        raise CanonicalError('too deep')
    if node is None or isinstance(node, (bool, str)):
        return
    if isinstance(node, int):
        if abs(node) >= 2 ** 63:
            raise CanonicalError('integer out of range')
        return
    if isinstance(node, list):
        for item in node:
            _check(item, depth + 1)
        return
    if isinstance(node, dict):
        for key, item in node.items():
            if not isinstance(key, str):
                raise CanonicalError('non-string key')
            _check(item, depth + 1)
        return
    raise CanonicalError(f'unsupported type {type(node).__name__}')


def canonical(value) -> str:
    _check(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
