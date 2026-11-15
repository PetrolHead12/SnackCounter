"""The refill lifecycle: who may commit, what happens when nobody delivers, and the receipt rules."""

import sys
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

from snackcounter import ClaimState, Escalation, RefillState, SnackLine
from snackcounter.service import (
    CommitmentExpired, InvalidReceipt, InvalidState, NotAdmin, NotEligible, NotYourTurn, OfferExpired,
    RefillError,
)
from tests.world import INR, NOK, NOVEMBER, OSLO_TZ, World


class HappyPath(unittest.TestCase):
    def test_empty_to_hr_approved(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-17", ["Chips "])
        self.assertEqual(refill.state, RefillState.OFFERED)
        buyer = refill.assignee_id
        w.svc.accept(refill.id, buyer)

        claim = w.svc.complete(refill.id, buyer, NOK("640"), b"receipt-1", [SnackLine("Chips", 6)])

        self.assertEqual(refill.state, RefillState.STOCKED)
        self.assertEqual(claim.state, ClaimState.SUBMITTED)  # filed with HR at once; nobody has to remember
        record = w.hr.claims[claim.hr_claim_id]
        self.assertEqual((record.employee_id, record.amount), (buyer, NOK("640")))
        self.assertIsNone(claim.receipt)                      # image dropped once HR has it

        w.clock.advance(timedelta(days=3))
        w.svc.tick()
        self.assertEqual(claim.state, ClaimState.APPROVED)
        approval = [m for m in w.chat.to(buyer) if "HR approved" in m]
        self.assertEqual(len(approval), 1)
        # HR approving is all we can observe. Whether payroll has paid out is outside this integration.
        self.assertNotIn("will be paid", approval[0])
        self.assertTrue(any("restocked" in m for m in w.chat.to("emp-17")))  # the reporter hears back
        self.assertIsNone(w.svc.open_refill("OSL"))           # the counter can be reported empty again


class OneBuyerPerRefill(unittest.TestCase):
    def setUp(self):
        # Switch threads as often as possible so a missing lock would show up as a failure here.
        self._interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)

    def tearDown(self):
        sys.setswitchinterval(self._interval)

    def test_many_reports_of_the_same_empty_box_make_one_refill_and_one_offer(self):
        w = World()
        a = w.svc.report_empty("OSL", "emp-1", ["nuts"])
        b = w.svc.report_empty("OSL", "emp-2", ["chips"])
        self.assertIs(a, b)
        self.assertEqual(len(w.svc.refills), 1)
        self.assertEqual(a.reporters, ["emp-1", "emp-2"])
        self.assertEqual(a.empty_items, {"nuts", "chips"})
        self.assertEqual(len(w.chat.to(a.assignee_id)), 1)

    def test_simultaneous_reports_still_make_one_refill(self):
        w = World()
        barrier = threading.Barrier(10)

        def report(i):
            barrier.wait()
            w.svc.report_empty("OSL", f"emp-{i}")

        threads = [threading.Thread(target=report, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(w.svc.refills), 1)

    def test_only_the_offered_volunteer_can_commit(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-1")
        offered, other = refill.assignee_id, "o5"
        self.assertNotEqual(offered, other)
        with self.assertRaises(NotYourTurn):
            w.svc.accept(refill.id, other)
        w.svc.accept(refill.id, offered)
        w.svc.accept(refill.id, offered)  # double click is harmless
        with self.assertRaises(NotYourTurn):
            w.svc.accept(refill.id, other)
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, offered))

    def test_race_at_the_hand_over_never_commits_two_people(self):
        # The offer to o1 has just lapsed. o1 says yes, o2 says yes, and tick() hands the offer on,
        # all at the same instant. Whatever the interleaving, at most one person ends up committed.
        for _ in range(60):
            w = World(n_oslo=3)
            refill = w.svc.report_empty("OSL", "emp-1")  # offered to o1
            w.chat.delay = 0.0005  # chat calls are slow, so other threads get a chance mid-operation
            w.clock.set(refill.deadline + timedelta(minutes=1))
            wins, barrier = [], threading.Barrier(3)

            def try_accept(who):
                barrier.wait()
                try:
                    w.svc.accept(refill.id, who)
                    wins.append(who)
                except NotYourTurn:
                    pass

            def tick():
                barrier.wait()
                w.svc.tick()

            threads = [threading.Thread(target=try_accept, args=(who,)) for who in ("o1", "o2")]
            threads.append(threading.Thread(target=tick))
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertLessEqual(len(wins), 1)
            if wins:
                self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, wins[0]))
            else:
                self.assertEqual(refill.state, RefillState.OFFERED)


    def test_a_late_yes_racing_with_tick_hands_the_refill_over_only_once(self):
        # The late "yes" and tick() both notice the lapsed offer at the same instant. Exactly one of them
        # does the hand-over: one new person is asked, nobody is skipped, and the late yes never succeeds.
        for _ in range(60):
            w = World(n_oslo=3)
            refill = w.svc.report_empty("OSL", "emp-1")  # offered to o1
            w.chat.delay = 0.0005
            w.clock.set(refill.deadline + timedelta(minutes=1))
            barrier, accepted = threading.Barrier(2), []

            def late_yes():
                barrier.wait()
                try:
                    w.svc.accept(refill.id, "o1")
                    accepted.append("o1")  # must never happen
                except NotYourTurn:
                    pass

            def tick():
                barrier.wait()
                w.svc.tick()

            threads = [threading.Thread(target=late_yes), threading.Thread(target=tick)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(accepted, [])
            self.assertEqual((refill.state, refill.assignee_id, refill.attempts), (RefillState.OFFERED, "o2", 2))
            self.assertEqual(refill.excluded, {"o1"})


class NobodyDelivers(unittest.TestCase):
    def test_declined_offer_moves_on_and_never_comes_back(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-1")
        first = refill.assignee_id
        w.svc.decline(refill.id, first)
        second = refill.assignee_id
        w.svc.decline(refill.id, second)
        third = refill.assignee_id
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(refill.excluded, {first, second})

    def test_unanswered_offer_is_passed_on_after_the_accept_window(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-1")
        first = refill.assignee_id
        w.clock.advance(timedelta(hours=3, minutes=59))
        w.svc.tick()
        self.assertEqual(refill.assignee_id, first)  # still inside 4 working hours
        w.clock.advance(timedelta(minutes=2))
        w.svc.tick()
        self.assertNotEqual(refill.assignee_id, first)
        self.assertIn(first, refill.excluded)
        with self.assertRaises(NotYourTurn):
            w.svc.accept(refill.id, first)  # too late

    def test_a_refill_nobody_completes_is_handed_to_someone_else(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-1")
        first = refill.assignee_id
        w.svc.accept(refill.id, first)
        w.clock.advance(timedelta(hours=7, minutes=59))
        w.svc.tick()
        self.assertEqual(refill.assignee_id, first)  # 8 working hours not used up yet
        w.clock.advance(timedelta(minutes=1))
        w.svc.tick()

        self.assertEqual(refill.state, RefillState.OFFERED)
        second = refill.assignee_id
        self.assertNotEqual(second, first)
        self.assertTrue(any("passed on" in m for m in w.chat.to(first)))
        with self.assertRaises(NotYourTurn):  # nobody can be paid twice for one refill
            w.svc.complete(refill.id, first, NOK("500"), b"late", [])

        w.svc.accept(refill.id, second)
        w.svc.complete(refill.id, second, NOK("500"), b"on-time", [])
        self.assertEqual([r.employee_id for r in w.hr.claims.values()], [second])

    def test_release_hands_over_immediately(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-1")
        first = refill.assignee_id
        w.svc.accept(refill.id, first)
        w.svc.release(refill.id, first)
        self.assertEqual(refill.state, RefillState.OFFERED)
        self.assertNotEqual(refill.assignee_id, first)

    def test_deadlines_count_working_time_not_wall_clock(self):
        # Friday 16:00 in Oslo: 4 working hours run out on Monday at 12:00, not on Friday night.
        friday = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
        w = World(start=friday)
        refill = w.svc.report_empty("OSL", "emp-1")
        first = refill.assignee_id
        w.clock.set(datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc))  # Saturday noon
        w.svc.tick()
        self.assertEqual(refill.assignee_id, first)
        w.clock.set(datetime(2026, 11, 16, 10, 59, tzinfo=timezone.utc))  # Monday 11:59 Oslo
        w.svc.tick()
        self.assertEqual(refill.assignee_id, first)
        w.clock.set(datetime(2026, 11, 16, 11, 1, tzinfo=timezone.utc))   # Monday 12:01 Oslo
        w.svc.tick()
        self.assertNotEqual(refill.assignee_id, first)


class EscalationToAdmin(unittest.TestCase):
    def test_admin_is_pulled_in_after_max_attempts(self):
        w = World(n_oslo=5)
        refill = w.svc.report_empty("OSL", "emp-1")
        for _ in range(3):
            w.run_out_the_clock(refill)  # nobody answers, three times
        self.assertEqual((refill.state, refill.escalation), (RefillState.ESCALATED, Escalation.NO_VOLUNTEER))
        self.assertEqual(refill.attempts, 3)
        self.assertTrue(any("needs you" in m for m in w.chat.to("admin-osl")))

        with self.assertRaises(NotAdmin):
            w.svc.admin_assign(refill.id, "o1", "o5")
        w.svc.admin_assign(refill.id, "admin-osl", "o5")
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, "o5"))

    def test_everybody_on_leave_goes_straight_to_the_admin(self):
        w = World(n_oslo=3)
        for v in ("o1", "o2", "o3"):
            w.svc.set_leave(v, date(2026, 11, 9), date(2026, 11, 20))
        refill = w.svc.report_empty("OSL", "emp-1")
        self.assertEqual((refill.state, refill.escalation), (RefillState.ESCALATED, Escalation.NO_VOLUNTEER))
        self.assertTrue(w.chat.to("admin-osl"))

    def test_admin_can_cancel_a_false_alarm(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-1")
        w.svc.admin_cancel(refill.id, "admin-osl", "box was not empty")
        self.assertEqual(refill.state, RefillState.CANCELLED)
        self.assertIsNone(w.svc.open_refill("OSL"))
        with self.assertRaises(InvalidState):
            w.svc.admin_cancel(refill.id, "admin-osl", "again")


class OfferDeadline(unittest.TestCase):
    """accept() enforces the offer deadline itself. It does not wait for tick() to notice."""

    def offered(self, w):
        refill = w.svc.report_empty("OSL", "emp-1")
        return refill, refill.assignee_id

    def test_a_late_yes_is_refused_even_though_tick_has_not_run(self):
        w = World()
        refill, late = self.offered(w)
        w.clock.set(refill.deadline + timedelta(minutes=1))  # the offer has run out and nobody called tick()
        with self.assertRaises(OfferExpired) as refused:
            w.svc.accept(refill.id, late)
        self.assertIsInstance(refused.exception, NotYourTurn)  # callers that handle NotYourTurn still work
        self.assertIn("ran out", str(refused.exception))
        self.assertEqual(refill.state, RefillState.OFFERED)    # not committed to the late volunteer
        self.assertNotEqual(refill.assignee_id, late)

    def test_the_refill_goes_to_the_next_eligible_volunteer(self):
        w = World()
        refill, late = self.offered(w)
        w.clock.set(refill.deadline + timedelta(minutes=1))
        with self.assertRaises(OfferExpired):
            w.svc.accept(refill.id, late)
        nxt = refill.assignee_id
        self.assertNotEqual(nxt, late)
        self.assertEqual((refill.state, refill.attempts, refill.excluded), (RefillState.OFFERED, 2, {late}))
        self.assertGreater(refill.deadline, w.clock.now())  # the next person gets a fresh, full window
        self.assertTrue(any("passed on" in m for m in w.chat.to(late)))            # the late volunteer is told
        self.assertTrue(any("snack counter is empty" in m for m in w.chat.to(nxt)))  # the next one is asked
        w.svc.accept(refill.id, nxt)
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, nxt))

    def test_the_late_volunteer_cannot_accept_the_same_offer_afterwards(self):
        w = World()
        refill, late = self.offered(w)
        w.clock.set(refill.deadline + timedelta(minutes=1))
        with self.assertRaises(OfferExpired):
            w.svc.accept(refill.id, late)
        second = refill.assignee_id
        with self.assertRaises(NotYourTurn):  # asking again changes nothing: the offer is no longer theirs
            w.svc.accept(refill.id, late)
        w.svc.tick()                          # and the next tick does not undo the hand-over
        with self.assertRaises(NotYourTurn):
            w.svc.accept(refill.id, late)
        with self.assertRaises(NotYourTurn):  # nor can they report a purchase for it
            w.svc.complete(refill.id, late, NOK("500"), b"receipt", [])
        self.assertEqual((refill.state, refill.assignee_id, refill.attempts), (RefillState.OFFERED, second, 2))
        # nor does it come back to them later: when the next person declines it goes to a third
        w.svc.decline(refill.id, second)
        self.assertNotIn(refill.assignee_id, {late, second})
        self.assertEqual(refill.excluded, {late, second})

    def test_a_late_yes_hands_the_refill_over_exactly_as_tick_would(self):
        outcomes = []
        for late_yes_first in (True, False):
            w = World()
            refill, late = self.offered(w)
            w.clock.set(refill.deadline + timedelta(minutes=1))
            if late_yes_first:
                with self.assertRaises(OfferExpired):
                    w.svc.accept(refill.id, late)
            else:
                w.svc.tick()
            outcomes.append((refill.state, refill.assignee_id, refill.deadline, refill.attempts,
                             sorted(refill.excluded), list(w.chat.sent)))  # same state, same messages
        self.assertEqual(outcomes[0], outcomes[1])

    def test_one_minute_before_the_deadline_a_yes_is_still_accepted(self):
        w = World()
        refill, who = self.offered(w)
        w.clock.set(refill.deadline - timedelta(minutes=1))
        w.svc.accept(refill.id, who)
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, who))

    def test_at_the_deadline_accept_and_tick_agree_that_time_is_up(self):
        for via in ("accept", "tick"):
            with self.subTest(via=via):
                w = World()
                refill, late = self.offered(w)
                w.clock.set(refill.deadline)  # exactly the instant the offer runs out
                if via == "accept":
                    with self.assertRaises(OfferExpired):
                        w.svc.accept(refill.id, late)
                else:
                    w.svc.tick()
                self.assertNotEqual(refill.assignee_id, late)

    def test_when_nobody_is_left_a_late_yes_sends_the_refill_to_the_admin(self):
        w = World(n_oslo=1)
        refill, late = self.offered(w)  # only o1 exists
        w.clock.set(refill.deadline + timedelta(minutes=1))
        with self.assertRaises(OfferExpired):
            w.svc.accept(refill.id, late)
        self.assertEqual((refill.state, refill.escalation), (RefillState.ESCALATED, Escalation.NO_VOLUNTEER))
        self.assertTrue(any("needs you" in m for m in w.chat.to("admin-osl")))
        with self.assertRaises(NotYourTurn):
            w.svc.accept(refill.id, late)

    def test_a_double_click_after_a_timely_yes_is_not_mistaken_for_a_late_one(self):
        w = World()
        refill, who = self.offered(w)
        offer_deadline = refill.deadline
        w.clock.set(offer_deadline - timedelta(minutes=1))
        w.svc.accept(refill.id, who)                          # in time
        w.clock.set(offer_deadline + timedelta(minutes=30))   # the offer window has closed; the commitment has not
        w.svc.accept(refill.id, who)                          # the same click again, e.g. a retried request
        self.assertEqual((refill.state, refill.assignee_id, refill.attempts), (RefillState.COMMITTED, who, 1))
        self.assertFalse(any("passed on" in m for m in w.chat.to(who)))

    def test_a_stranger_clicking_after_expiry_changes_nothing(self):
        w = World()
        refill, offered = self.offered(w)
        w.clock.set(refill.deadline + timedelta(minutes=1))
        with self.assertRaises(NotYourTurn) as refused:
            w.svc.accept(refill.id, "o5")
        self.assertNotIsInstance(refused.exception, OfferExpired)  # only the offered person's own yes counts
        self.assertEqual((refill.state, refill.assignee_id, refill.attempts), (RefillState.OFFERED, offered, 1))


