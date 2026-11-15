"""Money is an integer count of minor units (øre, paise) plus a currency. Floats never touch an amount."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import total_ordering
from typing import Mapping

_DECIMALS = {"NOK": 2, "INR": 2}  # add a currency here to support it


def _decimals(currency: str) -> int:
    if currency not in _DECIMALS:
        raise ValueError(f"unsupported currency {currency!r}")
    return _DECIMALS[currency]


@total_ordering
@dataclass(frozen=True)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        _decimals(self.currency)
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise TypeError("Money.minor must be an int number of minor units")

    @classmethod
    def of(cls, amount: str | int | Decimal, currency: str) -> Money:
        """Build from major units, e.g. Money.of("1200.50", "NOK"). Floats are rejected on purpose."""
        if isinstance(amount, float):
            raise TypeError("pass amounts as str, int or Decimal, never float")
        scaled = Decimal(amount).scaleb(_decimals(currency))
        return cls(int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP)), currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(0, currency)

    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"cannot combine {self.currency} and {other.currency} without a rate")

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor - other.minor, self.currency)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._same_currency(other)
        return self.minor < other.minor

    def __str__(self) -> str:
        major = Decimal(self.minor).scaleb(-_decimals(self.currency))
        return f"{major:,.2f} {self.currency}"


@dataclass(frozen=True)
class FxRates:
    """Fixed rates for the reporting month, as Finance would supply them.

    `per_unit[c]` is how many units of the reporting currency one unit of `c` is worth.
    """

    reporting: str
    per_unit: Mapping[str, Decimal]

    def convert(self, amount: Money) -> Money:
        if amount.currency == self.reporting:
            rate = Decimal(1)
        elif amount.currency in self.per_unit:
            rate = self.per_unit[amount.currency]
        else:
            raise ValueError(f"no rate from {amount.currency} to {self.reporting}")
        major = Decimal(amount.minor).scaleb(-_decimals(amount.currency)) * rate
        minor = major.scaleb(_decimals(self.reporting)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return Money(int(minor), self.reporting)
