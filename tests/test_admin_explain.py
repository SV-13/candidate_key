import os

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

import app.main as main


@pytest.fixture
def explain_api(monkeypatch):
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

    database_name = "attendance_admin_explain_test"
    mongo.drop_database(database_name)
    db = mongo[database_name]

    db.employees.insert_many([
        {
            "emp_code": "EMP6001",
            "name": "Explain Engineering",
            "email": "emp6001@example.invalid",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
        {
            "emp_code": "EMP6002",
            "name": "Explain Sales",
            "email": "emp6002@example.invalid",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    ])

    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP6001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "work_hours": 8.0,
            "late_minutes": 20,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        {
            "emp_code": "EMP6001",
            "date": "2026-07-07",
            "status": "WFH",
            "work_hours": 3.5,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
            "history": [],
        },
        {
            "emp_code": "EMP6002",
            "date": "2026-07-06",
            "status": "PRESENT",
            "work_hours": 8.5,
            "late_minutes": 35,
            "overtime_minutes": 30,
            "half_day": False,
            "history": [],
        },
    ])

    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "indexes_ready", False)

    try:
        # Application startup builds the indexes on this isolated DB.
        with TestClient(main.app) as api:
            yield api, db
    finally:
        mongo.drop_database(database_name)
        mongo.close()


@pytest.mark.parametrize(
    ("endpoint", "query", "expected_collection"),
    [
        (
            "attendance_list",
            "?date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20",
            "attendance_logs",
        ),
        (
            "employee_monthly",
            "?emp_code=EMP6001&month=2026-07",
            "attendance_logs",
        ),
        ("department_summary", "?month=2026-07", "employees"),
        (
            "late_leaderboard",
            "?month=2026-07&limit=10",
            "attendance_logs",
        ),
        (
            "department_trend",
            "?department=Engineering&from=2026-07-06&to=2026-07-12",
            "database",
        ),
    ],
)
def test_each_supported_endpoint_returns_real_explain(
    explain_api, endpoint, query, expected_collection
):
    api, _ = explain_api

    response = api.get(f"/admin/explain/{endpoint}{query}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == endpoint
    assert body["collection"] == expected_collection

    explain = body["explain"]
    assert isinstance(explain, dict)
    assert explain.get("ok") == 1
    assert (
        "queryPlanner" in explain
        or "executionStats" in explain
        or "stages" in explain
    )


@pytest.mark.parametrize(
    ("endpoint", "query"),
    [
        ("employee_monthly", ""),
        ("department_summary", ""),
        ("late_leaderboard", ""),
        ("department_trend", ""),
        ("department_trend", "?department=Engineering"),
        ("department_trend", "?department=Engineering&from=2026-07-01"),
    ],
)
def test_missing_required_parameters_return_422(explain_api, endpoint, query):
    api, _ = explain_api

    response = api.get(f"/admin/explain/{endpoint}{query}")

    assert response.status_code == 422


def test_unsupported_explain_target_returns_422(explain_api):
    api, _ = explain_api

    response = api.get("/admin/explain/not_supported")

    assert response.status_code == 422


def test_invalid_month_returns_422(explain_api):
    api, _ = explain_api

    response = api.get(
        "/admin/explain/employee_monthly"
        "?emp_code=EMP6001&month=2026-13"
    )

    assert response.status_code == 422
