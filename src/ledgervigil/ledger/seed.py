from uuid import UUID
from psycopg.types.json import Jsonb
from ledgervigil.db.tenant import tenant_transaction

def seed_manual_journal(connection, tenant_id: UUID, ledger_id: UUID, request: dict):
    """Commit one synthetic manual journal using the dedicated developer login.

    The database owns validation and permissions. This function does not expose
    an HTTP endpoint, implement an approved proposal, or accept a caller actor.
    """
    if not isinstance(ledger_id, UUID): raise TypeError('ledger_id must be a UUID')
    with tenant_transaction(connection,tenant_id):
        result=connection.execute('SELECT lv.seed_manual_journal(%s,%s)',(ledger_id,Jsonb(request))).fetchone()[0]
    # Deferred constraints have now passed and the transaction has committed.
    return result


def seed_processor_payment(connection, tenant_id: UUID, ledger_id: UUID, request: dict):
    """Commit one synthetic typed processor Payment (SANDBOX only) using the developer seed login.

    The database derives Dr PROCESSOR_CLEARING / Cr AR_CONTROL (customer) from the payment under rule
    sp1-posting/1.0, from the ledger's actual role bindings. The actor comes from the session login.
    This is fixture seeding, not an approved execution, and it exposes no endpoint.
    """
    if not isinstance(ledger_id, UUID): raise TypeError('ledger_id must be a UUID')
    with tenant_transaction(connection,tenant_id):
        result=connection.execute('SELECT lv.seed_processor_payment(%s,%s)',(ledger_id,Jsonb(request))).fetchone()[0]
    return result


def seed_processor_settlement(connection, tenant_id: UUID, ledger_id: UUID, request: dict):
    """Commit one synthetic typed processor Settlement (SANDBOX only) using the developer seed login.

    The database checks every included typed Payment (same tenant, ledger and processor; RECEIVED; full amount; not in an
    active settlement), derives Dr BANK net / Dr FEE fee / Cr CLR gross (zero lines omitted) under sp1-posting/1.0, writes
    the inclusion facts and a new SETTLED version of each Payment, and refuses or replays a repeated payout identity.
    This is fixture seeding, not an approved execution, and it exposes no endpoint.
    """
    if not isinstance(ledger_id, UUID): raise TypeError('ledger_id must be a UUID')
    with tenant_transaction(connection,tenant_id):
        result=connection.execute('SELECT lv.seed_processor_settlement(%s,%s)',(ledger_id,Jsonb(request))).fetchone()[0]
    return result
