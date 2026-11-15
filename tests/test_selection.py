"""Choosing who buys: eligibility (leave, visitors) and fairness ordering."""

import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from snackcounter import SnackLine, Volunteer
from snackcounter.selection import Load, rank_candidates, unavailable_reason
from tests.world import NOK, START, World


def ids(volunteers):
    return [v.id for v in volunteers]


class Eligibility(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def rank(self, volunteers, office=None, excluded=(), loads=None, now=START):
        return ids(rank_candidates(volunteers, office or self.w.oslo, now, set(excluded), loads or {}))

    def test_people_on_leave_are_skipped_and_leave_dates_are_inclusive(self):
        today = date(2026, 11, 9)
        on_leave_today = Volunteer("a", "A", "OSL", leave=[(today, today)])
        leave_starts_tomorrow = Volunteer("b", "B", "OSL", leave=[(today + timedelta(days=1), date(2026, 11, 20))])
        back_yesterday = Volunteer("c", "C", "OSL", leave=[(date(2026, 11, 1), today - timedelta(days=1))])
        self.assertEqual(self.rank([on_leave_today, leave_starts_tomorrow, back_yesterday]), ["b", "c"])

    def test_a_visitor_belongs_to_the_host_office_while_visiting(self):
        visitor = Volunteer("v", "Vera", "OSL", visits=[("BLR", date(2026, 11, 9), date(2026, 11, 20))])
        self.assertEqual(self.rank([visitor], self.w.blr), ["v"])      # eligible where she is
        self.assertEqual(self.rank([visitor], self.w.oslo), [])        # not where she is not
        after = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(self.rank([visitor], self.w.oslo, now=after), ["v"])  # home again
        self.assertEqual(self.rank([visitor], self.w.blr, now=after), [])

    def test_colleagues_from_the_other_office_are_never_eligible(self):
        self.assertEqual(self.rank([Volunteer("x", "X", "BLR")]), [])

    def test_unavailable_reason_names_the_cause_and_is_none_for_someone_available(self):
        on_leave = Volunteer("a", "Asha", "OSL", leave=[(date(2026, 11, 9), date(2026, 11, 9))])
        elsewhere = Volunteer("b", "Bo", "BLR")
        free = Volunteer("c", "Cy", "OSL")
        self.assertIn("on leave", unavailable_reason(on_leave, self.w.oslo, START))
        self.assertIn("BLR", unavailable_reason(elsewhere, self.w.oslo, START))
        self.assertIsNone(unavailable_reason(free, self.w.oslo, START))

    def test_volunteers_already_asked_about_this_refill_are_excluded(self):
        pool = [Volunteer(i, i, "OSL") for i in "abc"]
        self.assertEqual(self.rank(pool, excluded={"a", "c"}), ["b"])


class Fairness(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.pool = [Volunteer(i, i, "OSL") for i in "abcd"]

    def rank(self, loads=None):
        return ids(rank_candidates(self.pool, self.w.oslo, START, set(), loads or {}))

    def test_whoever_has_done_fewest_refills_goes_first(self):
        loads = {"a": Load(completed=3), "b": Load(completed=0), "c": Load(completed=1), "d": Load(completed=2)}
        self.assertEqual(self.rank(loads), ["b", "c", "d", "a"])

    def test_someone_still_waiting_for_their_money_goes_last_even_if_they_have_done_least(self):
        loads = {"a": Load(completed=0, open_claims=1), "b": Load(completed=5)}
        # c and d (nothing outstanding, nothing done) first; b has done five but is not out of pocket;
        # a has done nothing but is still waiting for HR, so a comes last
        self.assertEqual(self.rank(loads), ["c", "d", "b", "a"])

    def test_among_equals_the_one_asked_longest_ago_goes_first(self):
        self.pool[0].last_offered_at = START - timedelta(days=1)
        self.pool[1].last_offered_at = START - timedelta(days=5)
        # c and d were never asked, so they go before both; then b (asked longest ago), then a
        self.assertEqual(self.rank(), ["c", "d", "b", "a"])

    def test_ties_break_by_id_so_results_are_repeatable(self):
        self.assertEqual(self.rank(), ["a", "b", "c", "d"])

    def test_nobody_is_ever_excluded_for_having_an_open_claim(self):
        loads = {v.id: Load(completed=9, open_claims=1) for v in self.pool}
        self.assertEqual(len(self.rank(loads)), 4)  # a refill can always find someone

    def test_over_many_refills_the_work_spreads_evenly(self):
        w = World(n_oslo=5)
        done = Counter()
        for i in range(20):
            refill = w.svc.report_empty("OSL", "emp-1")
            buyer = refill.assignee_id
            w.svc.accept(refill.id, buyer)
            w.svc.complete(refill.id, buyer, NOK("500"), f"r{i}".encode(), [SnackLine("chips", 5)])
            done[buyer] += 1
            w.clock.advance(timedelta(days=4))
            w.svc.tick()  # HR approves meanwhile, so nobody is waiting on money
        self.assertEqual(sorted(done.values()), [4, 4, 4, 4, 4])

    def test_a_volunteer_with_an_unpaid_claim_is_not_picked_next(self):
        w = World(n_oslo=3)
        _, claim = w.restock("OSL", NOK("500"))           # o1 buys; HR has not decided yet
        refill = w.svc.report_empty("OSL", "emp-1")
        self.assertNotEqual(refill.assignee_id, claim.employee_id)


if __name__ == "__main__":
    unittest.main()
