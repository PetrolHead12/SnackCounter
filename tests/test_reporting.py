"""The monthly report: totals across both currencies must be exact and reconcile."""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from snackcounter import SnackLine, render_text
from snackcounter.ports import HRStatus
from tests.world import FX, INR, NOK, World


class Totals(unittest.TestCase):
    def test_each_office_in_its_own_currency_and_one_total_in_the_reporting_currency(self):
        w = World()
        w.restock("OSL", NOK("1000"), items=[("chips", 10)])
        w.restock("BLR", INR("2000"), items=[("namkeen", 8)])
        report = w.svc.monthly_report(2026, 11, FX)
        oslo, blr = report.offices
        self.assertEqual((oslo.spent, blr.spent), (NOK("1000"), INR("2000")))
        self.assertEqual((oslo.budget, blr.budget), (NOK("6000"), INR("15000")))
        self.assertEqual((oslo.used_pct, blr.used_pct), (Decimal("16.7"), Decimal("13.3")))
        self.assertEqual(report.total, NOK("1250"))  # 1,000 NOK + 2,000 INR x 0.125

    def test_only_the_requested_month_is_counted(self):
        w = World()
        w.restock("OSL", NOK("1000"))
        w.clock.set(datetime(2026, 12, 2, 9, 0, tzinfo=timezone.utc))
        w.restock("OSL", NOK("700"))
        self.assertEqual(w.svc.monthly_report(2026, 11, FX).total, NOK("1000"))
        self.assertEqual(w.svc.monthly_report(2026, 12, FX).total, NOK("700"))

    def test_a_month_with_no_purchases_is_zero(self):
        w = World()
        report = w.svc.monthly_report(2026, 11, FX)
        self.assertEqual((report.total, report.held), (NOK("0"), NOK("0")))
        self.assertTrue(all(o.refills == 0 for o in report.offices))

    def test_a_held_purchase_is_in_the_office_total_and_also_shown_as_held(self):
        w = World()
        refill, _ = w.restock("OSL", NOK("1500"))  # above the 1,200 cap
        oslo = w.svc.monthly_report(2026, 11, FX).offices[0]
        self.assertEqual((oslo.spent, oslo.held), (NOK("1500"), NOK("1500")))
        w.svc.approve_held(refill.id, "admin-osl")
        oslo = w.svc.monthly_report(2026, 11, FX).offices[0]
        self.assertEqual((oslo.spent, oslo.held), (NOK("1500"), NOK("0")))

    def test_the_consolidated_total_includes_held_purchases_and_shows_them_separately(self):
        w = World()
        w.restock("OSL", NOK("1000"))  # straight to HR
        w.restock("OSL", NOK("1500"))  # above the 1,200 cap: held
        w.restock("BLR", INR("3400"))  # above the 3,000 cap: held
        report = w.svc.monthly_report(2026, 11, FX)
        oslo, blr = report.offices
        self.assertEqual((oslo.spent, oslo.held), (NOK("2500"), NOK("1500")))
        self.assertEqual((blr.spent, blr.held), (INR("3400"), INR("3400")))
        self.assertEqual(report.total, NOK("2925"))  # 2,500 NOK + 3,400 INR x 0.125 (= 425 NOK)
        self.assertEqual(report.held, NOK("1925"))   # 1,500 NOK + 425 NOK: identifiable inside the total

    def test_approving_a_held_purchase_moves_it_out_of_held_without_changing_the_total(self):
        w = World()
        refill, _ = w.restock("OSL", NOK("1500"))
        before = w.svc.monthly_report(2026, 11, FX)
        w.svc.approve_held(refill.id, "admin-osl")
        after = w.svc.monthly_report(2026, 11, FX)
        self.assertEqual((before.total, before.held), (NOK("1500"), NOK("1500")))
        self.assertEqual((after.total, after.held), (NOK("1500"), NOK("0")))

    def test_budget_use_counts_held_purchases(self):
        w = World()
        w.restock("OSL", NOK("1000"))
        w.restock("OSL", NOK("1500"))  # held
        oslo = w.svc.monthly_report(2026, 11, FX).offices[0]
        self.assertEqual(oslo.used_pct, Decimal("41.7"))  # 2,500 of 6,000, not 1,000 of 6,000


