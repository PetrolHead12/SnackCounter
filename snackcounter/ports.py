"""The three things we do not own. The service talks to these interfaces only; tests plug in fakes."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from .money import Money


class Chat(Protocol):
    def send(self, user_id: str, text: str) -> None:
        """Direct message to one employee in the company chat tool."""


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware datetime."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class HRStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class HRError(Exception):
    """The HR system could not do what we asked. Safe to retry: see HRSystem.create_claim."""


class HRUnavailable(HRError):
    """HR said no (outage, 5xx). Nothing was stored."""


class HRTimeout(HRError):
    """No answer. The claim may or may not have been stored: the dangerous case for duplicates."""


class HRSystem(Protocol):
    def create_claim(
        self, *, reference: str, employee_id: str, amount: Money, description: str, receipt: bytes
    ) -> str:
        """Create a reimbursement claim and return HR's claim id.

        `reference` is our own unique id for the claim. ASSUMPTION: HR treats a repeated reference as
        the same claim (idempotency). If HR has no such field, the adapter embeds the reference in the
        description and looks it up before retrying.
        """

    def claim_status(self, hr_claim_id: str) -> HRStatus:
        """ASSUMPTION: we can poll HR for the outcome (approval takes 1-10 days)."""
