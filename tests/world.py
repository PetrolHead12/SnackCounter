"""A ready-made world for tests and the demo: Oslo (NOK) and Bengaluru (INR), volunteers, fake chat and HR.

Fixed UTC offsets keep tests independent of the tz database. Production would use ZoneInfo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from snackcounter import (
    FxRates, Money, Office, Policy, RefillService, SnackLine, Volunteer, WorkCalendar,
)
from snackcounter.simulated import FakeChat, FakeClock, FakeHR

OSLO_TZ = timezone(timedelta(hours=1))
BLR_TZ = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)  # a Monday: 09:00 in Oslo, 13:30 in Bengaluru
NOVEMBER = (2026, 11)
FX = FxRates("NOK", {"INR": Decimal("0.125")})  # illustrative rate; Finance supplies the real one


def NOK(amount) -> Money:
    return Money.of(amount, "NOK")


def INR(amount) -> Money:
    return Money.of(amount, "INR")


class World:
    def __init__(self, n_oslo=5, n_blr=5, oslo_budget="6000", blr_budget="15000", policy=Policy(), start=START):
        self.clock = FakeClock(start)
        self.chat = FakeChat()
        self.hr = FakeHR(self.clock)
        self.oslo = Office("OSL", "Oslo", "NOK", NOK(oslo_budget), NOK(300), NOK(1200),
                           WorkCalendar(OSLO_TZ), "admin-osl")
        self.blr = Office("BLR", "Bengaluru", "INR", INR(blr_budget), INR(500), INR(3000),
                          WorkCalendar(BLR_TZ), "admin-blr")
        volunteers = [Volunteer(f"o{i}", f"Olav{i}", "OSL") for i in range(1, n_oslo + 1)]
        volunteers += [Volunteer(f"b{i}", f"Bhanu{i}", "BLR") for i in range(1, n_blr + 1)]
        self.svc = RefillService([self.oslo, self.blr], volunteers, self.chat, self.hr, self.clock, policy)

    def restock(self, office_id, amount, *, items=(("chips", 5),), receipt=None):
        """One refill start to finish by whoever is offered it. Returns (refill, claim)."""
        refill = self.svc.report_empty(office_id, "emp-1")
        buyer = refill.assignee_id
        self.svc.accept(refill.id, buyer)
        claim = self.svc.complete(
            refill.id, buyer, amount, receipt or f"receipt-{refill.id}".encode(),
            [SnackLine(name, qty) for name, qty in items],
        )
        return refill, claim

    def run_out_the_clock(self, refill):
        """Jump to the exact moment the current offer or commitment lapses, and let the system react."""
        self.clock.set(max(self.clock.now(), refill.deadline))
        self.svc.tick()
