"""Internal value types for the read-only settlement adapter.

These types are internal and unstable. They are not a public API, a proposal, an approval or an
execution contract. Stage 2B/2C public resources (including the pending D-8 reconciliation contract)
are deliberately not modelled here.
"""
from dataclasses import dataclass
from decimal import Decimal

CONTRACT = 'ledgervigil-internal/settlement-calculation/0 (unstable; not a public API; not a proposal)'


class AdapterError(ValueError):
    """An explicit refusal. `source` says whether the adapter or the kernel refused."""

    def __init__(self, code: str, source: str = 'adapter', detail: str = ''):
        self.code, self.source, self.detail = code, source, detail
        super().__init__(f'{source}:{code}' + (f' ({detail})' if detail else ''))


class DuplicateSettlementRefused(AdapterError):
    """The kernel refused a typed settlement preview as a duplicate (DUPLICATE_SETTLEMENT or ITEM_ALREADY_SETTLED).

    `evidence` is the E-DUP-01 check built from authoritative payout and inclusion state in the same snapshot, and
    `snapshot` identifies that snapshot. This is a preview refusal, not an execution decision.
    """

    def __init__(self, code: str, evidence: dict, snapshot: dict):
        super().__init__(code, 'kernel', evidence['reason'])
        self.evidence, self.snapshot = evidence, snapshot


@dataclass(frozen=True)
class Binding:
    role: str                 # full Stage 2A role name, e.g. PROCESSOR_CLEARING
    account_id: str
    code: str
    type: str
    is_contra: bool
    active: bool
    requires_party: bool
    currency: str
    exponent: int


@dataclass(frozen=True)
class PostedLine:
    line_no: int
    account_id: str
    side: str
    amount: Decimal
    currency: str
    exponent: int
    party_id: str | None


@dataclass(frozen=True)
class PostedEntry:
    entry_id: str
    entry_no: int
    seq: int
    source_document_id: str
    lines: tuple[PostedLine, ...]


@dataclass(frozen=True)
class ProjectionRow:
    account_id: str
    party_id: str | None
    debit_total: Decimal
    credit_total: Decimal
    last_seq: int


@dataclass(frozen=True)
class Period:
    start_date: str           # inclusive, ISO
    end_date: str             # exclusive, ISO (schema convention: [start, end))
    status: str


@dataclass(frozen=True)
class ReferencedDocument:
    source_ref: str
    document_id: str
    doc_type: str


@dataclass(frozen=True)
class StatementEvidence:
    """A VALID staged synthetic statement and its full source trace. An observation, not a ledger fact."""
    staged_document_id: str
    import_batch_id: str
    source_record_id: str
    source_file_id: str
    source_file_sha256: str
    source_file_content: bytes
    declared_format: str
    declared_version: str
    record_content: str
    record_hash: str
    processor: str
    payout: str
    payout_date: str
    currency: str
    exponent: int
    gross: str
    fee: str
    net: str
    items: tuple            # ((line_no, item_ref, kind, amount_text), ...)
    financial_fingerprint: str
    provenance: str


@dataclass(frozen=True)
class EvidenceConflict:
    """A rejected ingestion that claimed the same processor/payout identity with different content."""
    import_batch_id: str
    source_file_sha256: str
    failure_code: str


@dataclass(frozen=True)
class TypedPayment:
    """A typed processor Payment: current version, authoritative processor identity, projection and posting entry."""
    document_id: str
    version: int
    version_id: str
    financial_fingerprint: str
    processor_ref: str
    processor_item_id: str
    party_id: str
    currency: str
    exponent: int
    amount: Decimal
    applied: Decimal
    unapplied: Decimal
    status: str
    identity_seq: int
    entry_id: str
    entry_kind: str
    posting_rule: str
    settlement_document_id: str | None = None   # set when status is SETTLED (2A §11.3 payment_current.settlement)
    entry_version: int = 1                      # document version that produced the (unchanged) posting entry


@dataclass(frozen=True)
class SettlementRef:
    """An authoritative settlement recorded for a (processor_ref, payout_id) identity in this ledger."""
    document_id: str
    version: int
    processor_ref: str
    payout_id: str
    payout_date: str
    gross: Decimal
    fee: Decimal
    net: Decimal
    status: str
    seq: int


@dataclass(frozen=True)
class ActiveInclusion:
    """A row of the active-inclusion projection (I-08) for a requested payment, with its settlement's payout identity."""
    item_document_id: str
    settlement_document_id: str
    settlement_version: int
    processor_ref: str
    payout_id: str
    last_seq: int


@dataclass(frozen=True)
class PaymentIdentity:
    """A persisted (processor_ref, processor_item_id) -> payment document link, in any processor namespace."""
    processor_ref: str
    processor_item_id: str
    document_id: str


@dataclass(frozen=True)
class LedgerSnapshot:
    """Facts read from one REPEATABLE READ, READ ONLY transaction as lv_app under tenant context."""
    tenant_id: str
    ledger_id: str
    purpose: str
    source_kind: str
    currency: str
    exponent: int
    seq: int
    last_entry_no: int
    max_batch_seq: int
    batch_head: str
    event_head: str
    bindings: tuple[Binding, ...]
    periods: tuple[Period, ...]
    entries: tuple[PostedEntry, ...]
    projection: tuple[ProjectionRow, ...]
    documents: tuple[ReferencedDocument, ...]
    read_role: str
    isolation: str
    read_only: bool
    transaction_timestamp: str
    write_xid_assigned: bool
    statement: StatementEvidence | None = None
    statement_conflicts: tuple = ()
    payments: tuple = ()               # TypedPayment for requested document ids and statement-linked identities
    statement_identities: tuple = ()   # PaymentIdentity rows whose item id appears on the statement (any namespace)
    payout_settlement: SettlementRef | None = None   # settlement already recorded for the requested payout identity
    active_inclusions: tuple = ()      # ActiveInclusion rows for requested payment documents
