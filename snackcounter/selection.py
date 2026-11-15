"""Choosing who buys. One pure function, so the rule is easy to read, test and explain.

ELIGIBLE  not already asked about this refill, in this office today (home, or a registered visit),
          and not on self-declared leave.

ORDER     1. nobody whose claim is still waiting for HR approval comes first. Soft rule: they go last,
             never excluded, so a refill can always find someone.
          2. fewest refills completed in the fairness window. This is what stops a few people doing most
             of the work.
          3. asked longest ago. Round-robin among equals, and a person who ignored their last offer
             moves back behind the others.
          4. id. Deterministic, so tests and audits are repeatable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .models import Office, Volunteer

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Load:
    completed: int = 0    # refills completed within the fairness window
    # Claims HR has not approved yet (held, queued or submitted): money this person has fronted. Once HR
    # approves we treat them as settled. When payroll actually pays out is not visible to us.
    open_claims: int = 0


def unavailable_reason(volunteer: Volunteer, office: Office, now: datetime) -> str | None:
    """Why this person cannot take a refill at this office today, or None if they can.

    Shared by the ranking and by admin assignment, so the two can never disagree about availability.
    """
    today = now.astimezone(office.calendar.tz).date()
    where = volunteer.office_on(today)
    if where != office.id:
        return f"{volunteer.name} is in {where} on {today:%d %b}, not in {office.name}"
    if volunteer.on_leave(today):
        return f"{volunteer.name} is on leave on {today:%d %b}"
    return None


def eligible(volunteer: Volunteer, office: Office, now: datetime, excluded: set[str]) -> bool:
    return volunteer.id not in excluded and unavailable_reason(volunteer, office, now) is None


def rank_candidates(
    volunteers: Iterable[Volunteer],
    office: Office,
    now: datetime,
    excluded: set[str],
    loads: Mapping[str, Load],
) -> list[Volunteer]:
    """Eligible volunteers, best candidate first."""

    def order(v: Volunteer) -> tuple:
        load = loads.get(v.id, Load())
        return (load.open_claims > 0, load.completed, v.last_offered_at or _EPOCH, v.id)

    return sorted((v for v in volunteers if eligible(v, office, now, excluded)), key=order)
