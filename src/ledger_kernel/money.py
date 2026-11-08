"""Exact input validation and explicitly rounded computations (2A §3)."""
from dataclasses import dataclass
from decimal import Context, Decimal, DivisionByZero, InvalidOperation, ROUND_HALF_UP, localcontext
import re


class DomainError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


CONTEXT = Context(prec=34, rounding=ROUND_HALF_UP, traps=[InvalidOperation, DivisionByZero])
WIRE = re.compile(r"-?[0-9]{1,16}(?:\.[0-9]{1,4})?\Z")


@dataclass(frozen=True, order=True)
class Currency:
    code: str
    exponent: int

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z]{3}", self.code) or type(self.exponent) is not int or not 0 <= self.exponent <= 4:
            raise DomainError("CURRENCY_INVALID")

    @property
    def quantum(self):
        return Decimal((0, (1,), -self.exponent))


INR, USD, GBP = Currency("INR", 2), Currency("USD", 2), Currency("GBP", 2)


@dataclass(frozen=True)
class Money:
    value: Decimal
    currency: Currency

    def __post_init__(self):
        if not isinstance(self.value, Decimal) or not isinstance(self.currency, Currency):
            raise DomainError("EXACT_DECIMAL_REQUIRED")
        with localcontext(CONTEXT):
            if not self.value.is_finite() or abs(self.value) >= Decimal("10000000000000000"):
                raise DomainError("MONEY_RANGE")
            q = self.value.quantize(self.currency.quantum)
            if q != self.value:
                raise DomainError("SUB_MINOR_UNIT")
            object.__setattr__(self, "value", abs(q) if q == 0 else q)

    @classmethod
    def parse(cls, raw: str, currency: Currency):
        if not isinstance(raw, str) or not WIRE.fullmatch(raw):
            raise DomainError("MONEY_WIRE_FORMAT")
        if "." in raw and len(raw.split(".")[1]) > currency.exponent:
            raise DomainError("MONEY_WIRE_SCALE")
        return cls(Decimal(raw), currency)

    def _same(self, other):
        if not isinstance(other, Money) or other.currency != self.currency:
            raise DomainError("CURRENCY_MISMATCH")

    def __add__(self, other):
        self._same(other)
        with localcontext(CONTEXT):
            return Money(self.value + other.value, self.currency)

    def __sub__(self, other):
        self._same(other)
        with localcontext(CONTEXT):
            return Money(self.value - other.value, self.currency)

    def __neg__(self):
        with localcontext(CONTEXT):
            return Money(-self.value, self.currency)

    def ratio(self, numerator: int, denominator: int):
        """Computed amount only; bounded integers preserve precision before rounding."""
        if type(numerator) is not int or type(denominator) is not int or not 0 < denominator <= 10**9 or abs(numerator) > 10**9:
            raise DomainError("RATIO_BOUNDS")
        with localcontext(CONTEXT):
            return Money((self.value * numerator / denominator).quantize(self.currency.quantum), self.currency)

    def text(self):
        return format(self.value, f".{self.currency.exponent}f")

    def wire(self):
        return {"amount": self.text(), "currency": self.currency.code}


def zero(currency):
    return Money(Decimal(0), currency)


def total(values, currency):
    result = zero(currency)
    for value in values:
        result = result + value
    return result
