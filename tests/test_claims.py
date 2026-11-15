"""Claims to HR: what we send, how failures are retried without duplicates, and how outcomes are handled."""

import unittest
from datetime import date, timedelta

from snackcounter import ClaimState, SnackLine
from snackcounter.ports import HRStatus
from tests.world import INR, NOK, NOVEMBER, World


class WhatHRReceives(unittest.TestCase):
    def test_a_visitor_buys_in_the_host_currency_and_the_claim_stays_in_it(self):
        w = World()
        for b in ("b1", "b2", "b3", "b4", "b5"):  # every local is away, so the visiting Oslo colleague gets it
            w.svc.set_leave(b, date(2026, 11, 9), date(2026, 11, 20))
        w.svc.register_visit("o1", "BLR", date(2026, 11, 9), date(2026, 11, 20))

        refill = w.svc.report_empty("BLR", "emp-1")
        self.assertEqual(refill.assignee_id, "o1")
        w.svc.accept(refill.id, "o1")
        claim = w.svc.complete(refill.id, "o1", INR("1500"), b"blr-receipt", [SnackLine("namkeen", 6)])

        sent = w.hr.claims[claim.hr_claim_id]
        # rupees as bought; HR converts to her payroll currency
        self.assertEqual((sent.employee_id, sent.amount), ("o1", INR("1500")))
        self.assertEqual(w.svc.ledger.spent(w.blr, NOVEMBER), INR("1500"))       # it counts against Bengaluru's budget
        self.assertEqual(w.svc.ledger.spent(w.oslo, NOVEMBER), NOK("0"))

    def test_the_description_carries_our_reference_for_reconciliation(self):
        w = World()
        _, claim = w.restock("OSL", NOK("500"))
        self.assertIn(claim.id, w.hr.claims[claim.hr_claim_id].description)


class FailuresNeverDuplicate(unittest.TestCase):
    def test_an_outage_is_retried_until_it_works_and_only_one_claim_exists(self):
        w = World()
        w.hr.fail_next_creates = 2
        _, claim = w.restock("OSL", NOK("500"))
        self.assertEqual(claim.state, ClaimState.QUEUED)  # HR was down: kept, not lost
        w.svc.tick()
        self.assertEqual(claim.state, ClaimState.QUEUED)
        w.svc.tick()
        self.assertEqual(claim.state, ClaimState.SUBMITTED)
        self.assertEqual((w.hr.create_calls, len(w.hr.claims)), (3, 1))

    def test_a_lost_reply_is_retried_without_creating_a_second_claim(self):
        w = World()
        w.hr.lose_reply_next_creates = 1  # HR stored the claim, but we never heard back
        _, claim = w.restock("OSL", NOK("500"))
        self.assertEqual(claim.state, ClaimState.QUEUED)
        self.assertEqual(len(w.hr.claims), 1)             # it is already there
        w.svc.tick()
        self.assertEqual(claim.state, ClaimState.SUBMITTED)
        self.assertEqual(len(w.hr.claims), 1)             # the retry found it by reference instead of adding another

    def test_the_receipt_image_is_kept_until_hr_has_it(self):
        w = World()
        w.hr.fail_next_creates = 1
        _, claim = w.restock("OSL", NOK("500"))
        self.assertIsNotNone(claim.receipt)               # needed for the retry
        w.svc.tick()
        self.assertIsNone(claim.receipt)                  # dropped once HR accepted it


class HROutcomes(unittest.TestCase):
    def test_hr_decides_within_its_one_to_ten_days(self):
        for days in (1, 5, 10):
            with self.subTest(days=days):
                w = World()
                w.hr.approval_days = days
                _, claim = w.restock("OSL", NOK("500"))
                w.clock.advance(timedelta(days=days) - timedelta(minutes=1))
                w.svc.tick()
                self.assertEqual(claim.state, ClaimState.SUBMITTED)
                w.clock.advance(timedelta(minutes=1))
                w.svc.tick()
                self.assertEqual(claim.state, ClaimState.APPROVED)

    def test_a_rejected_claim_tells_the_volunteer_and_the_admin(self):
        w = World()
        refill, claim = w.restock("OSL", NOK("500"))
        w.hr.decide(claim.id, days=2, outcome=HRStatus.REJECTED)
        w.clock.advance(timedelta(days=2))
        w.svc.tick()
        self.assertEqual(claim.state, ClaimState.REJECTED)
        self.assertTrue(any("rejected" in m for m in w.chat.to(claim.employee_id)))
        self.assertTrue(any("rejected" in m for m in w.chat.to("admin-osl")))
        self.assertEqual(w.svc.ledger.spent(w.oslo, NOVEMBER), NOK("500"))  # the snacks were bought: still spend

    def test_a_claim_stuck_at_hr_is_chased_once_not_forever(self):
        w = World()
        _, claim = w.restock("OSL", NOK("500"))
        w.hr.decide(claim.id, days=30, outcome=HRStatus.APPROVED)
        w.clock.advance(timedelta(days=9))
        w.svc.tick()
        self.assertFalse(claim.overdue_notified)          # inside HR's stated 10 days
        w.clock.advance(timedelta(days=2))
        w.svc.tick()
        w.svc.tick()
        w.clock.advance(timedelta(days=1))
        w.svc.tick()
        overdue = [m for m in w.chat.to("admin-osl") if "overdue" in m]
        self.assertEqual(len(overdue), 1)
        self.assertTrue(any("overdue" in m for m in w.chat.to(claim.employee_id)))


if __name__ == "__main__":
    unittest.main()
