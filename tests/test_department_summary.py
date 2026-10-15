import os

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

import app.main as main


@pytest.fixture
def department_api(monkeypatch):
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

    database_name = "attendance_department_summary_test"
    mongo.drop_database(database_name)
    db = mongo[database_name]

    db.employees.insert_many([
        {
            "emp_code": "EMP2001", "name": "Engineering One",
            "email": "emp2001@example.invalid", "department": "Engineering",
            "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
        {
            "emp_code": "EMP2002", "name": "Engineering No Logs",
            "email": "emp2002@example.invalid", "department": "Engineering",
            "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
        {
            "emp_code": "EMP2003", "name": "Future Engineering",
            "email": "emp2003@example.invalid", "department": "Engineering",
            "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-08-01",
        },
        {
            "emp_code": "EMP2004", "name": "Sales One",
            "email": "emp2004@example.invalid", "department": "Sales",
            "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
        {
            "emp_code": "EMP2005", "name": "Sales Two",
            "email": "emp2005@example.invalid", "department": "Sales",
            "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
        {
            "emp_code": "EMP2006", "name": "No Logs",
            "email": "emp2006@example.invalid", "department": "NoLogs",
            "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
        {
            "emp_code": "EMP2007", "name": "Future Department",
            "email": "emp2007@example.invalid", "department": "FutureDept",
            "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-08-01",
        },
    ])

    def add_log(code, day, status, work_hours=None, late=0,
                overtime=0, half_day=False):
        db.attendance_logs.insert_one({
            "emp_code": code,
            "date": day,
            "status": status,
            "work_hours": work_hours,
            "late_minutes": late,
            "overtime_minutes": overtime,
            "half_day": half_day,
        })

    # Engineering: two weekdays plus a weekend record.
    add_log("EMP2001", "2026-07-06", "PRESENT", 8.5, late=20, overtime=30)
    add_log("EMP2001", "2026-07-07", "WFH", 3.5, half_day=True)
    add_log("EMP2001", "2026-07-11", "ON_DUTY", 7.0, late=5, overtime=15)

    # Sales: five work-hour values across two employees, plus an open record.
    add_log("EMP2004", "2026-07-06", "PRESENT", 4.0, late=12)
    add_log("EMP2004", "2026-07-07", "LEAVE")
    add_log("EMP2004", "2026-07-08", "ON_DUTY", 7.0)
    add_log("EMP2004", "2026-07-09", "PRESENT", None)
    add_log("EMP2005", "2026-07-06", "PRESENT", 8.0)
    add_log("EMP2005", "2026-07-07", "PRESENT", 8.0)
    add_log("EMP2005", "2026-07-08", "PRESENT", 8.0)

    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "indexes_ready", False)

    try:
        with TestClient(main.app) as api:
            yield api, db
    finally:
        mongo.drop_database(database_name)
        mongo.close()


def test_department_summary_counts_headcount_and_aggregates_records(department_api):
    api, _ = department_api

    response = api.get(
        "/analytics/departments/summary?month=2026-07"
    )

    assert response.status_code == 200
    assert response.json() == {
        "month": "2026-07",
        "items": [
            {
                "department": "Engineering",
                "headcount": 2,
                "present_days": 1.5,
                "avg_work_hours": 6.33,
                "late_count": 2,
                "total_late_minutes": 25,
                "leave_count": 0,
                "on_duty_count": 1,
            },
            {
                "department": "NoLogs",
                "headcount": 1,
                "present_days": 0.0,
                "avg_work_hours": None,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            },
            {
                "department": "Sales",
                "headcount": 2,
                "present_days": 6.0,
                "avg_work_hours": 7.0,
                "late_count": 1,
                "total_late_minutes": 12,
                "leave_count": 1,
                "on_duty_count": 1,
            },
        ],
    }


def test_department_filter_returns_only_exact_match(department_api):
    api, _ = department_api

    response = api.get(
        "/analytics/departments/summary"
        "?month=2026-07&department=Engineering"
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "Engineering"
    assert items[0]["headcount"] == 2


def test_future_joiners_and_zero_headcount_departments_are_excluded(department_api):
    api, _ = department_api

    response = api.get(
        "/analytics/departments/summary?month=2026-07"
    )

    assert response.status_code == 200
    names = [item["department"] for item in response.json()["items"]]
    assert "FutureDept" not in names


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "not-a-month"])
def test_invalid_month_returns_422(department_api, month):
    api, _ = department_api

    response = api.get(
        f"/analytics/departments/summary?month={month}"
    )

    assert response.status_code == 422
