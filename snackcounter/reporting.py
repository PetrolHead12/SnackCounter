"""The one monthly report management asked for.

Total spending is every purchase actually made in the month, whether or not an admin has cleared it yet.
Purchases still waiting for admin approval stay visible on their own ("held") but are never left out of
the total. Each office is summed in its own currency first (exact, integer arithmetic) and converted
once, as a subtotal, at the fixed rate Finance supplies for the month. Converting purchase by purchase
would add rounding noise; converting subtotals keeps the error to at most one minor unit per office.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

from .models import ClaimState, Office
from .money import FxRates, Money


@dataclass(frozen=True)
class OfficeMonth:
    """Spent = held + hr_approved + awaiting_hr + rejected. Every purchase falls in exactly one of the four."""

    office: Office
    budget: Money
    spent: Money         # every purchase made this month, including the held ones
    held: Money          # of `spent`: waiting for admin approval, not sent to HR yet
    refills: int         # purchases made this month, including held ones
    hr_approved: Money   # of `spent`: HR approved the claim (the payroll payout is outside our view)
    awaiting_hr: Money   # of `spent`: sent or about to be sent, no HR decision yet
    rejected: Money      # of `spent`: HR said no, needs a human
    snack_units: Mapping[str, int]  # units restocked per snack (a proxy for what gets eaten)
    stockouts: Mapping[str, int]    # times each snack was reported as the thing that ran out

    @property
    def used_pct(self) -> Decimal:
        return (Decimal(self.spent.minor) * 100 / Decimal(self.budget.minor)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )


@dataclass(frozen=True)
class MonthlyReport:
    year: int
    month: int
    offices: tuple[OfficeMonth, ...]
    fx: FxRates
    total: Money  # all purchases, all offices, in fx.reporting; each office converted once, as a subtotal
    held: Money   # of `total`: purchases waiting for admin approval (each office's held amount converted)


def build_monthly_report(svc, year: int, month: int, fx: FxRates) -> MonthlyReport:
    key = (year, month)
    rows = []
    total = held_total = Money.zero(fx.reporting)
    for office in svc.offices.values():
        hr_approved = awaiting = rejected = Money.zero(office.currency)
        units: Counter[str] = Counter()
        purchases = svc.ledger.entries(office, key)  # every purchase, held or not
        for entry in purchases:
            refill = svc.refills[entry.refill_id]
            for line in refill.purchase.items:
                units[line.name] += line.qty
            if not entry.accepted:
                continue  # held: counted in `held` below, not yet in front of HR
            state = svc.claims[refill.claim_id].state
            if state is ClaimState.APPROVED:
                hr_approved += entry.amount
            elif state is ClaimState.REJECTED:
                rejected += entry.amount
            else:
                awaiting += entry.amount
        stockouts: Counter[str] = Counter()
        for refill in svc.refills.values():
            if refill.office_id == office.id and svc.ledger.month_of(office, refill.reported_at) == key:
                stockouts.update(refill.empty_items)
        spent = svc.ledger.spent(office, key)
        held = svc.ledger.held(office, key)
        total += fx.convert(spent)
        held_total += fx.convert(held)
        rows.append(
            OfficeMonth(
                office, office.monthly_budget, spent, held, len(purchases),
                hr_approved, awaiting, rejected, dict(units), dict(stockouts),
            )
        )
    return MonthlyReport(year, month, tuple(rows), fx, total, held_total)


def render_text(report: MonthlyReport) -> str:
    fx = report.fx
    rates = ", ".join(f"1 {c} = {r} {fx.reporting}" for c, r in fx.per_unit.items() if c != fx.reporting)
    lines = [
        f"Snack spending {report.year}-{report.month:02d}   (total in {fx.reporting}; fixed rates: {rates})",
        "",
        f"{'Office':<11}{'Spent':>16}{'Budget':>16}{'Used':>8}{'Refills':>9}"
        f"{'HR approved':>17}{'Awaiting HR':>17}{'Rejected':>15}{'Held':>15}",
    ]
    for r in report.offices:
        lines.append(
            f"{r.office.name:<11}{str(r.spent):>16}{str(r.budget):>16}{str(r.used_pct) + '%':>8}{r.refills:>9}"
            f"{str(r.hr_approved):>17}{str(r.awaiting_hr):>17}{str(r.rejected):>15}{str(r.held):>15}"
        )
    lines += [
        "",
        "Spent counts every purchase made, including those Held for admin approval:",
        "Spent = HR approved + Awaiting HR + Rejected + Held.  'HR approved' is HR's decision on the claim;",
        "whether payroll has actually paid out is not visible here.",
        "",
        f"TOTAL spending, all offices: {report.total}   (of which held for admin approval: {report.held})",
        "",
    ]
    for r in report.offices:
        if r.snack_units:
            top = ", ".join(f"{n} x{q}" for n, q in sorted(r.snack_units.items(), key=lambda kv: -kv[1]))
            lines.append(f"{r.office.name} restocked: {top}")
        if r.stockouts:
            out = ", ".join(f"{n} x{q}" for n, q in sorted(r.stockouts.items(), key=lambda kv: -kv[1]))
            lines.append(f"{r.office.name} ran out of: {out}")
    return "\n".join(lines)
