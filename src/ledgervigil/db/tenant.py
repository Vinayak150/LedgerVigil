from contextlib import contextmanager
from uuid import UUID
from psycopg.pq import TransactionStatus

@contextmanager
def tenant_transaction(connection, tenant_id: UUID):
    """Bind one authenticated tenant to one entire transaction.

    Internal boundary only: the caller must derive this UUID from trusted session
    or job context, never from a client-provided request field. A DB credential
    alone is not tenant identity; RLS guards queries, not a compromised service.
    """
    if not isinstance(tenant_id, UUID):
        raise TypeError('tenant_id must be a validated UUID')
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError('tenant context requires a fresh transaction')
    with connection.transaction():
        connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        yield connection
