import pytest
from pydantic import ValidationError

from app.main import EmployeeIn, PunchInIn


VALID_EMPLOYEE = {
    "emp_code": "EMP900001",
    "name": "Review Test",
    "email": "review.test@example.com",
    "department": "QA",
    "joined_on": "2026-10-12",
}


def test_employee_accepts_valid_input_and_defaults():
    employee = EmployeeIn(**VALID_EMPLOYEE)

    assert employee.emp_code == "EMP900001"
    assert employee.shift_start == "09:30"
    assert employee.shift_end == "18:30"


@pytest.mark.parametrize(
    "changes",
    [
        {"emp_code": "BAD001"},
        {"emp_code": "EMP123"},
        {"name": ""},
        {"name": "N" * 101},
        {"email": "not-an-email"},
        {"department": ""},
        {"department": "D" * 51},
        {"shift_start": "25:00"},
        {"shift_end": "18:60"},
        {"joined_on": "2026-02-30"},
        {"joined_on": "12-10-2026"},
        {"shift_start": "09:30", "shift_end": "09:30"},
    ],
)
def test_employee_rejects_invalid_fields(changes):
    payload = {**VALID_EMPLOYEE, **changes}

    with pytest.raises(ValidationError):
        EmployeeIn(**payload)


@pytest.mark.parametrize(
    "status",
    ["PRESENT", "WFH", "ON_DUTY"],
)
def test_punch_in_accepts_allowed_statuses(status):
    request = PunchInIn(emp_code="EMP0001", status=status)

    assert request.status == status


@pytest.mark.parametrize(
    "status",
    ["ABSENT", "LEAVE", "INVALID", ""],
)
def test_punch_in_rejects_disallowed_statuses(status):
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", status=status)


def test_punch_in_allows_omitted_timestamp():
    request = PunchInIn(emp_code="EMP0001")

    assert request.punched_at is None
    assert request.status == "PRESENT"


def test_punch_in_accepts_valid_epoch_milliseconds():
    request = PunchInIn(
        emp_code="EMP0001",
        punched_at=1783312815999,
    )

    assert request.punched_at == 1783312815999


@pytest.mark.parametrize(
    "timestamp",
    [
        1783312815,              # Seconds, not milliseconds
        "1783312815999",         # Numeric string
        1783312815999.0,         # Float
        True,                    # Boolean is not an acceptable timestamp
        False,
        99_999_999_999,          # Below minimum
        4_102_444_800_001,       # Above maximum
    ],
)
def test_punch_in_rejects_invalid_timestamps(timestamp):
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", punched_at=timestamp)
