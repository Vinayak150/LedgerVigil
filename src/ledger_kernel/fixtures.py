"""Synthetic fixture adapter. Never use this loader for BOOKS ingestion."""
from pathlib import Path
import json
from .canonical import strict_load
from .evidence import parse_statement
from .model import Account, Entry, Item, Line, Settlement, Snapshot
from .money import Currency, DomainError, Money


def load_fixture(path):
    data = strict_load(Path(path).read_bytes())
    if data["dataset_provenance"] != "SYNTHETIC" or data["purpose"] != "SANDBOX":
        raise DomainError("SYNTHETIC_SANDBOX_REQUIRED")
    currency = Currency(data["currency"], int(data["exponent"]))
    money = lambda x: Money.parse(x, currency)
    entries = tuple(Entry(tuple(Line(l["account"], l["side"], money(l["amount"]), l.get("party")) for l in e)) for e in data["entries"])
    accounts = tuple(Account(a["id"], a["role"]) for a in data["accounts"])
    items = tuple(Item(i["id"], i["kind"], i["processor"], money(i["amount"])) for i in data["items"])
    snapshot = Snapshot(data["tenant"], data["ledger"], data["sequence"], currency, accounts, entries, items)
    c = data["settlement"]
    command = Settlement(snapshot.tenant, snapshot.ledger, c["processor"], c["payout"], c["date"], tuple(c["items"]), money(c["gross"]), money(c["fee"]), money(c["net"]))
    # SOURCE is the statement's role inside a synthetic scenario, not a claim
    # that the fixture is a genuine bank/processor statement.
    statement = parse_statement(json.dumps(data["statement"], sort_keys=True).encode(), currency, provenance="SOURCE")
    return snapshot, command, statement
