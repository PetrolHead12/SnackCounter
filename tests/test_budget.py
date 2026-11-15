"""The budget check: ceiling before the purchase, receipt check after it, both currencies."""

import unittest
from datetime import datetime, timezone

from snackcounter import ClaimState, Escalation, RefillState
from snackcounter.budget import ABOVE_CAP, OVER_BUDGET, BudgetLedger
from snackcounter.service import NotAdmin
from tests.world import INR, NOK, NOVEMBER, World


class CeilingBeforeThePurchase(unittest.TestCase):
    def test_full_cap_while_the_month_is_young(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-1")
        self.assertEqual(refill.ceiling, NOK("1200"))
        self.assertIn("1,200.00 NOK", w.chat.to(refill.assignee_id)[0])  # the volunteer is told

    def test_ceiling_shrinks_to_what_is_left_of_the_month(self):
        w = World(oslo_budget="2000")
        w.restock("OSL", NOK("1100"))
        refill = w.svc.report_empty("OSL", "emp-1")
        self.assertEqual(refill.ceiling, NOK("900"))

    def test_each_office_uses_its_own_budget_and_currency(self):
        w = World()
        self.assertEqual(w.svc.report_empty("BLR", "emp-1").ceiling, INR("3000"))
        w.restock("OSL", NOK("1000"))
        self.assertEqual(w.svc.ledger.spent(w.blr, NOVEMBER), INR("0"))  # Oslo spending never touches Bengaluru


class ReceiptCheckAfterThePurchase(unittest.TestCase):
    def test_receipt_above_the_cap_is_held_until_the_admin_approves(self):
        w = World()
        refill, claim = w.restock("OSL", NOK("1500"))  # cap is 1,200
        self.assertEqual(refill.state, RefillState.STOCKED)  # the counter is full either way
        self.assertEqual(claim.state, ClaimState.HELD)
        self.assertEqual(len(w.hr.claims), 0)                # HR has not seen it
        # it is real money already spent: it counts as spent, and is also visible as held
        self.assertEqual(w.svc.ledger.spent(w.oslo, NOVEMBER), NOK("1500"))
        self.assertEqual(w.svc.ledger.held(w.oslo, NOVEMBER), NOK("1500"))
        self.assertTrue(any(ABOVE_CAP in m for m in w.chat.to("admin-osl")))

        with self.assertRaises(NotAdmin):
            w.svc.approve_held(refill.id, claim.employee_id)
        w.svc.approve_held(refill.id, "admin-osl")
        self.assertEqual(claim.state, ClaimState.SUBMITTED)
        self.assertEqual(len(w.hr.claims), 1)
        self.assertEqual(w.svc.ledger.spent(w.oslo, NOVEMBER), NOK("1500"))  # approval changes what HR sees,
        self.assertEqual(w.svc.ledger.held(w.oslo, NOVEMBER), NOK("0"))      # not what was spent

    def test_receipt_that_would_break_the_monthly_budget_is_held(self):
        w = World(oslo_budget="2000")
        w.restock("OSL", NOK("1100"))                        # 900 left
        _, claim = w.restock("OSL", NOK("1000"))             # within the 1,200 cap, but over what is left
        self.assertEqual(claim.state, ClaimState.HELD)
        self.assertTrue(any(OVER_BUDGET in m for m in w.chat.to("admin-osl")))
        self.assertEqual(w.svc.ledger.spent(w.oslo, NOVEMBER), NOK("2100"))  # the 1,000 was bought: it counts
        self.assertEqual(w.svc.ledger.held(w.oslo, NOVEMBER), NOK("1000"))

    def test_receipt_within_cap_and_budget_goes_straight_to_hr(self):
        w = World()
        _, claim = w.restock("OSL", NOK("1200"))             # exactly the cap is fine
        self.assertEqual(claim.state, ClaimState.SUBMITTED)

    def test_bengaluru_works_the_same_in_rupees(self):
        w = World(blr_budget="5000")
        _, ok = w.restock("BLR", INR("3000"))
        _, held = w.restock("BLR", INR("2500"))              # only 2,000 left
        self.assertEqual((ok.state, held.state), (ClaimState.SUBMITTED, ClaimState.HELD))
        self.assertEqual(w.svc.ledger.spent(w.blr, NOVEMBER), INR("5500"))
        self.assertEqual(w.svc.ledger.held(w.blr, NOVEMBER), INR("2500"))
        self.assertEqual(w.svc.ledger.remaining(w.blr, NOVEMBER), INR("-500"))  # overspent, and visibly so


class HeldPurchasesStillCount(unittest.TestCase):
    """A purchase waiting for admin approval is already spent: the budget must see it."""

    def test_a_held_purchase_uses_up_the_budget_for_the_next_receipt(self):
        w = World(oslo_budget="2000")
        _, first = w.restock("OSL", NOK("1500"))  # above the 1,200 cap: held
        self.assertEqual(first.state, ClaimState.HELD)
        self.assertEqual(w.svc.ledger.remaining(w.oslo, NOVEMBER), NOK("500"))
        refill = w.svc.report_empty("OSL", "emp-1")
        self.assertEqual(refill.ceiling, NOK("500"))  # the next buyer is told what is really left
        w.svc.accept(refill.id, refill.assignee_id)
        second = w.svc.complete(refill.id, refill.assignee_id, NOK("600"), b"next", [])
        # 600 is within the cap but beyond the 500 really left. If the held 1,500 were ignored this would
        # look like 600 of 2,000 and go straight to HR.
        self.assertEqual(second.state, ClaimState.HELD)
        self.assertEqual(w.svc.ledger.spent(w.oslo, NOVEMBER), NOK("2100"))
        self.assertEqual(w.svc.ledger.held(w.oslo, NOVEMBER), NOK("2100"))

    def test_held_purchases_can_use_up_the_budget_and_send_the_next_refill_to_the_admin(self):
        w = World(oslo_budget="2000")
        w.restock("OSL", NOK("1100"))
        _, held = w.restock("OSL", NOK("1000"))  # over what was left: held
        self.assertEqual(held.state, ClaimState.HELD)
        self.assertEqual(w.svc.ledger.remaining(w.oslo, NOVEMBER), NOK("-100"))
        refill = w.svc.report_empty("OSL", "emp-1")
        self.assertEqual((refill.state, refill.escalation), (RefillState.ESCALATED, Escalation.BUDGET))
        self.assertIsNone(refill.assignee_id)  # nobody is asked to spend money that is not there

    def test_approving_a_held_purchase_does_not_change_what_is_left(self):
        w = World()
        refill, _ = w.restock("OSL", NOK("1500"))
        before = w.svc.ledger.remaining(w.oslo, NOVEMBER)
        w.svc.approve_held(refill.id, "admin-osl")
        self.assertEqual(w.svc.ledger.remaining(w.oslo, NOVEMBER), before)
        self.assertEqual(before, NOK("4500"))

    def test_held_purchases_stay_in_their_own_office_and_month(self):
        w = World(oslo_budget="2000")
        w.restock("OSL", NOK("1500"))  # held, in Oslo, in November
        self.assertEqual(w.svc.ledger.spent(w.blr, NOVEMBER), INR("0"))
        w.clock.set(datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(w.svc.report_empty("OSL", "emp-1").ceiling, NOK("1200"))  # December starts clean


class BudgetExhausted(unittest.TestCase):
    def test_exhausted_budget_goes_to_the_admin_whose_decision_is_the_approval(self):
        w = World()
        for _ in range(5):
            w.restock("OSL", NOK("1180"))                    # 5,900 of 6,000
        self.assertEqual(w.svc.ledger.remaining(w.oslo, NOVEMBER), NOK("100"))

        refill = w.svc.report_empty("OSL", "emp-1")          # 100 is below the smallest typical purchase
        self.assertEqual((refill.state, refill.escalation), (RefillState.ESCALATED, Escalation.BUDGET))
        self.assertIsNone(refill.assignee_id)                # nobody was asked to spend money that is not there
        self.assertTrue(any("budget" in m for m in w.chat.to("admin-osl")))

        w.svc.admin_assign(refill.id, "admin-osl", "o2")
        claim = w.svc.complete(refill.id, "o2", NOK("800"), b"special", [])
        self.assertEqual(claim.state, ClaimState.SUBMITTED)  # pre-approved by the admin's decision
        self.assertEqual(w.svc.ledger.spent(w.oslo, NOVEMBER), NOK("6700"))

    def test_the_cap_still_applies_after_an_admin_override(self):
        w = World()
        for _ in range(5):
            w.restock("OSL", NOK("1180"))
        refill = w.svc.report_empty("OSL", "emp-1")
        w.svc.admin_assign(refill.id, "admin-osl", "o2")
        claim = w.svc.complete(refill.id, "o2", NOK("1500"), b"big", [])
        self.assertEqual(claim.state, ClaimState.HELD)


class MonthsAndTimezones(unittest.TestCase):
    def test_a_new_month_starts_with_a_fresh_budget_and_no_rollover(self):
        w = World(oslo_budget="2000")
        w.restock("OSL", NOK("1100"))
        w.clock.set(datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(w.svc.report_empty("OSL", "emp-1").ceiling, NOK("1200"))  # full again, not 900 + 2000

    def test_the_month_is_the_offices_own_local_month(self):
        w = World()
        late_october_utc = datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(BudgetLedger.month_of(w.oslo, late_october_utc), (2026, 10))  # 23:30 in Oslo
        self.assertEqual(BudgetLedger.month_of(w.blr, late_october_utc), (2026, 11))   # 04:00 in Bengaluru

    def test_a_purchase_is_booked_in_the_month_it_was_made(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-1")
        w.svc.accept(refill.id, refill.assignee_id)
        bought = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)  # bought on the 31st, reported on the 9th
        w.svc.complete(refill.id, refill.assignee_id, NOK("500"), b"old", [], purchased_at=bought)
        self.assertEqual(w.svc.ledger.spent(w.oslo, (2026, 10)), NOK("500"))
        self.assertEqual(w.svc.ledger.spent(w.oslo, NOVEMBER), NOK("0"))


if __name__ == "__main__":
    unittest.main()
