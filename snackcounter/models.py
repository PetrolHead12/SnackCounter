"""Plain data. Almost no behaviour, so every record maps 1:1 to a database row."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from .money import Money
from .workcal import WorkCalendar


class RefillState(str, Enum):
    REQUESTED = "requested"  # reported empty, nobody asked yet (transient)
    OFFERED = "offered"      # exclusively offered to one volunteer, waiting for yes/no
    COMMITTED = "committed"  # that volunteer said yes; the delivery clock is running
    ESCALATED = "escalated"  # the office admin must decide (nobody left to ask, or budget gone)
    STOCKED = "stocked"      # receipt received, counter is full. The money side continues in Claim
    CANCELLED = "cancelled"  # closed by the admin (e.g. false alarm)


# A refill is "open" until the receipt is in. After that it is only a financial record, so a new
# "empty" report can start a new refill even while HR is still deciding on the old claim.
OPEN_STATES = frozenset(
    {RefillState.REQUESTED, RefillState.OFFERED, RefillState.COMMITTED, RefillState.ESCALATED}
)


class Escalation(str, Enum):
    NO_VOLUNTEER = "no volunteer available"
    BUDGET = "monthly budget used up"


class ClaimState(str, Enum):
    HELD = "held"            # receipt needs admin approval before HR sees it
    QUEUED = "queued"        # approved on our side, not (successfully) sent to HR yet
    SUBMITTED = "submitted"  # HR has it and is deciding (takes 1-10 days)
    APPROVED = "approved"    # HR approved the claim. The payroll payout happens inside HR; we never see it
    REJECTED = "rejected"    # HR said no


@dataclass(frozen=True)
class SnackLine:
    name: str
    qty: int


@dataclass(frozen=True)
class Purchase:
    amount: Money
    purchased_at: datetime
    items: tuple[SnackLine, ...]
    receipt_sha256: str  # the image itself lives in HR; we keep a fingerprint to catch double submission


@dataclass(frozen=True)
class Office:
    id: str
    name: str
    currency: str
    monthly_budget: Money
    typical_min: Money       # below this much budget left, nobody is asked: the admin decides
    typical_max: Money       # per-purchase cap; receipts above it need the admin's approval
    calendar: WorkCalendar   # timezone, working hours and holidays
    admin_id: str            # final escalation point


@dataclass
class Volunteer:
    id: str                  # the company login / HR employee id
    name: str
    home_office: str
    leave: list[tuple[date, date]] = field(default_factory=list)              # self-declared, inclusive
    visits: list[tuple[str, date, date]] = field(default_factory=list)        # (office, from, to), inclusive
    last_offered_at: datetime | None = None

    def office_on(self, day: date) -> str:
        """Where this person is on a given day: a registered visit overrides the home office."""
        for office_id, start, end in self.visits:
            if start <= day <= end:
                return office_id
        return self.home_office

    def on_leave(self, day: date) -> bool:
        return any(start <= day <= end for start, end in self.leave)


@dataclass
class Refill:
    id: str
    office_id: str
    reported_at: datetime
    reporters: list[str]
    empty_items: set[str] = field(default_factory=set)
    state: RefillState = RefillState.REQUESTED
    assignee_id: str | None = None
    deadline: datetime | None = None                    # when the current offer / commitment lapses
    attempts: int = 0                                   # offers made so far
    excluded: set[str] = field(default_factory=set)     # asked and did not deliver: never asked again for this refill
    ceiling: Money | None = None                        # most the buyer may spend without needing approval
    over_budget_ok: bool = False                        # admin has pre-approved going past the monthly budget
    escalation: Escalation | None = None
    buyer_id: str | None = None
    stocked_at: datetime | None = None
    purchase: Purchase | None = None
    claim_id: str | None = None
    log: list[tuple[datetime, str]] = field(default_factory=list)   # audit trail


@dataclass
class Claim:
    id: str                          # also the reference we give HR, so a retry can never create a second claim
    refill_id: str
    employee_id: str
    office_id: str
    amount: Money                    # in the receipt's currency; HR converts to the payroll currency
    description: str
    created_at: datetime
    state: ClaimState
    receipt: bytes | None = None     # kept only until HR has accepted it, then dropped
    hr_claim_id: str | None = None
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    attempts: int = 0
    last_error: str | None = None
    overdue_notified: bool = False