class CommitmentDeadline(unittest.TestCase):
    """complete() enforces the deadline itself. It does not wait for tick() to notice."""

    def committed(self, w):
        refill = w.svc.report_empty("OSL", "emp-1")
        buyer = refill.assignee_id
        w.svc.accept(refill.id, buyer)
        return refill, buyer

    def test_a_late_receipt_is_refused_even_though_tick_has_not_run(self):
        w = World()
        refill, buyer = self.committed(w)
        w.clock.set(refill.deadline + timedelta(minutes=1))  # past due, and nobody has called tick()
        with self.assertRaises(CommitmentExpired) as refused:
            w.svc.complete(refill.id, buyer, NOK("500"), b"late", [SnackLine("chips", 5)])
        self.assertIsInstance(refused.exception, NotYourTurn)  # callers that handle NotYourTurn still work
        self.assertIn("admin-osl", str(refused.exception))      # told who to talk to if they already bought
        # nothing was recorded anywhere
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, buyer))
        self.assertEqual((len(w.svc.claims), len(w.hr.claims), w.hr.create_calls), (0, 0, 0))
        self.assertEqual(w.svc.ledger.spent(w.oslo, NOVEMBER), NOK("0"))
        # the normal hand-over then happens on the next tick
        w.svc.tick()
        self.assertNotEqual(refill.assignee_id, buyer)
        self.assertIn(buyer, refill.excluded)

    def test_one_minute_before_the_deadline_is_still_accepted(self):
        w = World()
        refill, buyer = self.committed(w)
        w.clock.set(refill.deadline - timedelta(minutes=1))
        claim = w.svc.complete(refill.id, buyer, NOK("500"), b"in-time", [])
        self.assertEqual(claim.state, ClaimState.SUBMITTED)

    def test_at_the_deadline_complete_and_tick_agree_that_time_is_up(self):
        w = World()
        refill, buyer = self.committed(w)
        w.clock.set(refill.deadline)  # exactly the instant tick() would hand it over
        with self.assertRaises(CommitmentExpired):
            w.svc.complete(refill.id, buyer, NOK("500"), b"on-the-dot", [])
        w.svc.tick()
        self.assertNotEqual(refill.assignee_id, buyer)

    def test_a_retried_request_after_success_is_not_mistaken_for_a_late_one(self):
        w = World()
        refill, buyer = self.committed(w)
        claim = w.svc.complete(refill.id, buyer, NOK("500"), b"photo", [])
        w.clock.advance(timedelta(days=3))  # long past the old deadline
        self.assertIs(w.svc.complete(refill.id, buyer, NOK("500"), b"photo", []), claim)
        self.assertEqual(len(w.hr.claims), 1)


