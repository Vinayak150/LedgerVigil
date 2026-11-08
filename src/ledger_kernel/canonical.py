"""Restricted canonical JSON: ASCII keys/strings, no JSON numeric values.

This intentionally avoids claiming a general RFC 8785 implementation. Decimal
money, dates, versions and sequence numbers are represented as strings.
"""
import hashlib
import json
from .money import DomainError


def canonical(value):
    def check(node):
        if node is None or isinstance(node, bool):
            return
        if isinstance(node, str):
            if not node.isascii():
                raise DomainError("CANONICAL_ASCII_REQUIRED")
            return
        if isinstance(node, list):
            for item in node:
                check(item)
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str):
                    raise DomainError("CANONICAL_STRING_KEY_REQUIRED")
                check(key)
                check(item)
            return
        raise DomainError("CANONICAL_TYPE_UNSUPPORTED")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_load(raw):
    if not isinstance(raw, bytes) or len(raw) > 1_000_000:
        raise DomainError("INPUT_SIZE_OR_TYPE")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DomainError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def number(_):
        raise DomainError("JSON_NUMBERS_NOT_ALLOWED")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_int=number, parse_float=number, parse_constant=number)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DomainError("INVALID_JSON") from exc
