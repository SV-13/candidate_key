
from datetime import datetime, timedelta, timezone
from app.main import from_epoch_ms, to_epoch_ms

import pytest

from app.main import from_epoch_ms

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)



def test_to_epoch_ms_round_trips():
    original = 1783312815000  # Whole-second epoch milliseconds

    dt = from_epoch_ms(original)

    assert to_epoch_ms(dt) == original


def test_to_epoch_ms_rejects_naive_datetime():
    naive = datetime(2026, 7, 6, 4, 40, 15)

    with pytest.raises(ValueError):
        to_epoch_ms(naive)


def test_to_epoch_ms_normalizes_microseconds():
    dt = datetime(
        2026, 7, 6, 4, 40, 15, 999000, tzinfo=UTC
    )

    result = to_epoch_ms(dt)
    expected = to_epoch_ms(
        datetime(2026, 7, 6, 4, 40, 15, tzinfo=UTC)
    )

    assert result == expected


def test_from_epoch_ms_truncates_milliseconds():
    result = from_epoch_ms(1783312815999)

    expected = EPOCH + timedelta(seconds=1783312815)

    assert result == expected
    assert result.tzinfo == UTC
    assert result.utcoffset() == timedelta(0)
    assert result.microsecond == 0


def test_from_epoch_ms_rejects_seconds_looking_value():
    with pytest.raises(ValueError):
        from_epoch_ms(1783312815)


@pytest.mark.parametrize(
    "value",
    [
        "1783312815999",
        1783312815999.0,
        True,
        False,
    ],
)
def test_from_epoch_ms_rejects_non_integer_inputs(value):
    with pytest.raises(ValueError):
        from_epoch_ms(value)


@pytest.mark.parametrize(
    "value",
    [
        99_999_999_999,
        4_102_444_800_001,
    ],
)
def test_from_epoch_ms_rejects_out_of_range_values(value):
    with pytest.raises(ValueError):
        from_epoch_ms(value)


def test_from_epoch_ms_accepts_range_boundaries():
    minimum = from_epoch_ms(100_000_000_000)
    maximum = from_epoch_ms(4_102_444_800_000)

    assert minimum.tzinfo == UTC
    assert maximum.tzinfo == UTC
    assert minimum.microsecond == 0
    assert maximum.microsecond == 0