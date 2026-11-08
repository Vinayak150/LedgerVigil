"""Pure settlement derivation and diagnostic evaluation; no state transitions."""
from dataclasses import dataclass
from .canonical import digest
from .model import Entry, Line, ROLE_TYPES
from .money import DomainError, total, zero

RULE_VERSION = "sp1-posting/1.0:settlement-slice"
CHECK_VERSION = "sp1-policies/1.1:settlement-slice"


def selected_items(snapshot, command):
    if (snapshot.tenant, snapshot.ledger) != (command.tenant, command.ledger):
        raise DomainError("SCOPE_MISMATCH")
    if command.gross.currency != snapshot.currency:
        raise DomainError("CURRENCY_MISMATCH")
    if len(set(command.items)) != len(command.items):
        raise DomainError("DUPLICATE_ITEM")
    available = {i.id: i for i in snapshot.items}
    if any(i not in available for i in command.items):
        raise DomainError("ITEM_NOT_FOUND")
    return tuple(available[i] for i in sorted(command.items))


def preconditions(snapshot, command):
    items = selected_items(snapshot, command)
    if (command.processor, command.payout) in snapshot.used_payouts:
        raise DomainError("DUPLICATE_SETTLEMENT")
    if any(i.settled for i in items):
        raise DomainError("ITEM_ALREADY_SETTLED")
    if any(i.voided for i in items):
        raise DomainError("ITEM_VOIDED")
    if any(i.processor != command.processor for i in items):
        raise DomainError("PROCESSOR_MISMATCH")
    if command.date[:7] in snapshot.closed_months:
        raise DomainError("PERIOD_CLOSED")
    if command.gross.value <= 0 or command.fee.value < 0 or command.net.value < 0 or command.gross != command.fee + command.net:
        raise DomainError("SETTLEMENT_TOTALS")
    if command.gross != total((i.clearing for i in items), snapshot.currency):
        raise DomainError("ITEM_TOTAL_MISMATCH")
    for role in ("BANK", "FEE", "CLR"):
        if not snapshot.by_role(role).active:
            raise DomainError("INACTIVE_ACCOUNT")
        if snapshot.by_role(role).type != ROLE_TYPES[role]:
            raise DomainError("ACCOUNT_ROLE_TYPE_MISMATCH")
    return items


def derive_settlement(snapshot, command):
    preconditions(snapshot, command)
    lines = []
    for role, side, amount in (("BANK", "D", command.net), ("FEE", "D", command.fee), ("CLR", "C", command.gross)):
        if amount.value > 0:
            lines.append(Line(snapshot.by_role(role).id, side, amount))
    return Entry(tuple(lines))


@dataclass(frozen=True)
class Check:
    id: str
    verdict: str
    reason: str

    def wire(self):
        return {"id": self.id, "verdict": self.verdict, "reason": self.reason}


@dataclass(frozen=True)
class Report:
    input_hash: str
    snapshot_hash: str
    entry: Entry
    checks: tuple[Check, ...]
    before: tuple[tuple[str, str], ...]
    after: tuple[tuple[str, str], ...]
    settlement_clearing_residual: str

    @property
    def outcome(self):
        return "BLOCKED" if any(c.verdict in ("FAIL", "INSUFFICIENT_EVIDENCE") for c in self.checks) else "SUPPORTED_CHECKS_PASS"

    def payload(self):
        return {"format": "ledgervigil-settlement-review/1", "purpose": "SANDBOX_REVIEW_ONLY",
                "rule_version": RULE_VERSION, "check_version": CHECK_VERSION,
                "input_hash": self.input_hash, "snapshot_hash": self.snapshot_hash,
                "entry": self.entry.wire(), "checks": [c.wire() for c in self.checks],
                "before": dict(self.before), "after": dict(self.after),
                "settlement_clearing_residual": self.settlement_clearing_residual,
                "outcome": self.outcome, "eligible_for_execution": False,
                "accounting_semantics": "UNREVIEWED", "external_evidence_authenticity": "NOT_VERIFIED"}

    def wire(self):
        payload = self.payload()
        return {"payload": payload, "sha256": digest(payload)}


