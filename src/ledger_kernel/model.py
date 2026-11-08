"""Immutable domain inputs. They are supplied by a future authenticated read service."""
from dataclasses import dataclass
from datetime import date
import re
from .money import Currency, DomainError, Money, total, zero

ROLES = ("BANK", "AR", "CLR", "OBE", "REV", "RET", "FEE")
ROLE_TYPES = {"BANK": "ASSET", "AR": "ASSET", "CLR": "ASSET", "OBE": "EQUITY", "REV": "REVENUE", "RET": "REVENUE", "FEE": "EXPENSE"}


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        raise DomainError("IDENTIFIER_INVALID")
    return value


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DomainError("DATE_INVALID")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DomainError("DATE_INVALID") from exc


@dataclass(frozen=True)
class Account:
    id: str
    role: str
    active: bool = True
    type: str = ""

    def __post_init__(self):
        token(self.id)
        if self.role not in ROLES or type(self.active) is not bool:
            raise DomainError("ACCOUNT_INVALID")
        if self.type == "":
            object.__setattr__(self, "type", ROLE_TYPES[self.role])
        if self.type not in ("ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"):
            raise DomainError("ACCOUNT_TYPE_INVALID")


@dataclass(frozen=True)
class Line:
    account: str
    side: str
    amount: Money
    party: str | None = None

    def __post_init__(self):
        token(self.account)
        if self.party is not None:
            token(self.party)
        if self.side not in ("D", "C") or not isinstance(self.amount, Money) or self.amount.value <= 0:
            raise DomainError("LINE_INVALID")

    @property
    def signed(self):
        return self.amount if self.side == "D" else -self.amount

    def wire(self):
        return {"account": self.account, "side": self.side, "amount": self.amount.wire(), "party": self.party}


@dataclass(frozen=True)
class Entry:
    lines: tuple[Line, ...]

    def __post_init__(self):
        if type(self.lines) is not tuple or len(self.lines) < 2 or not all(isinstance(x, Line) for x in self.lines):
            raise DomainError("ENTRY_LINES_INVALID")
        currency = self.lines[0].amount.currency
        if any(x.amount.currency != currency for x in self.lines):
            raise DomainError("CURRENCY_MISMATCH")

    @property
    def balanced(self):
        return total((x.signed for x in self.lines), self.lines[0].amount.currency).value == 0

    def wire(self):
        # Preserve order: journal line numbers carry meaning.
        return [x.wire() for x in self.lines]


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    processor: str
    amount: Money
    version: str = "1"
    settled: bool = False
    voided: bool = False

    def __post_init__(self):
        for s in (self.id, self.processor, self.version):
            token(s)
        if self.kind not in ("PAYMENT", "REFUND") or self.amount.value <= 0 or type(self.settled) is not bool or type(self.voided) is not bool:
            raise DomainError("ITEM_INVALID")

    @property
    def clearing(self):
        return self.amount if self.kind == "PAYMENT" else -self.amount

    def wire(self):
        return {"id": self.id, "kind": self.kind, "processor": self.processor, "amount": self.amount.wire(), "version": self.version, "settled": self.settled, "voided": self.voided}


@dataclass(frozen=True)
class Snapshot:
    tenant: str
    ledger: str
    sequence: str
    currency: Currency
    accounts: tuple[Account, ...]
    entries: tuple[Entry, ...]
    items: tuple[Item, ...]
    used_payouts: tuple[tuple[str, str], ...] = ()
    closed_months: tuple[str, ...] = ()

    def __post_init__(self):
        for value in (self.tenant, self.ledger, self.sequence):
            token(value)
        for value in (self.accounts, self.entries, self.items, self.used_payouts, self.closed_months):
            if type(value) is not tuple:
                raise DomainError("IMMUTABLE_TUPLE_REQUIRED")
        if len({a.id for a in self.accounts}) != len(self.accounts) or len({a.role for a in self.accounts}) != len(self.accounts):
            raise DomainError("ACCOUNT_BINDINGS_DUPLICATE")
        if set(a.role for a in self.accounts) != set(ROLES):
            raise DomainError("ACCOUNT_BINDINGS_INCOMPLETE")
        if len({i.id for i in self.items}) != len(self.items):
            raise DomainError("ITEM_ID_DUPLICATE")
        for month in self.closed_months:
            iso_date(month + "-01")
        for payout in self.used_payouts:
            if type(payout) is not tuple or len(payout) != 2:
                raise DomainError("PAYOUT_ID_INVALID")
            for value in payout:
                token(value)
        for entry in self.entries:
            if not entry.balanced:
                raise DomainError("SNAPSHOT_UNBALANCED")
            for line in entry.lines:
                account = self.by_id(line.account)
                if line.amount.currency != self.currency:
                    raise DomainError("CURRENCY_MISMATCH")
                if account.role == "AR" and line.party is None:
                    raise DomainError("AR_PARTY_REQUIRED")
        if any(i.amount.currency != self.currency for i in self.items):
            raise DomainError("CURRENCY_MISMATCH")

    def by_id(self, account_id):
        for account in self.accounts:
            if account.id == account_id:
                return account
        raise DomainError("UNKNOWN_ACCOUNT")

    def by_role(self, role):
        return next(a for a in self.accounts if a.role == role)

    def balances(self, extra=()):
        values = {a.id: zero(self.currency) for a in self.accounts}
        for entry in (*self.entries, *extra):
            for line in entry.lines:
                self.by_id(line.account)
                values[line.account] = values[line.account] + line.signed
        return values

    def wire(self):
        return {"tenant": self.tenant, "ledger": self.ledger, "sequence": self.sequence,
                "currency": {"code": self.currency.code, "exponent": str(self.currency.exponent)},
                "accounts": [{"id": a.id, "role": a.role, "active": a.active, "type": a.type} for a in sorted(self.accounts, key=lambda a: a.id)],
                "entries": [e.wire() for e in self.entries],
                "items": [i.wire() for i in sorted(self.items, key=lambda i: i.id)],
                "used_payouts": [list(p) for p in sorted(self.used_payouts)], "closed_months": sorted(self.closed_months)}


@dataclass(frozen=True)
class Settlement:
    tenant: str
    ledger: str
    processor: str
    payout: str
    date: str
    items: tuple[str, ...]
    gross: Money
    fee: Money
    net: Money

    def __post_init__(self):
        for v in (self.tenant, self.ledger, self.processor, self.payout):
            token(v)
        iso_date(self.date)
        if type(self.items) is not tuple or not self.items or len(self.items) > 1000:
            raise DomainError("ITEMS_INVALID")
        for value in self.items:
            token(value)
        self.gross._same(self.fee)
        self.gross._same(self.net)

    def wire(self):
        return {"tenant": self.tenant, "ledger": self.ledger, "processor": self.processor, "payout": self.payout,
                "date": self.date, "items": sorted(self.items), "gross": self.gross.wire(), "fee": self.fee.wire(), "net": self.net.wire()}
