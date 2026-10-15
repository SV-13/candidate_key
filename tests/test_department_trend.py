import os

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

import app.main as main


@pytest.fixture
def trend_api(monkeypatch):
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

    database_name = "attendance_department_trend_test"
    mongo.drop_database(database_name)
    db = mongo[database_name]

    db.employees.insert_many([
        {
            "emp_code": "EMP4001",
            "name": "Trend One",
            "email": "emp4001@example.invalid",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-06",
        },
        {
            "emp_code": "EMP4002",
            "name": "Trend Two",
            "email": "emp4002@example.invalid",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-08",
        },
        {
            "emp_code": "EMP4003",
            "name": "Other Department",
            "email": "emp4003@example.invalid",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    ])

    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP4001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 5,
            "overtime_minutes": 0,
            "work_hours": 8.0,
            "half_day": False,
        },
        {
            "emp_code": "EMP4001",
            "date": "2026-07-07",
            "status": "WFH",
            "late_minutes": 0,
            "overtime_minutes": 0,
            "work_hours": 3.5,
            "half_day": True,
        },
        {
            "emp_code": "EMP4001",
            "date": "2026-07-08",
            "status": "ABSENT",
            "late_minutes": 0,
            "overtime_minutes": 0,
            "work_hours": None,
            "half_day": False,
        },
        {
            "emp_code": "EMP4002",
            "date": "2026-07-08",
            "status": "PRESENT",
            "late_minutes": 10,
            "overtime_minutes": 0,
            "work_hours": 8.0,
            "half_day": False,
        },
        {
            "emp_code": "EMP4001",
            "date": "2026-07-11",
            "status": "ON_DUTY",
            "late_minutes": 3,
            "overtime_minutes": 0,
            "work_hours": 4.0,
            "half_day": True,
        },
    ])

    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "indexes_ready", False)

    try:
        with TestClient(main.app) as api:
            yield api, db
    finally:
        mongo.drop_database(database_name)
        mongo.close()


def test_daily_series_gap_filling_headcount_rates_and_moving_average(trend_api):
    api, _ = trend_api

    response = api.get(
        "/analytics/departments/Engineering/trend"
        "?from=2026-07-06&to=2026-07-12"
    )

    assert response.status_code == 200
    result = response.json()
    assert result["department"] == "Engineering"

    rows = result["items"]
    assert len(rows) == 7
    assert [row["date"] for row in rows] == [
        "2026-07-06",
        "2026-07-07",
        "2026-07-08",
        "2026-07-09",
        "2026-07-10",
        "2026-07-11",
        "2026-07-12",
    ]

    # A second employee joins on July 8 and therefore counts from that day.
    assert [row["headcount"] for row in rows] == [1, 1, 2, 2, 2, 2, 2]
    assert [row["present_count"] for row in rows] == [
        1.0, 0.5, 1.0, 0.0, 0.0, 0.5, 0.0
    ]
    assert [row["late_count"] for row in rows] == [1, 0, 1, 0, 0, 1, 0]

    # Weekday rates include zero-record days; weekends have null rates.
    assert [row["attendance_rate"] for row in rows] == [
        1.0, 0.5, 0.5, 0.0, 0.0, None, None
    ]
    assert [row["moving_avg_7d"] for row in rows] == [
        1.0, 0.75, 0.6667, 0.5, 0.4, 0.4, 0.4
    ]
    assert [row["is_working_day"] for row in rows] == [
        True, True, True, True, True, False, False
    ]


def test_no_logs_still_returns_every_day(trend_api):
    api, _ = trend_api

    response = api.get(
        "/analytics/departments/Engineering/trend"
        "?from=2026-07-20&to=2026-07-22"
    )

    assert response.status_code == 200
    rows = response.json()["items"]
    assert len(rows) == 3
    assert [row["present_count"] for row in rows] == [0.0, 0.0, 0.0]
    assert [row["attendance_rate"] for row in rows] == [0.0, 0.0, 0.0]
    assert [row["moving_avg_7d"] for row in rows] == [0.0, 0.0, 0.0]


def test_unknown_department_returns_404(trend_api):
    api, _ = trend_api

    response = api.get(
        "/analytics/departments/Unknown/trend"
        "?from=2026-07-06&to=2026-07-12"
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-07-12", "2026-07-06"),  # reversed range
        ("2026-01-01", "2026-04-03"),  # 93 inclusive days
        ("not-a-date", "2026-07-12"),  # invalid ISO date
    ],
)
def test_invalid_ranges_return_422(trend_api, start, end):
    api, _ = trend_api

    response = api.get(
        "/analytics/departments/Engineering/trend"
        f"?from={start}&to={end}"
    )

    assert response.status_code == 422


def test_ninety_two_day_range_is_accepted(trend_api):
    api, _ = trend_api

    response = api.get(
        "/analytics/departments/Engineering/trend"
        "?from=2026-01-01&to=2026-04-02"
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 92