class Reconciliation(unittest.TestCase):
    def test_every_purchase_falls_in_exactly_one_bucket(self):
        w = World()
        w.restock("OSL", NOK("500"))                       # HR approves it
        _, rejected = w.restock("OSL", NOK("600"))         # HR rejects it
        _, waiting = w.restock("OSL", NOK("700"))          # HR has not decided
        w.restock("OSL", NOK("1500"))                      # above the cap: held for the admin
        w.hr.decide(rejected.id, days=2, outcome=HRStatus.REJECTED)
        w.hr.decide(waiting.id, days=30, outcome=HRStatus.APPROVED)
        w.clock.advance(timedelta(days=5))
        w.svc.tick()
        oslo = w.svc.monthly_report(2026, 11, FX).offices[0]
        self.assertEqual(oslo.hr_approved, NOK("500"))
        self.assertEqual(oslo.rejected, NOK("600"))
        self.assertEqual(oslo.awaiting_hr, NOK("700"))
        self.assertEqual(oslo.held, NOK("1500"))
        self.assertEqual(oslo.hr_approved + oslo.rejected + oslo.awaiting_hr + oslo.held, oslo.spent)
        self.assertEqual(oslo.spent, NOK("3300"))
        self.assertEqual(oslo.refills, 4)

    def test_the_report_total_is_the_converted_office_subtotals(self):
        w = World()
        w.restock("OSL", NOK("333.33"))
        w.restock("BLR", INR("1000.04"))
        w.restock("BLR", INR("1000.04"))
        report = w.svc.monthly_report(2026, 11, FX)
        oslo, blr = report.offices
        self.assertEqual(report.total, FX.convert(oslo.spent) + FX.convert(blr.spent))
        self.assertEqual(report.total, NOK("333.33") + NOK("250.01"))  # 2,000.08 INR x 0.125 = 250.01


class WhatGetsEaten(unittest.TestCase):
    def test_units_restocked_and_stockouts_are_counted_per_snack(self):
        w = World()
        refill = w.svc.report_empty("OSL", "emp-1", ["Chips", "nuts"])
        w.svc.report_empty("OSL", "emp-2", ["chips"])
        w.svc.accept(refill.id, refill.assignee_id)
        w.svc.complete(refill.id, refill.assignee_id, NOK("800"), b"r1",
                       [SnackLine("Chips", 6), SnackLine("nuts", 3), SnackLine("chips", 2)])
        w.restock("OSL", NOK("400"), items=[("chips", 4), ("fruit", 10)])
        oslo = w.svc.monthly_report(2026, 11, FX).offices[0]
        self.assertEqual(oslo.snack_units, {"chips": 12, "nuts": 3, "fruit": 10})
        self.assertEqual(oslo.stockouts, {"chips": 1, "nuts": 1})  # one refill counts a snack once

    def test_units_and_purchase_counts_include_held_purchases(self):
        w = World()
        w.restock("OSL", NOK("500"), items=[("chips", 5)])
        w.restock("OSL", NOK("1500"), items=[("fruit", 10)])  # held, but the snacks are in the box
        oslo = w.svc.monthly_report(2026, 11, FX).offices[0]
        self.assertEqual(oslo.refills, 2)
        self.assertEqual(oslo.snack_units, {"chips": 5, "fruit": 10})

    def test_the_text_report_shows_both_offices_the_total_and_the_held_part(self):
        w = World()
        w.restock("OSL", NOK("1000"))
        w.restock("BLR", INR("2000"))
        text = render_text(w.svc.monthly_report(2026, 11, FX))
        for expected in ("Oslo", "Bengaluru", "1,000.00 NOK", "2,000.00 INR", "HR approved",
                         "TOTAL spending, all offices: 1,250.00 NOK",
                         "of which held for admin approval: 0.00 NOK"):
            self.assertIn(expected, text)
        self.assertNotIn("Reimbursed", text)  # HR approval is not payment: the report must not say so

        w.restock("OSL", NOK("1500"))  # held
        text = render_text(w.svc.monthly_report(2026, 11, FX))
        self.assertIn("TOTAL spending, all offices: 2,750.00 NOK", text)
        self.assertIn("of which held for admin approval: 1,500.00 NOK", text)


if __name__ == "__main__":
    unittest.main()