class AdminAssignmentRules(unittest.TestCase):
    """The admin may overrule fairness, never availability."""

    def nobody_answered(self, w):
        """An Oslo refill that three volunteers ignored, so it now sits with the admin."""
        refill = w.svc.report_empty("OSL", "emp-1")
        for _ in range(3):
            w.run_out_the_clock(refill)
        self.assertEqual(refill.state, RefillState.ESCALATED)
        return refill

    def budget_gone(self, w):
        """An Oslo refill escalated at once because the month's budget is used up (World(oslo_budget="1000"))."""
        w.restock("OSL", NOK("800"))  # 200 left, below the smallest typical purchase of 300
        refill = w.svc.report_empty("OSL", "emp-1")
        self.assertEqual((refill.state, refill.escalation), (RefillState.ESCALATED, Escalation.BUDGET))
        return refill

    @staticmethod
    def today(w):
        return w.clock.now().astimezone(OSLO_TZ).date()

    def test_someone_on_leave_cannot_be_assigned(self):
        w = World(n_oslo=5)
        refill = self.nobody_answered(w)
        today = self.today(w)
        w.svc.set_leave("o5", today, today + timedelta(days=3))
        with self.assertRaises(NotEligible) as refused:
            w.svc.admin_assign(refill.id, "admin-osl", "o5")
        self.assertIn("on leave", str(refused.exception))
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.ESCALATED, None))
        self.assertEqual(w.chat.to("o5"), [])  # o5 was never bothered

    def test_leave_covers_its_last_day_and_is_over_the_day_after(self):
        w = World(n_oslo=5)
        refill = self.nobody_answered(w)
        today = self.today(w)
        w.svc.set_leave("o4", today - timedelta(days=5), today)                        # back tomorrow
        w.svc.set_leave("o5", today - timedelta(days=5), today - timedelta(days=1))    # back today
        with self.assertRaises(NotEligible):
            w.svc.admin_assign(refill.id, "admin-osl", "o4")
        w.svc.admin_assign(refill.id, "admin-osl", "o5")
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, "o5"))

    def test_someone_from_the_other_office_cannot_be_assigned(self):
        w = World(n_oslo=5)
        refill = self.nobody_answered(w)
        with self.assertRaises(NotEligible) as refused:
            w.svc.admin_assign(refill.id, "admin-osl", "b1")
        self.assertIn("BLR", str(refused.exception))
        self.assertEqual(refill.state, RefillState.ESCALATED)

    def test_an_oslo_colleague_visiting_bengaluru_is_not_available_in_oslo(self):
        w = World(n_oslo=5)
        refill = self.nobody_answered(w)
        today = self.today(w)
        w.svc.register_visit("o5", "BLR", today, today + timedelta(days=10))
        with self.assertRaises(NotEligible):
            w.svc.admin_assign(refill.id, "admin-osl", "o5")

    def test_a_visiting_colleague_can_be_assigned_to_the_host_office(self):
        w = World(n_blr=3)
        for b in ("b1", "b2", "b3"):
            w.svc.set_leave(b, date(2026, 11, 9), date(2026, 11, 20))
        refill = w.svc.report_empty("BLR", "emp-1")  # nobody local is available: straight to the admin
        self.assertEqual(refill.state, RefillState.ESCALATED)
        with self.assertRaises(NotEligible):          # o1 is still in Oslo
            w.svc.admin_assign(refill.id, "admin-blr", "o1")
        w.svc.register_visit("o1", "BLR", date(2026, 11, 9), date(2026, 11, 20))
        w.svc.admin_assign(refill.id, "admin-blr", "o1")
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, "o1"))

    def test_the_admin_can_overrule_the_fairness_order(self):
        w = World(n_oslo=3, oslo_budget="1000")
        _, claim = w.restock("OSL", NOK("800"))  # o1 buys: now the busiest, and still waiting for HR
        self.assertEqual(claim.state, ClaimState.SUBMITTED)  # so the fairness order would put o1 last
        refill = w.svc.report_empty("OSL", "emp-1")
        self.assertEqual(refill.escalation, Escalation.BUDGET)
        w.svc.admin_assign(refill.id, "admin-osl", claim.employee_id)
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, claim.employee_id))

    def test_the_admin_may_pick_someone_who_was_already_asked_about_this_refill(self):
        # Who timed out earlier is the history of this refill, not availability: the admin may have phoned them.
        w = World(n_oslo=5)
        refill = self.nobody_answered(w)
        timed_out = sorted(refill.excluded)[0]
        w.svc.admin_assign(refill.id, "admin-osl", timed_out)
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.COMMITTED, timed_out))

    def test_a_refused_assignment_changes_nothing_not_even_the_budget_override(self):
        w = World(n_oslo=3, oslo_budget="1000")
        refill = self.budget_gone(w)
        today = self.today(w)
        w.svc.set_leave("o3", today, today)
        with self.assertRaises(NotEligible):
            w.svc.admin_assign(refill.id, "admin-osl", "o3")
        self.assertFalse(refill.over_budget_ok)  # the override is only granted with a valid assignment
        self.assertEqual((refill.state, refill.assignee_id), (RefillState.ESCALATED, None))
        w.svc.admin_assign(refill.id, "admin-osl", "o2")
        self.assertTrue(refill.over_budget_ok)

    def test_only_the_admin_can_assign_and_the_volunteer_must_exist(self):
        w = World(n_oslo=5)
        refill = self.nobody_answered(w)
        with self.assertRaises(NotAdmin):
            w.svc.admin_assign(refill.id, "o1", "o5")
        with self.assertRaises(RefillError):
            w.svc.admin_assign(refill.id, "admin-osl", "nobody")


