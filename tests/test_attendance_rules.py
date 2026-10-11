from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.main import (
    get_shift_bounds,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def ist_to_utc(
    day: int,
    hour: int,
    minute: int,
    second: int = 0,
    microsecond: int = 0,
    month: int = 7,
    year: int = 2026,
) -> datetime:
    """Create an IST datetime and convert the same instant to UTC."""
    local = datetime(
        year, month, day, hour, minute, second, microsecond, tzinfo=IST
    )
    return local.astimezone(UTC)


def test_overnight_shift_bounds_are_aware_utc():
    start, end = get_shift_bounds(date(2026, 7, 6), "22:00", "06:00")

    assert start == datetime(2026, 7, 6, 16, 30, tzinfo=UTC)
    assert end == datetime(2026, 7, 7, 0, 30, tzinfo=UTC)
    assert start.utcoffset() == timedelta(0)
    assert end.utcoffset() == timedelta(0)


def test_late_grace_boundary_truncates_to_whole_seconds():
    start, _ = get_shift_bounds(date(2026, 7, 6), "09:30", "18:30")

    at_ten_minutes = ist_to_utc(6, 9, 40, 0, 900_000)
    one_second_after = ist_to_utc(6, 9, 40, 1, 999_000)

    assert compute_late_minutes(at_ten_minutes, start) == 0
    assert compute_late_minutes(one_second_after, start) == 10


def test_overtime_threshold_for_day_shift():
    _, end = get_shift_bounds(date(2026, 7, 6), "09:30", "18:30")

    before_threshold = ist_to_utc(6, 18, 59, 59)
    at_threshold = ist_to_utc(6, 19, 0, 0)

    assert compute_overtime(before_threshold, end) == 0
    assert compute_overtime(at_threshold, end) == 30


def test_overtime_threshold_for_overnight_shift():
    _, end = get_shift_bounds(date(2026, 7, 6), "22:00", "06:00")

    before_threshold = ist_to_utc(7, 6, 29, 59)
    at_threshold = ist_to_utc(7, 6, 30, 0)

    assert compute_overtime(before_threshold, end) == 0
    assert compute_overtime(at_threshold, end) == 30


def test_work_hours_rounding_controls_half_day():
    punch_in = ist_to_utc(6, 10, 0, 0)
    first_punch_out = ist_to_utc(6, 14, 29, 41)
    second_punch_out = ist_to_utc(6, 14, 29, 42)

    first_hours = compute_work_hours(punch_in, first_punch_out)
    second_hours = compute_work_hours(punch_in, second_punch_out)

    assert first_hours == Decimal("4.49")
    assert compute_half_day(first_hours) is True

    assert second_hours == Decimal("4.50")
    assert compute_half_day(second_hours) is False

