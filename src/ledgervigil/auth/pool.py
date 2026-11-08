"""A small connection pool for the runtime role that refuses privileged logins and never reuses a dirty connection."""
import queue
import threading
from contextlib import contextmanager

from psycopg.pq import TransactionStatus

CONTEXT_SETTINGS = ('app.tenant_id', 'app.user_id', 'app.session_id')


class RuntimeRoleError(RuntimeError):
    pass


def verify_runtime_role(conn):
    """The API must run as the restricted lv_app login: not a superuser, not BYPASSRLS, not a schema owner (2A §12.1)."""
    row = conn.execute("SELECT current_user, session_user, r.rolsuper, r.rolbypassrls, "
                       "has_schema_privilege(current_user, 'lv', 'CREATE'), has_schema_privilege(current_user, 'lv_identity', 'CREATE') "
                       "FROM pg_roles r WHERE r.rolname = current_user").fetchone()
    conn.rollback()
    if row != ('lv_app', 'lv_app', False, False, False, False):
        raise RuntimeRoleError('the API must connect as the restricted lv_app role')


class ConnectionPool:
    def __init__(self, connect, max_size: int = 8):
        self._connect, self._idle, self._lock = connect, queue.LifoQueue(), threading.Lock()
        self._slots = threading.BoundedSemaphore(max_size)

    def _new(self):
        conn = self._connect()
        conn.autocommit = False
        try:
            verify_runtime_role(conn)
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
            if conn is not None:
                self._release(conn)
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
