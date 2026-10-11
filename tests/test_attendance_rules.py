
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.main import (
    truncate_to_seconds,
    compute_attendance_date,
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


# ---------------------------------------------------------
# R1: Timestamp truncation and attendance dates
# ---------------------------------------------------------

def test_truncation_happens_before_calculation():
    raw = ist_to_utc(6, 9, 40, 0, 900_000)
    normalized = truncate_to_seconds(raw)

    expected = ist_to_utc(6, 9, 40, 0)

    assert normalized == expected
    assert normalized.microsecond == 0
    assert normalized.utcoffset() == timedelta(0)


def test_overnight_attendance_dates():
    # A: 21:55 IST on 6 July belongs to 6 July.
    punch_a = ist_to_utc(6, 21, 55)

    assert compute_attendance_date(
        punch_a, "22:00", "06:00"
    ) == date(2026, 7, 6)

    # B: 00:30 IST on 7 July belongs to the shift
    # that started on 6 July.
    punch_b = ist_to_utc(7, 0, 30)

    assert compute_attendance_date(
        punch_b, "22:00", "06:00"
    ) == date(2026, 7, 6)

    # C: Exactly 06:00 is not earlier than shift_end.
    punch_c = ist_to_utc(7, 6, 0)

    assert compute_attendance_date(
        punch_c, "22:00", "06:00"
    ) == date(2026, 7, 7)


def test_attendance_date_uses_ist_not_utc_calendar_date():
    # 00:20 IST on 6 July is 18:50 UTC on 5 July.
    punch_in = datetime(2026, 7, 5, 18, 50, tzinfo=UTC)

    assert compute_attendance_date(
        punch_in, "09:30", "18:30"
    ) == date(2026, 7, 6)


# ---------------------------------------------------------
# Shift bounds
# ---------------------------------------------------------

def test_overnight_shift_bounds_are_aware_utc():
    start, end = get_shift_bounds(
        date(2026, 7, 6), "22:00", "06:00"
    )

    assert start == datetime(2026, 7, 6, 16, 30, tzinfo=UTC)
    assert end == datetime(2026, 7, 7, 0, 30, tzinfo=UTC)

    assert start.utcoffset() == timedelta(0)
    assert end.utcoffset() == timedelta(0)


def test_day_shift_bounds_are_aware_utc():
    start, end = get_shift_bounds(
        date(2026, 7, 6), "09:30", "18:30"
    )

    assert start == datetime(2026, 7, 6, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 6, 13, 0, tzinfo=UTC)

    assert start.utcoffset() == timedelta(0)
    assert end.utcoffset() == timedelta(0)


# ---------------------------------------------------------
# R2: Late minutes and the 10-minute grace period
# ---------------------------------------------------------

def test_late_grace_boundary_truncates_to_whole_seconds():
    start, _ = get_shift_bounds(
        date(2026, 7, 6), "09:30", "18:30"
    )

    at_ten_minutes = truncate_to_seconds(
    ist_to_utc(6, 9, 40, 0, 900_000)
    )   
    one_second_after = truncate_to_seconds(
    ist_to_utc(6, 9, 40, 1, 999_000)
    )

    assert compute_late_minutes(at_ten_minutes, start) == 0
    assert compute_late_minutes(one_second_after, start) == 10


def test_overnight_case_b_is_150_minutes_late():
    # EMP0005: shift 22:00-06:00, punch-in at 00:30 IST
    # on 7 July belongs to the shift that started on 6 July.
    punch_in = ist_to_utc(7, 0, 30)

    attendance_date = compute_attendance_date(
        punch_in, "22:00", "06:00"
    )

    shift_start, _ = get_shift_bounds(
        attendance_date, "22:00", "06:00"
    )

    assert attendance_date == date(2026, 7, 6)
    assert compute_late_minutes(punch_in, shift_start) == 150


def test_late_minutes_floor_fractional_elapsed_minute():
    shift_start, _ = get_shift_bounds(
        date(2026, 7, 6), "09:30", "18:30"
    )

    punch_in = ist_to_utc(6, 10, 15, 59)

    assert compute_late_minutes(punch_in, shift_start) == 45


# ---------------------------------------------------------
# R3: Overtime and the 30-minute minimum
# ---------------------------------------------------------

def test_overtime_threshold_for_day_shift():
    _, shift_end = get_shift_bounds(
        date(2026, 7, 6), "09:30", "18:30"
    )

    before_threshold = ist_to_utc(6, 18, 59, 59)
    at_threshold = ist_to_utc(6, 19, 0, 0)

    assert compute_overtime(before_threshold, shift_end) == 0
    assert compute_overtime(at_threshold, shift_end) == 30


def test_overtime_threshold_for_overnight_shift():
    _, shift_end = get_shift_bounds(
        date(2026, 7, 6), "22:00", "06:00"
    )

    before_threshold = ist_to_utc(7, 6, 29, 59)
    at_threshold = ist_to_utc(7, 6, 30, 0)

    assert compute_overtime(before_threshold, shift_end) == 0
    assert compute_overtime(at_threshold, shift_end) == 30


# ---------------------------------------------------------
# R4: Work hours and decimal half-up rounding
# ---------------------------------------------------------

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


def test_half_up_rounding_trap_cases():
    start = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)

    # 450 / 3600 = exactly 0.125 hours.
    assert compute_work_hours(
        start, start + timedelta(seconds=450)
    ) == Decimal("0.13")

    # 9630 / 3600 = exactly 2.675 hours.
    assert compute_work_hours(
        start, start + timedelta(seconds=9630)
    ) == Decimal("2.68")
