"""The budget check. It runs at two moments because the real amount is only known after the purchase.

  before: ceiling() tells the volunteer how much they may spend: min(per-purchase cap, what is left
          this month). If what is left is below the smallest typical purchase, nobody is asked at all;
          the office admin decides (see RefillService).
  after : check() looks at the receipt. Above the cap, or beyond the month's budget, it is held for the
          admin instead of being blocked (the money is already spent) or silently paid (nobody decided).

Every purchase counts against the budget from the moment it is made. A purchase held for admin approval
is real money already spent, so it is counted (and reported separately as "held") instead of ignored.
Admin approval decides whether the claim goes to HR, not whether the purchase happened.

Budgets are per office, in the office's own currency, per calendar month in the office's own timezone,
with no rollover. Nothing here converts currency: conversion exists only in the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Office
from .money import Money

Month = tuple[int, int]  # (year, month) as the office's own calendar sees it

ABOVE_CAP = "above the per-purchase cap"
OVER_BUDGET = "over the monthly budget"


@dataclass
class Entry:
    refill_id: str
    office_id: str
    month: Month
    amount: Money
    accepted: bool = True  # False = held for admin approval. Held entries still count as spent


class BudgetLedger:
    """One entry per purchase. The single source for both budget checks and the monthly report."""

    def __init__(self) -> None:
        self._entries: list[Entry] = []

    @staticmethod
    def month_of(office: Office, moment: datetime) -> Month:
        local = moment.astimezone(office.calendar.tz)
        return (local.year, local.month)

    def record(self, refill_id: str, office: Office, month: Month, amount: Money, accepted: bool) -> None:
        self._entries.append(Entry(refill_id, office.id, month, amount, accepted))

    def accept(self, refill_id: str) -> None:
        """The admin approved a held purchase: from now on it counts against the budget."""
        for entry in self._entries:
            if entry.refill_id == refill_id:
                entry.accepted = True

    def entries(self, office: Office, month: Month, *, accepted: bool | None = None) -> list[Entry]:
        return [
            e
            for e in self._entries
            if e.office_id == office.id and e.month == month and (accepted is None or e.accepted == accepted)
        ]

    def _total(self, office: Office, month: Month, accepted: bool | None = None) -> Money:
        total = Money.zero(office.currency)
        for entry in self.entries(office, month, accepted=accepted):
            total += entry.amount
        return total

    def spent(self, office: Office, month: Month) -> Money:
        """Every purchase booked to the month, held or not: the real spend."""
        return self._total(office, month)

    def held(self, office: Office, month: Month) -> Money:
        """The part of `spent` that is still waiting for admin approval."""
        return self._total(office, month, accepted=False)

    def remaining(self, office: Office, month: Month) -> Money:
        return office.monthly_budget - self.spent(office, month)

    def ceiling(self, office: Office, month: Month) -> Money | None:
        """What a volunteer may spend on the next refill, or None if the month cannot fund one."""
        left = self.remaining(office, month)
        if left < office.typical_min:
            return None
        return min(office.typical_max, left)

    def check(self, office: Office, month: Month, amount: Money) -> list[str]:
        """Reasons the receipt needs admin approval. Empty list = fine to send to HR."""
        reasons = []
        if amount > office.typical_max:
            reasons.append(ABOVE_CAP)
        if amount > self.remaining(office, month):
            reasons.append(OVER_BUDGET)
        return reasons
