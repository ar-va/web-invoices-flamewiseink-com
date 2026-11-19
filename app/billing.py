"""Monthly billing periods: pure date math, no database or web code.

A monthly plan has an anchor day (the day of the month its periods start).
A period runs from the anchor day to the day before the next month's anchor day,
e.g. anchor 15: Nov 15 - Dec 14. Anchor days past a month's end clamp to its last
day (anchor 31: Feb 28/29, then Mar 31).
"""
import calendar
from datetime import date, timedelta


def period_start_in(anchor_day: int, year: int, month: int) -> date:
    return date(year, month, min(anchor_day, calendar.monthrange(year, month)[1]))


def next_period_start(anchor_day: int, start: date) -> date:
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return period_start_in(anchor_day, year, month)


def period_end(anchor_day: int, start: date) -> date:
    return next_period_start(anchor_day, start) - timedelta(days=1)


def first_period_start(start_date: date, today: date) -> date:
    """Where billing begins for a plan starting on start_date.

    A future (or today's) start date is the first period. A start date in the
    past begins with the period that contains today, so creating a plan never
    back-bills months that already went by.
    """
    if start_date >= today:
        return start_date
    anchor = start_date.day
    current = period_start_in(anchor, today.year, today.month)
    if current > today:
        prev_year, prev_month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        current = period_start_in(anchor, prev_year, prev_month)
    return max(current, start_date)


def due_period_starts(anchor_day: int, next_start: date, today: date, stop_after: date | None) -> list[date]:
    """Period starts that should be billed now: on or before today, not after stop_after."""
    starts, cur = [], next_start
    while cur <= today and (stop_after is None or cur <= stop_after):
        starts.append(cur)
        cur = next_period_start(anchor_day, cur)
    return starts


def period_label(anchor_day: int, start: date) -> str:
    end = period_end(anchor_day, start)
    if start.year == end.year:
        return f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}"


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def schedule_label(anchor_day: int) -> str:
    """Plain-English period, e.g. 'the 15th to the 14th' or 'the 1st to the end of the month'."""
    if anchor_day == 1:
        return "the 1st to the end of the month"
    return f"the {ordinal(anchor_day)} to the {ordinal(anchor_day - 1)}"
