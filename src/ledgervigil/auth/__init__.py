"""Authentication, verified tenant context and server-side permissions (ADR-015, ADR-027, Stage 2B Part 5).

FastAPI is the only authentication authority. Sessions are server-side rows (lv_identity.session) referenced by an
HttpOnly cookie that carries a random token; only its SHA-256 is stored. Every authenticated request resolves the token
inside its own database transaction (lv_identity.session_context), which binds that transaction's tenant and user from
the session row. Client-supplied tenant or user values are never authorization.
"""
