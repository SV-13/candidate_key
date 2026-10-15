import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

import app.main as main


@pytest.fixture
def monthly_api(monkeypatch):
    mongo = MongoClient(
        os.getenv("MONGO_URI", "mongodb://localhost:27017"),
        tz_aware=True,
        serverSelectionTimeoutMS=1500,
    )

    try:
        mongo.admin.command("ping")
    except PyMongoError as exc:
        mongo.close()
        pytest.skip(f"Real MongoDB is unavailable: {exc}")

    database_name = "attendance_employee_monthly_test"
    mongo.drop_database(database_name)
    test_db = mongo[database_name]

    monkeypatch.setattr(main, "db", test_db)
    monkeypatch.setattr(main, "indexes_ready", False)

    try:
        with TestClient(main.app) as api:
            yield api, test_db
    finally:
        mongo.drop_database(database_name)
        mongo.close()


def add_employee(db, code, joined_on):
    db.employees.insert_one({
        "emp_code": code,
        "name": f"Test {code}",
        "email": f"{code.lower()}@example.invalid",
        "department": "Test",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": joined_on,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    })


def add_attendance(db, code, day, status, **fields):
    record = {
        "emp_code": code,
        "date": day,
        "status": status,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "work_hours": None,
    }
    record.update(fields)
    db.attendance_logs.insert_one(record)


def test_monthly_summary_aggregates_real_mongo_records(monthly_api):
    api, db = monthly_api
    add_employee(db, "EMP1001", "2026-07-13")

    add_attendance(
        db, "EMP1001", "2026-07-13", "PRESENT",
        late_minutes=35, overtime_minutes=40, work_hours=9.08,
    )
    add_attendance(
        db, "EMP1001", "2026-07-14", "WFH",
        half_day=True, work_hours=3.5,
    )
    add_attendance(db, "EMP1001", "2026-07-15", "LEAVE")
    # Weekend records do not add present_days, but still affect late/overtime totals.
    add_attendance(
        db, "EMP1001", "2026-07-18", "ON_DUTY",
        late_minutes=5, overtime_minutes=15, work_hours=8.0,
    )

    response = api.get(
        "/analytics/employees/EMP1001/monthly?month=2026-07"
    )

    assert response.status_code == 200
    assert response.json() == {
        "emp_code": "EMP1001",
        "month": "2026-07",
        "working_days": 15,
        "present_days": 1.5,
        "leave_days": 1,
        "late_count": 2,
        "total_late_minutes": 40,
        "total_overtime_minutes": 55,
        "attendance_pct": 10.0,
    }


def test_monthly_percentage_uses_four_decimal_places(monthly_api):
    api, db = monthly_api
    add_employee(db, "EMP1002", "2026-01-05")
    add_attendance(db, "EMP1002", "2026-07-06", "PRESENT")

    response = api.get(
        "/analytics/employees/EMP1002/monthly?month=2026-07"
    )

    assert response.status_code == 200
    assert response.json()["working_days"] == 23
    assert response.json()["present_days"] == 1.0
    assert response.json()["attendance_pct"] == 4.3478


def test_known_employee_with_no_month_records(monthly_api):
    api, db = monthly_api
    add_employee(db, "EMP1003", "2026-01-05")

    response = api.get(
        "/analytics/employees/EMP1003/monthly?month=2026-08"
    )

    assert response.status_code == 200
    assert response.json() == {
        "emp_code": "EMP1003",
        "month": "2026-08",
        "working_days": 21,
        "present_days": 0.0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
        "attendance_pct": 0.0,
    }


def test_month_before_joining_has_no_working_days(monthly_api):
    api, db = monthly_api
    add_employee(db, "EMP1004", "2026-08-03")

    response = api.get(
        "/analytics/employees/EMP1004/monthly?month=2026-07"
    )

    assert response.status_code == 200
    assert response.json()["working_days"] == 0
    assert response.json()["present_days"] == 0.0
    assert response.json()["attendance_pct"] is None


def test_unknown_employee_returns_404(monthly_api):
    api, _ = monthly_api

    response = api.get(
        "/analytics/employees/EMP999999/monthly?month=2026-07"
    )

    assert response.status_code == 404


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "not-a-month"])
def test_invalid_month_returns_422(monthly_api, month):
    api, _ = monthly_api

    response = api.get(
        f"/analytics/employees/EMP1001/monthly?month={month}"
    )

    assert response.status_code == 422
