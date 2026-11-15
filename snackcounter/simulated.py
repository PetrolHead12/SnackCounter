"""Stand-ins for the systems we do not own: a controllable clock, a chat tool and the external HR system."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple

from .money import Money
from .ports import HRStatus, HRTimeout, HRUnavailable


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, moment: datetime) -> None:
        self._now = moment


class Message(NamedTuple):
    user_id: str
    text: str


class FakeChat:
    """Records every direct message so tests can assert who was told what.

    `delay` makes every send slow, which lets a concurrency test force other threads to run in the
    middle of an operation (a real chat API call is slow in exactly this way).
    """

    def __init__(self) -> None:
        self.sent: list[Message] = []
        self.delay = 0.0

    def send(self, user_id: str, text: str) -> None:
        if self.delay:
            time.sleep(self.delay)
        self.sent.append(Message(user_id, text))

    def to(self, user_id: str) -> list[str]:
        return [m.text for m in self.sent if m.user_id == user_id]


@dataclass
class HRRecord:
    reference: str
    employee_id: str
    amount: Money
    description: str
    created_at: datetime
    decide_at: datetime
    outcome: HRStatus


class FakeHR:
    """The external HR system as the brief describes it, plus the ways a real one misbehaves.

    - decides a claim 1-10 days after creation (default 3, scriptable per claim with `decide`)
    - `fail_next_creates`: the next N create calls fail and store nothing (outage)
    - `lose_reply_next_creates`: the next N create calls store the claim, then time out (lost reply)
    - repeated `reference` returns the existing claim (the idempotency we assume HR offers)
    """

    def __init__(self, clock: FakeClock, approval_days: int = 3) -> None:
        self._clock = clock
        self.approval_days = approval_days
        self.claims: dict[str, HRRecord] = {}
        self.create_calls = 0
        self.fail_next_creates = 0
        self.lose_reply_next_creates = 0
        self._by_reference: dict[str, str] = {}
        self._scripted: dict[str, tuple[int, HRStatus]] = {}

    def decide(self, reference: str, days: int, outcome: HRStatus) -> None:
        """Script what HR does with a claim: works before or after the claim exists."""
        self._scripted[reference] = (days, outcome)
        hr_id = self._by_reference.get(reference)
        if hr_id:
            record = self.claims[hr_id]
            record.decide_at = record.created_at + timedelta(days=days)
            record.outcome = outcome

    def create_claim(
        self, *, reference: str, employee_id: str, amount: Money, description: str, receipt: bytes
    ) -> str:
        self.create_calls += 1
        if self.fail_next_creates:
            self.fail_next_creates -= 1
            raise HRUnavailable("503 service unavailable")
        hr_id = self._by_reference.get(reference)
        if hr_id is None:
            hr_id = f"HR-{len(self.claims) + 1:05d}"
            days, outcome = self._scripted.get(reference, (self.approval_days, HRStatus.APPROVED))
            now = self._clock.now()
            self.claims[hr_id] = HRRecord(
                reference, employee_id, amount, description, now, now + timedelta(days=days), outcome
            )
            self._by_reference[reference] = hr_id
        if self.lose_reply_next_creates:
            self.lose_reply_next_creates -= 1
            raise HRTimeout("no reply from HR")
        return hr_id

    def claim_status(self, hr_claim_id: str) -> HRStatus:
        record = self.claims[hr_claim_id]
        return record.outcome if self._clock.now() >= record.decide_at else HRStatus.PENDING
