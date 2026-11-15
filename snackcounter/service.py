"""The refill lifecycle.

  empty -> OFFERED --accept--> COMMITTED --receipt--> STOCKED --> claim: SUBMITTED -> APPROVED | REJECTED
              |  declined / no answer    |  released / overdue            \\-> HELD (admin) -> QUEUED
              +-------- next volunteer <-+
  after `max_attempts` offers, nobody left to ask, or no budget left -> ESCALATED -> office admin

Two rules keep this simple and safe:
  * Every state change happens inside one lock (in production: one DB transaction with a conditional
    UPDATE ... WHERE state = 'offered' AND assignee = :me). Two people can never hold the same refill.
  * Hand-overs and HR retries/polling are driven by tick(). Both deadlines are also enforced at the moment
    they matter, even if tick() has not run yet: accept() refuses a late yes and passes the refill on,
    complete() refuses a late receipt. All three use the same `_expired` test and the same `_expire`
    hand-over, so they can never disagree about when time is up or what happens next.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from .budget import OVER_BUDGET, BudgetLedger
from .models import (
    OPEN_STATES,
    Claim,
    ClaimState,
    Escalation,
    Office,
    Purchase,
    Refill,
    RefillState,
    SnackLine,
    Volunteer,
)
from .money import FxRates, Money
from .ports import Chat, Clock, HRError, HRStatus, HRSystem
from .reporting import MonthlyReport, build_monthly_report
from .selection import Load, rank_candidates, unavailable_reason


class RefillError(Exception):
    """The caller asked for something the rules do not allow. Nothing was changed."""


class NotYourTurn(RefillError):
    """This refill is not offered or committed to you (any more)."""


class OfferExpired(NotYourTurn):
    """The offer to you ran out before you answered, and the refill has been passed on."""


class CommitmentExpired(NotYourTurn):
    """You committed to this refill, but your deadline has passed. Nothing was recorded."""


class NotEligible(RefillError):
    """The person cannot take this refill right now: on leave, or in the other office."""


class InvalidReceipt(RefillError):
    pass


class InvalidState(RefillError):
    pass


class NotAdmin(RefillError):
    pass


@dataclass(frozen=True)
class Policy:
    accept_hours: float = 4.0       # working hours an offered volunteer has to say yes or no
    complete_hours: float = 8.0     # working hours from yes to receipt: about one working day
    max_attempts: int = 3           # offers per refill before the office admin is pulled in
    fairness_window_days: int = 90  # how far back "refills completed" looks
    claim_sla_days: int = 10        # HR's stated worst case; after this we chase them


def _locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _when(office: Office, moment: datetime) -> str:
    return moment.astimezone(office.calendar.tz).strftime("%a %d %b %H:%M")


def _expired(refill: Refill, now: datetime) -> bool:
    """The single definition of 'time is up' for an offer or commitment. Used by tick() and complete()."""
    return refill.deadline is not None and refill.deadline <= now


class RefillService:
    def __init__(
        self,
        offices: Iterable[Office],
        volunteers: Iterable[Volunteer],
        chat: Chat,
        hr: HRSystem,
        clock: Clock,
        policy: Policy = Policy(),
    ) -> None:
        self.offices = {o.id: o for o in offices}
        self.volunteers = {v.id: v for v in volunteers}
        self.chat, self.hr, self.clock, self.policy = chat, hr, clock, policy
        self.refills: dict[str, Refill] = {}
        self.claims: dict[str, Claim] = {}
        self.ledger = BudgetLedger()
        self._receipts: set[str] = set()
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- lookups

    def _office(self, office_id: str) -> Office:
        if office_id not in self.offices:
            raise RefillError(f"unknown office {office_id!r}")
        return self.offices[office_id]

    def _refill(self, refill_id: str) -> Refill:
        if refill_id not in self.refills:
            raise RefillError(f"unknown refill {refill_id!r}")
        return self.refills[refill_id]

    def _volunteer(self, volunteer_id: str) -> Volunteer:
        if volunteer_id not in self.volunteers:
            raise RefillError(f"unknown volunteer {volunteer_id!r}")
        return self.volunteers[volunteer_id]

    def _require_admin(self, office: Office, user_id: str) -> None:
        if user_id != office.admin_id:
            raise NotAdmin(f"only {office.admin_id} can do this for {office.name}")

    @_locked
    def open_refill(self, office_id: str) -> Refill | None:
        return next(
            (r for r in self.refills.values() if r.office_id == office_id and r.state in OPEN_STATES), None
        )

    def _say(self, user_id: str, text: str) -> None:
        self.chat.send(user_id, text)

    def _log(self, refill: Refill, text: str) -> None:
        refill.log.append((self.clock.now(), text))

    # ------------------------------------------------- volunteer self-service

    @_locked
    def set_leave(self, volunteer_id: str, start: date, end: date) -> None:
        """Self-declared unavailability (holiday, sick leave). Dates are inclusive."""
        self._volunteer(volunteer_id).leave.append((start, end))

    @_locked
    def register_visit(self, volunteer_id: str, office_id: str, start: date, end: date) -> None:
        """For the dates given the person counts as being in that office, not at home."""
        self._office(office_id)
        self._volunteer(volunteer_id).visits.append((office_id, start, end))

    # ------------------------------------------------------ the refill lifecycle

    @_locked
    def report_empty(self, office_id: str, reporter_id: str, empty_items: Iterable[str] = ()) -> Refill:
        """Anyone can report an empty counter. A second report joins the open refill instead of starting one."""
        office = self._office(office_id)
        items = {i.strip().lower() for i in empty_items if i.strip()}
        refill = self.open_refill(office_id)
        if refill:
            if reporter_id not in refill.reporters:
                refill.reporters.append(reporter_id)
            refill.empty_items |= items
            self._log(refill, f"another report from {reporter_id}")
            return refill

        now = self.clock.now()
        refill = Refill(f"R{next(self._ids):04d}", office_id, now, [reporter_id], items)
        self.refills[refill.id] = refill
        self._log(refill, f"reported empty by {reporter_id}")
        refill.ceiling = self.ledger.ceiling(office, self.ledger.month_of(office, now))
        if refill.ceiling is None:
            self._escalate(refill, Escalation.BUDGET)
        else:
            self._offer_next(refill)
        return refill

    @_locked
    def accept(self, refill_id: str, volunteer_id: str) -> Refill:
        """Say yes to an offer. A yes that arrives after the offer's deadline is refused and the refill is
        passed on at once, exactly as tick() would have done, even if tick() has not run yet."""
        refill = self._refill(refill_id)
        if refill.state is RefillState.COMMITTED and refill.assignee_id == volunteer_id:
            return refill  # double click: harmless
        if refill.state is not RefillState.OFFERED or refill.assignee_id != volunteer_id:
            raise NotYourTurn(f"{refill_id} is not on offer to {volunteer_id}")
        if _expired(refill, self.clock.now()):
            ran_out = _when(self.offices[refill.office_id], refill.deadline)
            self._expire(refill)  # the hand-over is complete before the error is raised
            raise OfferExpired(f"{refill_id}: your time to answer ran out at {ran_out}, so it has been "
                               f"passed on to someone else.")
        self._commit(refill, volunteer_id)
        return refill

    @_locked
    def decline(self, refill_id: str, volunteer_id: str) -> None:
        refill = self._refill(refill_id)
        if refill.state is not RefillState.OFFERED or refill.assignee_id != volunteer_id:
            raise NotYourTurn(f"{refill_id} is not on offer to {volunteer_id}")
        self._pass_on(refill, "declined")

    @_locked
    def release(self, refill_id: str, volunteer_id: str) -> None:
        """A committed volunteer hands the refill back, so it moves on now instead of at the deadline."""
        refill = self._refill(refill_id)
        if refill.state is not RefillState.COMMITTED or refill.assignee_id != volunteer_id:
            raise NotYourTurn(f"{refill_id} is not committed to {volunteer_id}")
        self._pass_on(refill, "released it")

    @_locked
    def complete(
        self,
        refill_id: str,
        volunteer_id: str,
        amount: Money,
        receipt: bytes,
        items: Iterable[SnackLine] = (),
        purchased_at: datetime | None = None,
    ) -> Claim:
        """The committed volunteer reports the purchase. Stocks the refill and files the HR claim."""
        refill = self._refill(refill_id)
        office = self.offices[refill.office_id]
        now = self.clock.now()
        if refill.state is RefillState.STOCKED and refill.buyer_id == volunteer_id:
            return self.claims[refill.claim_id]  # retried request: no second claim
        if refill.state is not RefillState.COMMITTED or refill.assignee_id != volunteer_id:
            raise NotYourTurn(f"{refill_id} is not committed to {volunteer_id}")
        if _expired(refill, now):  # do not wait for tick(): a late receipt is refused the moment it arrives
            raise CommitmentExpired(
                f"{refill_id}: your time ran out at {_when(office, refill.deadline)}, so it is being passed "
                f"on. If you already bought snacks, please tell {office.admin_id}."
            )

        if amount.currency != office.currency:
            raise InvalidReceipt(f"{office.name} receipts must be in {office.currency}, not {amount.currency}")
        if amount.minor <= 0:
            raise InvalidReceipt("the amount must be positive")
        if not receipt:
            raise InvalidReceipt("a receipt image is required")
        lines = tuple(SnackLine(i.name.strip().lower(), i.qty) for i in items)
        if any(not line.name or line.qty <= 0 for line in lines):
            raise InvalidReceipt("every snack needs a name and a positive quantity")
        digest = hashlib.sha256(receipt).hexdigest()
        if digest in self._receipts:
            raise InvalidReceipt("this receipt has already been submitted")

        purchased_at = purchased_at or now
        month = self.ledger.month_of(office, purchased_at)
        reasons = self.ledger.check(office, month, amount)
        if refill.over_budget_ok:  # the admin already agreed to go past the monthly budget
            reasons = [r for r in reasons if r != OVER_BUDGET]

        self._receipts.add(digest)
        refill.state, refill.assignee_id, refill.deadline = RefillState.STOCKED, None, None
        refill.buyer_id, refill.stocked_at = volunteer_id, now
        refill.purchase = Purchase(amount, purchased_at, lines, digest)
        claim = Claim(
            id=f"C-{refill.id}",
            refill_id=refill.id,
            employee_id=volunteer_id,
            office_id=office.id,
            amount=amount,
            description=f"Office snacks, {office.name}, {purchased_at:%Y-%m-%d} (ref C-{refill.id})",
            created_at=now,
            state=ClaimState.HELD if reasons else ClaimState.QUEUED,
            receipt=receipt,
        )
        refill.claim_id = claim.id
        self.claims[claim.id] = claim
        self.ledger.record(refill.id, office, month, amount, accepted=not reasons)
        self._log(refill, f"stocked by {volunteer_id}: {amount}")

        buyer = self.volunteers[volunteer_id].name
        for reporter in refill.reporters:
            self._say(reporter, f"The {office.name} snack counter has been restocked. Thanks, {buyer}!")
        if reasons:
            why = " and ".join(reasons)
            self._say(volunteer_id, f"Thanks! Your receipt of {amount} is {why}, so {office.admin_id} must "
                                    f"approve it before it goes to HR.")
            self._say(office.admin_id, f"Please approve {amount} from {buyer} for refill {refill.id}: {why}.")
        else:
            self._submit(claim)
        return claim

    # ----------------------------------------------------------- admin actions

    @_locked
    def admin_assign(self, refill_id: str, admin_id: str, volunteer_id: str) -> Refill:
        """The admin picks the buyer for an escalated refill. Where the budget was the reason, this
        decision is also the approval to go past it.

        The admin may overrule the fairness order, and the history of this refill (who was already
        asked and did not deliver), but not availability: someone on leave, or in the other office,
        cannot restock this counter. Checked before anything is changed.
        """
        refill = self._refill(refill_id)
        office = self.offices[refill.office_id]
        self._require_admin(office, admin_id)
        if refill.state is not RefillState.ESCALATED:
            raise InvalidState(f"{refill_id} is {refill.state.value}; only escalated refills can be assigned")
        reason = unavailable_reason(self._volunteer(volunteer_id), office, self.clock.now())
        if reason:
            raise NotEligible(reason)
        if refill.escalation is Escalation.BUDGET:
            refill.over_budget_ok = True
            refill.ceiling = office.typical_max
        self._commit(refill, volunteer_id)
        return refill

    @_locked
    def approve_held(self, refill_id: str, admin_id: str) -> Claim:
        """The admin accepts an over-cap or over-budget receipt: it now counts and goes to HR."""
        refill = self._refill(refill_id)
        office = self.offices[refill.office_id]
        self._require_admin(office, admin_id)
        claim = self.claims.get(refill.claim_id) if refill.claim_id else None
        if claim is None or claim.state is not ClaimState.HELD:
            raise InvalidState(f"{refill_id} has no held receipt")
        self.ledger.accept(refill.id)
        claim.state = ClaimState.QUEUED
        self._log(refill, f"receipt approved by {admin_id}")
        self._submit(claim)
        return claim

    @_locked
    def admin_cancel(self, refill_id: str, admin_id: str, reason: str) -> None:
        refill = self._refill(refill_id)
        office = self.offices[refill.office_id]
        self._require_admin(office, admin_id)
        if refill.state not in OPEN_STATES:
            raise InvalidState(f"{refill_id} is already {refill.state.value}")
        if refill.assignee_id:
            self._say(refill.assignee_id, f"Refill {refill.id} was cancelled by {admin_id}: {reason}")
        refill.state, refill.assignee_id, refill.deadline = RefillState.CANCELLED, None, None
        self._log(refill, f"cancelled by {admin_id}: {reason}")

    # --------------------------------------------------------------- time

    @_locked
    def tick(self) -> None:
        """Run every few minutes: hands over expired offers and commitments, retries and polls HR."""
        now = self.clock.now()
        for refill in list(self.refills.values()):
            if refill.state in (RefillState.OFFERED, RefillState.COMMITTED) and _expired(refill, now):
                self._expire(refill)
        for claim in list(self.claims.values()):
            if claim.state is ClaimState.QUEUED:
                self._submit(claim)  # earlier attempt failed: retry with the same reference
        for claim in list(self.claims.values()):
            if claim.state is ClaimState.SUBMITTED:
                self._poll_hr(claim, now)

    # ------------------------------------------------------------ reporting

    @_locked
    def monthly_report(self, year: int, month: int, fx: FxRates) -> MonthlyReport:
        return build_monthly_report(self, year, month, fx)

    # ------------------------------------------------------------ internals

    def _loads(self, now: datetime) -> dict[str, Load]:
        since = now - timedelta(days=self.policy.fairness_window_days)
        done = Counter(
            r.buyer_id for r in self.refills.values() if r.state is RefillState.STOCKED and r.stocked_at >= since
        )
        open_claims = Counter(  # claims HR has not approved yet: the person is still out of pocket
            c.employee_id
            for c in self.claims.values()
            if c.state in (ClaimState.HELD, ClaimState.QUEUED, ClaimState.SUBMITTED)
        )
        return {vid: Load(done[vid], open_claims[vid]) for vid in self.volunteers}

    def _offer_next(self, refill: Refill) -> None:
        office = self.offices[refill.office_id]
        now = self.clock.now()
        if refill.attempts >= self.policy.max_attempts:
            return self._escalate(refill, Escalation.NO_VOLUNTEER)
        ranked = rank_candidates(self.volunteers.values(), office, now, refill.excluded, self._loads(now))
        if not ranked:
            return self._escalate(refill, Escalation.NO_VOLUNTEER)
        chosen = ranked[0]
        chosen.last_offered_at = now
        refill.state, refill.assignee_id = RefillState.OFFERED, chosen.id
        refill.attempts += 1
        refill.deadline = office.calendar.add_working_hours(now, self.policy.accept_hours)
        self._log(refill, f"offered to {chosen.id} (attempt {refill.attempts}), "
                         f"answer by {refill.deadline:%Y-%m-%d %H:%M}Z")
        self._say(
            chosen.id,
            f"The {office.name} snack counter is empty. Can you restock it? Please answer by "
            f"{_when(office, refill.deadline)}. You may spend up to {refill.ceiling}.",
        )

    def _commit(self, refill: Refill, volunteer_id: str) -> None:
        office = self.offices[refill.office_id]
        refill.state, refill.assignee_id, refill.escalation = RefillState.COMMITTED, volunteer_id, None
        refill.deadline = office.calendar.add_working_hours(self.clock.now(), self.policy.complete_hours)
        self._log(refill, f"{volunteer_id} committed, due {refill.deadline:%Y-%m-%d %H:%M}Z")
        due = _when(office, refill.deadline)
        self._say(volunteer_id, f"Thanks! Please restock the {office.name} counter and send the receipt "
                                f"by {due}. You may spend up to {refill.ceiling}.")
        name = self.volunteers[volunteer_id].name
        for reporter in refill.reporters:
            self._say(reporter, f"{name} is on it: the {office.name} counter will be restocked by {due}.")

    def _expire(self, refill: Refill) -> None:
        """An offer or commitment ran out: tell the person and pass the refill on. Shared by tick() and accept()."""
        why = "did not answer in time" if refill.state is RefillState.OFFERED else "did not finish in time"
        self._say(refill.assignee_id, f"Refill {refill.id} has been passed on to someone else ({why}). If you "
                                      f"already bought snacks, please tell {self.offices[refill.office_id].admin_id}.")
        self._pass_on(refill, why)

    def _pass_on(self, refill: Refill, why: str) -> None:
        refill.excluded.add(refill.assignee_id)
        self._log(refill, f"{refill.assignee_id} {why}")
        refill.assignee_id, refill.deadline = None, None
        self._offer_next(refill)

    def _escalate(self, refill: Refill, reason: Escalation) -> None:
        office = self.offices[refill.office_id]
        refill.state, refill.assignee_id, refill.deadline = RefillState.ESCALATED, None, None
        refill.escalation = reason
        self._log(refill, f"escalated to {office.admin_id}: {reason.value}")
        self._say(office.admin_id, f"Refill {refill.id} at {office.name} needs you: {reason.value}. "
                                   f"Please assign someone.")

    def _submit(self, claim: Claim) -> None:
        """Send a claim to HR. Safe to call again after any failure: the reference makes it idempotent."""
        claim.attempts += 1
        try:
            claim.hr_claim_id = self.hr.create_claim(
                reference=claim.id,
                employee_id=claim.employee_id,
                amount=claim.amount,
                description=claim.description,
                receipt=claim.receipt,
            )
        except HRError as error:
            claim.last_error = f"{type(error).__name__}: {error}"
            return  # stays QUEUED; the next tick retries
        claim.state, claim.submitted_at = ClaimState.SUBMITTED, self.clock.now()
        claim.receipt, claim.last_error = None, None
        self._say(claim.employee_id, f"Your claim of {claim.amount} has been sent to HR (ref {claim.id}). "
                                     f"HR usually decides within {self.policy.claim_sla_days} days.")

    def _poll_hr(self, claim: Claim, now: datetime) -> None:
        try:
            status = self.hr.claim_status(claim.hr_claim_id)
        except HRError:
            return
        admin = self.offices[claim.office_id].admin_id
        if status is HRStatus.APPROVED:
            claim.state, claim.decided_at = ClaimState.APPROVED, now
            self._say(claim.employee_id, f"HR approved claim {claim.id} ({claim.amount}). HR and payroll "
                                         f"handle the payout from here.")
        elif status is HRStatus.REJECTED:
            claim.state, claim.decided_at = ClaimState.REJECTED, now
            self._say(claim.employee_id, f"HR rejected claim {claim.id} ({claim.amount}). {admin} has been told.")
            self._say(admin, f"HR rejected claim {claim.id} from {claim.employee_id} ({claim.amount}). "
                             f"Please follow up.")
        elif now - claim.submitted_at > timedelta(days=self.policy.claim_sla_days) and not claim.overdue_notified:
            claim.overdue_notified = True
            self._say(claim.employee_id, f"Claim {claim.id} is overdue at HR; {admin} will chase it.")
            self._say(admin, f"Claim {claim.id} has been with HR for over {self.policy.claim_sla_days} days "
                             f"(overdue). Please chase.")