def inspect_settlement_entry(snapshot, command, entry, statement=None):
    """Read-only diagnostics for proposed lines, including deliberately bad entries.

    Unlike typed simulation this permits duplicate/closed-period proposals so
    their failed checks can be explained. Callers must not use this as execution.
    """
    items = selected_items(snapshot, command)
    if entry.lines[0].amount.currency != snapshot.currency:
        raise DomainError("CURRENCY_MISMATCH")
    for line in entry.lines:
        snapshot.by_id(line.account)
    before = snapshot.balances()
    after = snapshot.balances((entry,))
    changes = {a.id: zero(snapshot.currency) for a in snapshot.accounts}
    for line in entry.lines:
        changes[line.account] = changes[line.account] + line.signed
    amount = lambda role: changes[snapshot.by_role(role).id]
    residual = total((i.clearing for i in items), snapshot.currency) + amount("CLR")
    touched = {snapshot.by_id(l.account).role for l in entry.lines}
    checks = []
    def add(id, ok, reason, *, applicable=True):
        checks.append(Check(id, ("PASS" if ok else "FAIL") if applicable else "NOT_APPLICABLE", reason))
    add("P-BAL-01", entry.balanced, "Debits equal credits.")
    add("P-ACC-01", all(snapshot.by_id(l.account).active and snapshot.by_id(l.account).type == ROLE_TYPES[snapshot.by_id(l.account).role] and (snapshot.by_id(l.account).role != "AR" or l.party is not None) for l in entry.lines), "Bound, active accounts of the required type; AR requires a party.")
    add("P-CUR-01", True, "Exact scale and functional currency checked at construction.")
    add("P-REV-01", not touched.intersection({"REV", "RET"}), "Settlement must not touch revenue or sales returns.", applicable=bool(touched.intersection({"REV", "RET"})))
    if statement is None or statement.provenance != "SOURCE":
        checks.append(Check("P-CLR-01", "INSUFFICIENT_EVIDENCE" if residual.value == 0 else "FAIL", "Included items must clear; SOURCE statement also required."))
    else:
        add("P-CLR-01", residual.value == 0, "Selected item clearing movements plus settlement clearing must be zero.")
    add("P-CLR-02", after[snapshot.by_role("CLR").id].value >= 0, "Total processor clearing must not end in credit.", applicable="CLR" in touched)
    add("P-DUP-01", (command.processor, command.payout) not in snapshot.used_payouts and not any(i.settled for i in items), "Payout and included items must be unused.")
    add("P-PER-01", command.date[:7] not in snapshot.closed_months, "Payout date is in an open period.")
    # Implementation guard, not a new accepted SP-1 policy: exact signed vector
    # prevents a balanced wrong-bank or extra-account entry passing this slice.
    expected = {a.id: zero(snapshot.currency) for a in snapshot.accounts}
    for role, value in (("BANK", command.net), ("FEE", command.fee), ("CLR", -command.gross)):
        expected[snapshot.by_role(role).id] = value
    amount_contract = (command.gross.value > 0 and command.fee.value >= 0 and command.net.value >= 0
                       and command.gross == command.fee + command.net
                       and command.gross == total((i.clearing for i in items), snapshot.currency))
    add("K-SETTLEMENT-POSTINGS", changes == expected and amount_contract and not any(i.voided or i.processor != command.processor for i in items), "Local guard: signed posting vector and selected items equal the typed settlement contract.")
    if statement is None or statement.provenance != "SOURCE":
        for id in ("P-FEE-01", "P-STL-01", "P-EVD-01"):
            checks.append(Check(id, "INSUFFICIENT_EVIDENCE", "An ingested SOURCE statement is required; no override implemented."))
    else:
        scope_ok = (statement.tenant, statement.ledger, statement.processor, statement.payout, statement.date) == (command.tenant, command.ledger, command.processor, command.payout, command.date)
        items_ok = statement.items == tuple(sorted((i.id, i.kind, i.amount) for i in items))
        checks.append(Check("P-EVD-01", "PASS" if scope_ok else "INSUFFICIENT_EVIDENCE", "SOURCE evidence for this payout and scope is required."))
        add("P-FEE-01", command.fee == statement.gross - statement.net and amount("FEE") == command.fee, "Recorded fee equals statement gross less net.", applicable=command.fee.value > 0 or "FEE" in touched)
        add("P-STL-01", scope_ok and items_ok and (command.gross, command.fee, command.net) == (statement.gross, statement.fee, statement.net), "Gross, fee, net, identity and full item list equal the statement.")
    inputs = {"snapshot": snapshot.wire(), "command": command.wire(), "entry": entry.wire(), "statement": statement.wire() if statement else None,
              "rule_version": RULE_VERSION, "check_version": CHECK_VERSION}
    return Report(digest(inputs), digest(snapshot.wire()), entry, tuple(sorted(checks, key=lambda c: c.id)),
                  tuple(sorted((k, v.text()) for k, v in before.items())), tuple(sorted((k, v.text()) for k, v in after.items())), residual.text())


def simulate_settlement(snapshot, command, statement=None):
    return inspect_settlement_entry(snapshot, command, derive_settlement(snapshot, command), statement)


def verify_report(envelope):
    """Checks internal consistency only; a hash is not a signature or provenance."""
    return isinstance(envelope, dict) and set(envelope) == {"payload", "sha256"} and digest(envelope["payload"]) == envelope["sha256"]
