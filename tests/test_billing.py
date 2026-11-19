"""Table tests for app/billing.py (monthly billing periods).

Run: python -I -m unittest discover -s tests -p "test_billing.py"
"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import billing as B  # noqa: E402


class Periods(unittest.TestCase):
    def test_15th_to_14th(self):
        s = date(2026, 11, 15)
        self.assertEqual(B.period_end(15, s), date(2026, 12, 14))
        self.assertEqual(B.next_period_start(15, s), date(2026, 12, 15))
        self.assertEqual(B.period_label(15, s), "Nov 15 – Dec 14, 2026")

    def test_year_rollover_label(self):
        self.assertEqual(B.period_label(15, date(2026, 12, 15)), "Dec 15, 2026 – Jan 14, 2027")

    def test_first_of_month(self):
        s = date(2026, 2, 1)
        self.assertEqual(B.period_end(1, s), date(2026, 2, 28))
        self.assertEqual(B.schedule_label(1), "the 1st to the end of the month")

    def test_anchor_31_clamps_and_recovers(self):
        jan = date(2027, 1, 31)
        feb = B.next_period_start(31, jan)
        self.assertEqual(feb, date(2027, 2, 28))
        self.assertEqual(B.next_period_start(31, feb), date(2027, 3, 31))   # back to the 31st
        self.assertEqual(B.period_end(31, jan), date(2027, 2, 27))
        self.assertEqual(B.next_period_start(31, date(2028, 1, 31)), date(2028, 2, 29))  # leap year

    def test_schedule_labels(self):
        self.assertEqual(B.schedule_label(15), "the 15th to the 14th")
        self.assertEqual(B.schedule_label(22), "the 22nd to the 21st")
        self.assertEqual(B.schedule_label(12), "the 12th to the 11th")


class FirstPeriod(unittest.TestCase):
    def test_future_or_today_start_is_first_period(self):
        today = date(2026, 11, 19)
        self.assertEqual(B.first_period_start(date(2026, 12, 15), today), date(2026, 12, 15))
        self.assertEqual(B.first_period_start(today, today), today)

    def test_past_start_never_back_bills(self):
        today = date(2026, 11, 19)
        # started months ago on the 15th: begin with Nov 15 (contains today)
        self.assertEqual(B.first_period_start(date(2026, 6, 15), today), date(2026, 11, 15))
        # started on the 25th: Nov 25 is after today, so Oct 25 - Nov 24 contains today
        self.assertEqual(B.first_period_start(date(2026, 3, 25), today), date(2026, 10, 25))
        # started 4 days ago: that's the current period
        self.assertEqual(B.first_period_start(date(2026, 11, 15), today), date(2026, 11, 15))
        # January edge: previous month is December
        self.assertEqual(B.first_period_start(date(2025, 5, 20), date(2027, 1, 5)), date(2026, 12, 20))


class DuePeriods(unittest.TestCase):
    def test_nothing_due_before_start(self):
        self.assertEqual(B.due_period_starts(15, date(2026, 12, 15), date(2026, 11, 19), None), [])

    def test_due_on_start_day(self):
        self.assertEqual(B.due_period_starts(15, date(2026, 11, 15), date(2026, 11, 15), None), [date(2026, 11, 15)])

    def test_catch_up_after_downtime(self):
        self.assertEqual(B.due_period_starts(15, date(2026, 10, 15), date(2026, 11, 20), None),
                         [date(2026, 10, 15), date(2026, 11, 15)])

    def test_stop_after(self):
        self.assertEqual(B.due_period_starts(15, date(2026, 10, 15), date(2027, 1, 20), date(2026, 11, 30)),
                         [date(2026, 10, 15), date(2026, 11, 15)])


if __name__ == "__main__":
    unittest.main()
