"""The two small building blocks everything else leans on: exact money, and working-time deadlines."""

import unittest
from datetime import date, datetime, timezone

from snackcounter import FxRates, Money, WorkCalendar
from tests.world import FX, INR, NOK, OSLO_TZ

UTC = timezone.utc


class MoneyRules(unittest.TestCase):
    def test_amounts_are_exact_integers_of_minor_units(self):
        self.assertEqual(NOK("1200.50").minor, 120050)
        self.assertEqual(NOK("0.10") + NOK("0.20"), NOK("0.30"))  # the float trap: 0.1 + 0.2
        self.assertEqual(str(NOK("1200.5")), "1,200.50 NOK")

    def test_floats_are_refused(self):
        with self.assertRaises(TypeError):
            Money.of(0.1, "NOK")

    def test_currencies_never_mix_silently(self):
        with self.assertRaises(ValueError):
            NOK("1") + INR("1")
        with self.assertRaises(ValueError):
            NOK("1") < INR("1")
        with self.assertRaises(ValueError):
            Money.of("1", "EUR")  # unsupported until added on purpose

    def test_ordering_within_a_currency(self):
        self.assertTrue(NOK("300") < NOK("1200"))
        self.assertEqual(min(NOK("1200"), NOK("900")), NOK("900"))
        self.assertTrue((NOK("100") - NOK("300")).minor < 0)  # a budget can go negative


class FixedRateConversion(unittest.TestCase):
    def test_converts_at_the_fixed_rate(self):
        self.assertEqual(FX.convert(INR("15000")), NOK("1875"))
        self.assertEqual(FX.convert(NOK("6000")), NOK("6000"))  # reporting currency is untouched

    def test_rounds_half_up_to_the_minor_unit(self):
        self.assertEqual(FX.convert(INR("100.04")), NOK("12.51"))  # 12.505 -> 12.51

    def test_converting_a_subtotal_differs_from_summing_converted_purchases(self):
        # This is why the report converts each office's subtotal once.
        each = FX.convert(INR("100.04")) + FX.convert(INR("100.04"))
        once = FX.convert(INR("200.08"))
        self.assertEqual((each, once), (NOK("25.02"), NOK("25.01")))

    def test_missing_rate_is_an_error_not_a_guess(self):
        with self.assertRaises(ValueError):
            FxRates("NOK", {}).convert(INR("1"))


class WorkingTime(unittest.TestCase):
    def setUp(self):
        self.cal = WorkCalendar(OSLO_TZ)  # Mon-Fri 09:00-17:00, UTC+1

    def at(self, y, m, d, hh, mm=0):
        """A moment given in Oslo local time, as UTC."""
        return datetime(y, m, d, hh, mm, tzinfo=OSLO_TZ).astimezone(UTC)

    def test_inside_the_working_day_it_is_plain_addition(self):
        self.assertEqual(self.cal.add_working_hours(self.at(2026, 11, 9, 9), 4), self.at(2026, 11, 9, 13))

    def test_it_stops_at_closing_time_and_resumes_next_morning(self):
        self.assertEqual(self.cal.add_working_hours(self.at(2026, 11, 9, 16), 4), self.at(2026, 11, 10, 12))

    def test_weekends_do_not_count(self):
        # Friday 16:00 + 4h = 1h on Friday + 3h on Monday
        self.assertEqual(self.cal.add_working_hours(self.at(2026, 11, 13, 16), 4), self.at(2026, 11, 16, 12))

    def test_holidays_do_not_count(self):
        cal = WorkCalendar(OSLO_TZ, holidays=frozenset({date(2026, 11, 16)}))  # the Monday is a holiday
        self.assertEqual(cal.add_working_hours(self.at(2026, 11, 13, 16), 4), self.at(2026, 11, 17, 12))

    def test_a_start_outside_working_hours_begins_at_the_next_opening(self):
        before_opening = self.cal.add_working_hours(self.at(2026, 11, 9, 6), 2)
        on_a_saturday = self.cal.add_working_hours(self.at(2026, 11, 14, 12), 2)
        self.assertEqual(before_opening, self.at(2026, 11, 9, 11))
        self.assertEqual(on_a_saturday, self.at(2026, 11, 16, 11))

    def test_a_full_working_day_ends_exactly_at_closing(self):
        self.assertEqual(self.cal.add_working_hours(self.at(2026, 11, 9, 9), 8), self.at(2026, 11, 9, 17))

    def test_daylight_saving_is_respected_with_a_real_timezone(self):
        try:
            from zoneinfo import ZoneInfo
            oslo = ZoneInfo("Europe/Oslo")
        except Exception:  # no tz database on this machine
            self.skipTest("tz database not available")
        cal = WorkCalendar(oslo)
        # Clocks go forward on Sunday 29 March 2026 (UTC+1 -> UTC+2). Friday 16:00 + 4h lands on Monday 12:00 local.
        start = datetime(2026, 3, 27, 16, 0, tzinfo=oslo)
        self.assertEqual(cal.add_working_hours(start, 4), datetime(2026, 3, 30, 12, 0, tzinfo=oslo).astimezone(UTC))


if __name__ == "__main__":
    unittest.main()