class ReceiptRules(unittest.TestCase):
    def committed(self, w, office="OSL"):
        refill = w.svc.report_empty(office, "emp-1")
        w.svc.accept(refill.id, refill.assignee_id)
        return refill, refill.assignee_id

    def test_bad_receipts_are_refused_and_change_nothing(self):
        w = World()
        refill, buyer = self.committed(w)
        for amount, image in [(INR("500"), b"x"), (NOK("0"), b"x"), (NOK("500"), b"")]:
            with self.assertRaises(InvalidReceipt):  # wrong currency for Oslo / zero amount / no image
                w.svc.complete(refill.id, buyer, amount, image, [])
        with self.assertRaises(InvalidReceipt):
            w.svc.complete(refill.id, buyer, NOK("500"), b"x", [SnackLine("chips", 0)])
        self.assertEqual(refill.state, RefillState.COMMITTED)
        self.assertEqual(len(w.hr.claims), 0)

    def test_the_same_receipt_cannot_be_used_twice(self):
        w = World()
        refill, buyer = self.committed(w)
        w.svc.complete(refill.id, buyer, NOK("500"), b"same-photo", [])
        refill2, buyer2 = self.committed(w)
        with self.assertRaises(InvalidReceipt):
            w.svc.complete(refill2.id, buyer2, NOK("500"), b"same-photo", [])
        self.assertEqual(refill2.state, RefillState.COMMITTED)

    def test_submitting_twice_creates_one_claim(self):
        w = World()
        refill, buyer = self.committed(w)
        first = w.svc.complete(refill.id, buyer, NOK("500"), b"photo", [])
        again = w.svc.complete(refill.id, buyer, NOK("500"), b"photo", [])  # double click / retried webhook
        self.assertIs(first, again)
        self.assertEqual(len(w.hr.claims), 1)

    def test_someone_else_cannot_complete_your_refill(self):
        w = World()
        refill, buyer = self.committed(w)
        with self.assertRaises(NotYourTurn):
            w.svc.complete(refill.id, "o5", NOK("500"), b"photo", [])


if __name__ == "__main__":
    unittest.main()
