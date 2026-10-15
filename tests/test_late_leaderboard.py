import os

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

import app.main as main


@pytest.fixture
def leaderboard_api(monkeypatch):
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

    database_name = "attendance_leaderboard_test"
    mongo.drop_database(database_name)
    db = mongo[database_name]

    employees = [
        ("EMP3001", "Engineering", "Leaderboard A"),
        ("EMP3002", "Sales", "Leaderboard B"),
        ("EMP3003", "Engineering", "Leaderboard C"),
        ("EMP3004", "Sales", "Leaderboard D"),
        ("EMP3005", "Engineering", "Leaderboard E"),
    ]

    db.employees.insert_many([
        {
            "emp_code": code,
            "name": name,
            "email": f"{code.lower()}@example.invalid",
            "department": department,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        for code, department, name in employees
    ])

    logs = [
        ("EMP3001", "2026-07-06", 60),
        ("EMP3001", "2026-07-07", 40),
        ("EMP3002", "2026-07-06", 80),
        ("EMP3003", "2026-07-06", 50),
        ("EMP3003", "2026-07-07", 30),
        ("EMP3004", "2026-07-06", 60),
        ("EMP3005", "2026-07-06", 20),
        ("EMP3005", "2026-07-07", 0),
        # This employee does not exist and must not appear in the leaderboard.
        ("EMP3999", "2026-07-06", 200),
        # Outside the requested month; must not affect the ranking.
        ("EMP3004", "2026-08-01", 500),
    ]

    db.attendance_logs.insert_many([
        {
            "emp_code": code,
            "date": day,
            "status": "PRESENT",
            "late_minutes": late,
            "overtime_minutes": 0,
            "work_hours": 8.0,
            "half_day": False,
        }
        for code, day, late in logs
    ])

    # Test a legacy record with no late_minutes field.
    db.attendance_logs.insert_one({
        "emp_code": "EMP3005",
        "date": "2026-07-08",
        "status": "PRESENT",
        "overtime_minutes": 0,
        "work_hours": 8.0,
        "half_day": False,
    })

    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "indexes_ready", False)

    try:
        with TestClient(main.app) as api:
            yield api, db
    finally:
        mongo.drop_database(database_name)
        mongo.close()


def test_competition_rank_and_limit_keep_all_ties(leaderboard_api):
    api, _ = leaderboard_api

    response = api.get(
        "/analytics/leaderboard/late?month=2026-07&limit=2"
    )

    assert response.status_code == 200
    assert response.json() == {
        "month": "2026-07",
        "items": [
            {
                "rank": 1,
                "emp_code": "EMP3001",
                "name": "Leaderboard A",
                "department": "Engineering",
                "total_late_minutes": 100,
                "late_count": 2,
            },
            {
                "rank": 2,
                "emp_code": "EMP3002",
                "name": "Leaderboard B",
                "department": "Sales",
                "total_late_minutes": 80,
                "late_count": 1,
            },
            {
                "rank": 2,
                "emp_code": "EMP3003",
                "name": "Leaderboard C",
                "department": "Engineering",
                "total_late_minutes": 80,
                "late_count": 2,
            },
        ],
    }


def test_department_filter_recalculates_ranks(leaderboard_api):
    api, _ = leaderboard_api

    response = api.get(
        "/analytics/leaderboard/late"
        "?month=2026-07&limit=10&department=Engineering"
    )

    assert response.status_code == 200
    rows = response.json()["items"]
    assert [
        (row["emp_code"], row["rank"], row["total_late_minutes"])
        for row in rows
    ] == [
        ("EMP3001", 1, 100),
        ("EMP3003", 2, 80),
        ("EMP3005", 3, 20),
    ]
    assert all(row["department"] == "Engineering" for row in rows)


def test_only_positive_totals_are_ranked_and_orphans_are_ignored(leaderboard_api):
    api, _ = leaderboard_api

    response = api.get(
        "/analytics/leaderboard/late?month=2026-07&limit=50"
    )

    assert response.status_code == 200
    rows = response.json()["items"]
    codes = [row["emp_code"] for row in rows]

    assert "EMP3999" not in codes
    assert all(row["total_late_minutes"] > 0 for row in rows)
    assert "EMP3005" in codes
    emp3005 = next(row for row in rows if row["emp_code"] == "EMP3005")
    assert emp3005["late_count"] == 1
    assert emp3005["total_late_minutes"] == 20


def test_month_with_no_records_returns_empty_leaderboard(leaderboard_api):
    api, _ = leaderboard_api

    response = api.get(
        "/analytics/leaderboard/late?month=2026-09"
    )

    assert response.status_code == 200
    assert response.json() == {"month": "2026-09", "items": []}


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "not-a-month"])
def test_invalid_month_returns_422(leaderboard_api, month):
    api, _ = leaderboard_api

    response = api.get(
        f"/analytics/leaderboard/late?month={month}"
    )

    assert response.status_code == 422


@pytest.mark.parametrize("limit", [0, 51])
def test_invalid_limit_returns_422(leaderboard_api, limit):
    api, _ = leaderboard_api

    response = api.get(
        f"/analytics/leaderboard/late?month=2026-07&limit={limit}"
    )

    assert response.status_code == 422
