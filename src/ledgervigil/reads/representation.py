"""Response rendering with validators for read routes (Stage 2B Part 10 "Freshness": ETag with If-None-Match, 304 when unchanged).

The body is validated against its declared response model, rendered ONCE to bytes, and the strong ETag is the SHA-256 of
exactly those bytes, so equal ETags mean byte-identical representations. These ETags are representation validators for GET
only (prefix `r`); they are not resource versions ("v<n>") and are never accepted as `If-Match` by any mutation.

`If-None-Match` is evaluated only inside the route, i.e. after the session boundary and the permission check have passed
and the representation has been read under row-level security: a 304 is never an answer to an unauthenticated or
unauthorized request, and never says anything about another tenant's data.
"""
import hashlib
import json

from fastapi import Request, Response
from pydantic import BaseModel


def render(request: Request, model: type[BaseModel], body: dict, etag: str | None = None) -> Response:
    """etag=None: representation hash ("r..."). A resource with a version passes its own validator ('"v<n>"')."""
    model.model_validate(body)                       # the declared contract is enforced at runtime; failure is a 500
    content = json.dumps(body, separators=(',', ':'), ensure_ascii=False).encode()
    etag = etag or '"r' + hashlib.sha256(content).hexdigest()[:40] + '"'
    if _matches(request.headers.get('if-none-match'), etag):
        return Response(status_code=304, headers={'ETag': etag})
    return Response(content=content, media_type='application/json', headers={'ETag': etag})


def _matches(header: str | None, etag: str) -> bool:
    """RFC 9110 §13.1.2: weak comparison; '*' matches any current representation."""
    if not header or len(header) > 2048:
        return False
    for candidate in header.split(','):
        candidate = candidate.strip()
        if candidate == '*':
            return True
        if candidate.startswith('W/'):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False
