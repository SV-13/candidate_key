from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import ASCENDING, DESCENDING
from pymongo import ReturnDocument

import app.main as main


UTC = timezone.utc


class FakeCollection:
    def __init__(self):
        self.documents = []

    def create_index(self, *args, **kwargs):
        return kwargs.get("name", "test-index")

    @staticmethod
    def matches(document, query):
        for field, expected in query.items():
            actual = document.get(field)

            if isinstance(expected, dict):
                for operator, value in expected.items():
                    # MongoDB comparison operators do not treat a null
                    # punch_in as a datetime.
                    if not isinstance(actual, datetime):
                        return False

                    if operator == "$lte" and not actual <= value:
                        return False
                    if operator == "$lt" and not actual < value:
                        return False
                    if operator == "$gte" and not actual >= value:
                        return False
                    if operator == "$gt" and not actual > value:
                        return False
            elif actual != expected:
                return False

        return True

    def find_one(self, query, sort=None):
        matches = [
            document
            for document in self.documents
            if self.matches(document, query)
        ]

        if sort:
            field, direction = sort[0]
            matches.sort(
                key=lambda document: document.get(field)
                or datetime.min.replace(tzinfo=UTC),
                reverse=(direction == DESCENDING),
            )

        return deepcopy(matches[0]) if matches else None

    def find_one_and_update(
        self, query, update, return_document=ReturnDocument.BEFORE
    ):
        for document in self.documents:
            if not self.matches(document, query):
                continue

            before = deepcopy(document)

            for field, value in update.get("$set", {}).items():
                document[field] = value

            # Support $push so a regression that writes history is caught
            # by the history assertion in the tests.
            for field, value in update.get("$push", {}).items():
                document.setdefault(field, []).append(value)

            if return_document == ReturnDocument.AFTER:
                return deepcopy(document)

            return before

        return None


class FakeDatabase:
    def __init__(self):
        self.employees = FakeCollection()
        self.attendance_logs = FakeCollection()


@pytest.fixture
def api(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(main, "db", fake_db)

    with TestClient(main.app) as client:
        yield client, fake_db


def add_employee(
    db,
    emp_code="EMP0001",
    shift_start="09:30",
    shift_end="18:30",
):
    db.employees.documents.append({
        "emp_code": emp_code,
        "name": "Test Employee",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": "2026-01-01",
    })


def add_attendance(
    db,
    punch_in,
    *,
    emp_code="EMP0001",
    attendance_date="2026-10-12",
    punch_out=None,
    history=None,
):
    record = {
        "_id": f"record-{len(db.attendance_logs.documents) + 1}",
        "emp_code": emp_code,
        "date": attendance_date,
        "status": "PRESENT",
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [] if history is None else history,
    }
    db.attendance_logs.documents.append(record)
    return record


def punch_out(client, timestamp, emp_code="EMP0001"):
    return client.post(
        "/attendance/punch-out",
        json={
            "emp_code": emp_code,
            "punched_at": main.to_epoch_ms(timestamp),
        },
    )


def test_punch_out_unknown_employee_returns_404(api):
    client, _ = api

    response = punch_out(
        client,
        datetime(2026, 10, 12, 10, 0, tzinfo=UTC),
        emp_code="EMP9999",
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "employee not found"


def test_punch_out_without_attendance_record_returns_404(api):
    client, db = api
    add_employee(db)

    response = punch_out(
        client, datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "attendance record not found"


def test_punch_out_already_closed_record_returns_409(api):
    client, db = api
    add_employee(db)

    start = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    add_attendance(
        db,
        start,
        punch_out=start + timedelta(minutes=30),
    )

    response = punch_out(client, start + timedelta(hours=1))

    assert response.status_code == 409


@pytest.mark.parametrize("seconds_before", [0, 1])
def test_punch_out_at_or_before_punch_in_returns_422(api, seconds_before):
    client, db = api
    add_employee(db)

    start = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    add_attendance(db, start)

    response = punch_out(
        client, start - timedelta(seconds=seconds_before)
    )

    assert response.status_code == 422


def test_punch_out_more_than_24_hours_after_returns_422(api):
    client, db = api
    add_employee(db)

    start = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    add_attendance(db, start)

    response = punch_out(
        client, start + timedelta(hours=24, seconds=1)
    )

    assert response.status_code == 422


def test_punch_out_exactly_24_hours_after_is_allowed(api):
    client, db = api
    add_employee(db)

    start = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    add_attendance(db, start)

    response = punch_out(client, start + timedelta(hours=24))

    assert response.status_code == 200


def test_invalid_time_takes_precedence_over_closed_conflict(api):
    client, db = api
    add_employee(db)

    start = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    add_attendance(
        db,
        start,
        punch_out=start + timedelta(hours=1),
    )

    response = punch_out(
        client, start + timedelta(hours=24, seconds=1)
    )

    assert response.status_code == 422


def test_overnight_punch_out_uses_stored_attendance_date(api):
    client, db = api
    add_employee(db, shift_start="22:00", shift_end="06:00")

    # 22:00 IST on 11 October.
    start = datetime(2026, 10, 11, 16, 30, tzinfo=UTC)
    record = add_attendance(
        db,
        start,
        attendance_date="2026-10-11",
    )

    # 06:30 IST on 12 October: 8.5 hours worked, 30 minutes overtime.
    finish = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
    response = punch_out(client, finish)

    assert response.status_code == 200
    body = response.json()
    assert body["date"] == "2026-10-11"
    assert body["punch_out"] == main.to_epoch_ms(finish)
    assert body["work_hours"] == 8.5
    assert body["overtime_minutes"] == 30
    assert body["half_day"] is False
    assert record["punch_out"] == finish


def test_punch_out_does_not_modify_history(api):
    client, db = api
    add_employee(db)

    start = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    record = add_attendance(db, start, history=[])

    response = punch_out(client, start + timedelta(hours=1))

    assert response.status_code == 200
    assert response.json()["history"] == []
    assert record["history"] == []
