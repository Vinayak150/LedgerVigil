"""Strict parser for this lab's statement format, not a provider adapter."""
from dataclasses import dataclass
import hashlib
from .canonical import strict_load
from .model import iso_date, token
from .money import DomainError, Money


@dataclass(frozen=True)
class Statement:
    tenant: str
    ledger: str
    processor: str
    payout: str
    date: str
    items: tuple[tuple[str, str, Money], ...]
    gross: Money
    fee: Money
    net: Money
    provenance: str
    raw_sha256: str

    def wire(self):
        return {"tenant": self.tenant, "ledger": self.ledger, "processor": self.processor, "payout": self.payout,
                "date": self.date, "items": [{"id": i, "kind": k, "amount": m.wire()} for i, k, m in sorted(self.items)],
                "gross": self.gross.wire(), "fee": self.fee.wire(), "net": self.net.wire(),
                "provenance": self.provenance, "raw_sha256": self.raw_sha256}


def parse_statement(raw: bytes, currency, *, provenance: str):
    """Provenance comes from trusted ingestion metadata, not uploaded JSON."""
    if provenance not in ("SOURCE", "DERIVED", "INFERRED", "SYNTHETIC"):
        raise DomainError("PROVENANCE_INVALID")
    data = strict_load(raw)
    keys = {"tenant", "ledger", "processor", "payout", "date", "currency", "items", "gross", "fee", "net", "memo"}
    if not isinstance(data, dict) or set(data) != keys:
        raise DomainError("STATEMENT_FIELDS")
    for key in ("tenant", "ledger", "processor", "payout"):
        token(data[key])
    iso_date(data["date"])
    if data["currency"] != currency.code:
        raise DomainError("CURRENCY_MISMATCH")
    if not isinstance(data["memo"], str) or len(data["memo"]) > 4096:
        raise DomainError("MEMO_INVALID")
    if not isinstance(data["items"], list) or not 1 <= len(data["items"]) <= 1000:
        raise DomainError("STATEMENT_ITEMS")
    items = []
    for item in data["items"]:
        if not isinstance(item, dict) or set(item) != {"id", "kind", "amount"} or item["kind"] not in ("PAYMENT", "REFUND"):
            raise DomainError("STATEMENT_ITEM_INVALID")
        token(item["id"])
        amount = Money.parse(item["amount"], currency)
        if amount.value <= 0:
            raise DomainError("STATEMENT_ITEM_AMOUNT")
        items.append((item["id"], item["kind"], amount))
    if len({i[0] for i in items}) != len(items):
        raise DomainError("STATEMENT_ITEM_DUPLICATE")
    gross, fee, net = (Money.parse(data[k], currency) for k in ("gross", "fee", "net"))
    if gross.value <= 0 or fee.value < 0 or net.value < 0 or gross != fee + net:
        raise DomainError("STATEMENT_TOTALS")
    # Memo is stored only in the raw bytes; it never affects rules or instructions.
    return Statement(data["tenant"], data["ledger"], data["processor"], data["payout"], data["date"],
                     tuple(sorted(items)), gross, fee, net, provenance, hashlib.sha256(raw).hexdigest())
