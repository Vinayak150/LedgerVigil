"""A small connection pool for the runtime role that refuses privileged logins and never reuses a dirty connection."""
import queue
import threading
from contextlib import contextmanager

from psycopg.pq import TransactionStatus

CONTEXT_SETTINGS = ('app.tenant_id', 'app.user_id', 'app.session_id')


class RuntimeRoleError(RuntimeError):
    pass


def verify_runtime_role(conn, expected_role="lv_app"):
    """The API must run as the restricted lv_app login: not a superuser, not BYPASSRLS, not a schema owner (2A §12.1)."""
    row = conn.execute("SELECT current_user, session_user, r.rolsuper, r.rolbypassrls, "
                       "has_schema_privilege(current_user, 'lv', 'CREATE'), has_schema_privilege(current_user, 'lv_identity', 'CREATE') "
                       "FROM pg_roles r WHERE r.rolname = current_user").fetchone()
    conn.rollback()
    if row != (expected_role, expected_role, False, False, False, False):
        raise RuntimeRoleError(f'connection must use the restricted {expected_role} login')
    if expected_role == 'lv_authn':
        restricted = conn.execute("""
            SELECT NOT (r.rolinherit OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication)
              AND NOT EXISTS (SELECT FROM pg_auth_members m WHERE r.oid IN (m.roleid, m.member))
              AND NOT has_function_privilege('lv_app', 'lv_identity.login_finish(uuid,boolean,bytea,bytea,text,text)', 'EXECUTE')
              AND has_function_privilege(r.oid, 'lv_identity.login_finish(uuid,boolean,bytea,bytea,text,text)', 'EXECUTE')
              FROM pg_roles r WHERE r.rolname = current_user
        """).fetchone()
        conn.rollback()
        if restricted != (True,):
            raise RuntimeRoleError('login issuer privilege boundary is not installed')


class ConnectionPool:
    def __init__(self, connect, max_size: int = 8, *, expected_role="lv_app", reuse=True):
        self._expected_role = expected_role
        self._reuse = reuse
        self._connect, self._idle, self._lock = connect, queue.LifoQueue(), threading.Lock()
        self._slots = threading.BoundedSemaphore(max_size)

    def _new(self):
        conn = self._connect()
        conn.autocommit = False
        try:
            verify_runtime_role(conn, self._expected_role)
        except Exception:
            conn.close()
            raise
        return conn

    @contextmanager
    def connection(self):
        self._slots.acquire()
        conn = None
        try:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                conn = self._new()
            yield conn
        finally:
            try:
                if conn is not None:
                    if self._reuse:
                        self._release(conn)
                    else:
                        conn.close()
            finally:
                self._slots.release()

    def _release(self, conn):
        """Return a connection only if it is idle and carries no tenant, user or session context."""
        try:
            if conn.closed:
                return
            if conn.info.transaction_status != TransactionStatus.IDLE:
                conn.rollback()
            values = conn.execute('SELECT ' + ', '.join(f"current_setting('{name}', true)" for name in CONTEXT_SETTINGS)).fetchone()
            conn.rollback()
            if any(values):
                conn.close()          # a session-level setting survived the transaction: never hand this connection out again
                return
            self._idle.put(conn)
        except Exception:
            conn.close()

    def close(self):
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                return
