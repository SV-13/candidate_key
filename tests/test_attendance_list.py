from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main


UTC = timezone.utc


class FakeCursor:
    """Small Mongo-style cursor that records database operations."""

    def __init__(self, collection, query):
        self.collection = collection
        self.documents = [
            deepcopy(doc)
            for doc in collection.documents
            if collection.matches(doc, query)
        ]

    def sort(self, fields):
        self.collection.sort_calls.append(list(fields))

        # Stable sorts from the least significant key to the first key.
        for field, direction in reversed(fields):
            self.documents.sort(
                key=lambda doc: doc.get(field),
                reverse=(direction == -1),
            )
        return self

    def skip(self, amount):
        self.collection.skip_calls.append(amount)
        self.documents = self.documents[amount:]
        return self

    def limit(self, amount):
        self.collection.limit_calls.append(amount)
        self.documents = self.documents[:amount]
        return self

    def __iter__(self):
        return iter(deepcopy(self.documents))


class FakeCollection:
    def __init__(self):
        self.documents = []
        self.find_calls = []
        self.count_calls = []
        self.sort_calls = []
        self.skip_calls = []
        self.limit_calls = []

    def create_index(self, *args, **kwargs):
        return kwargs.get("name", "test-index")

    @staticmethod
    def matches(document, query):
        for field, expected in query.items():
            actual = document.get(field)

            if isinstance(expected, dict):
                if actual is None:
                    return False

                for operator, value in expected.items():
                    if operator == "$gte" and actual < value:
                        return False
                    if operator == "$lte" and actual > value:
                        return False
            elif actual != expected:
                return False

        return True

    def find(self, query):
        self.find_calls.append(deepcopy(query))
        return FakeCursor(self, query)

    def count_documents(self, query):
        self.count_calls.append(deepcopy(query))
        return sum(
            self.matches(doc, query)
            for doc in self.documents
        )


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


def add_record(
    db,
    *,
    emp_code="EMP0001",
    attendance_date="2026-10-12",
    status="PRESENT",
    punch_in=None,
    punch_out=None,
):
    record = {
        "_id": f"record-{len(db.attendance_logs.documents) + 1}",
        "emp_code": emp_code,
        "date": attendance_date,
        "status": status,
        "punch_in": punch_in or datetime(
            2026, 10, 12, 4, 30, tzinfo=UTC
        ),
        "punch_out": punch_out,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    db.attendance_logs.documents.append(record)
    return record


def test_list_sorts_in_mongo_and_normalizes_responses(api):
    client, db = api

    # Same date: emp_code must break the tie in ascending order.
    add_record(db, emp_code="EMP0002")
    add_record(db, emp_code="EMP0001")
    add_record(
        db,
        emp_code="EMP0003",
        attendance_date="2026-10-11",
    )

    response = client.get("/attendance?page=1&page_size=2")

    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert [item["emp_code"] for item in body["items"]] == [
        "EMP0001",
        "EMP0002",
    ]

    first = body["items"][0]
    assert "id" not in first
    assert "_id" not in first
    assert first["punch_in"] == main.to_epoch_ms(
        datetime(2026, 10, 12, 4, 30, tzinfo=UTC)
    )

    # Assert that the endpoint delegates the work to Mongo-style operations.
    collection = db.attendance_logs
    assert collection.find_calls == [{}]
    assert collection.count_calls == [{}]
    assert collection.sort_calls == [
        [("date", -1), ("emp_code", 1)]
    ]
    assert collection.skip_calls == [0]
    assert collection.limit_calls == [2]


def test_page_two_uses_database_skip(api):
    client, db = api
    add_record(db, emp_code="EMP0003")
    add_record(db, emp_code="EMP0001")
    add_record(db, emp_code="EMP0002")

    response = client.get("/attendance?page=2&page_size=1")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert [item["emp_code"] for item in body["items"]] == ["EMP0002"]
    assert db.attendance_logs.skip_calls == [1]
    assert db.attendance_logs.limit_calls == [1]


def test_filters_are_applied_before_count_and_pagination(api):
    client, db = api

    add_record(
        db,
        emp_code="EMP0002",
        attendance_date="2026-10-12",
        status="WFH",
    )
    add_record(
        db,
        emp_code="EMP0002",
        attendance_date="2026-10-13",
        status="WFH",
    )
    add_record(
        db,
        emp_code="EMP0002",
        attendance_date="2026-10-13",
        status="PRESENT",
    )
    add_record(
        db,
        emp_code="EMP0001",
        attendance_date="2026-10-13",
        status="WFH",
    )

    response = client.get(
        "/attendance"
        "?emp_code=EMP0002"
        "&date_from=2026-10-12"
        "&date_to=2026-10-13"
        "&status=WFH"
        "&page_size=1"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["items"][0]["date"] == "2026-10-13"

    expected_filter = {
        "emp_code": "EMP0002",
        "date": {
            "$gte": "2026-10-12",
            "$lte": "2026-10-13",
        },
        "status": "WFH",
    }
    assert db.attendance_logs.find_calls == [expected_filter]
    assert db.attendance_logs.count_calls == [expected_filter]


@pytest.mark.parametrize(
    "query",
    [
        "status=INVALID",
        "date_from=2026-02-30",
        "date_to=not-a-date",
        "date_from=2026-10-13&date_to=2026-10-12",
        "page=0",
        "page_size=0",
        "page_size=101",
    ],
)
def test_invalid_query_parameters_return_422(api, query):
    client, _ = api

    response = client.get(f"/attendance?{query}")

    assert response.status_code == 422


def test_valid_status_values_are_accepted(api):
    client, db = api

    for status in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        add_record(
            db,
            emp_code=f"EMP{len(db.attendance_logs.documents) + 1:04d}",
            status=status,
        )

    for status in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        response = client.get(f"/attendance?status={status}")
        assert response.status_code == 200
        assert all(
            item["status"] == status
            for item in response.json()["items"]
        )


def test_legacy_record_gets_response_defaults(api):
    client, db = api
    record = add_record(db)
    record.pop("half_day")
    record.pop("history")
    record.pop("late_minutes")
    record.pop("overtime_minutes")

    response = client.get("/attendance")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["half_day"] is False
    assert item["history"] == []
    assert item["late_minutes"] == 0
    assert item["overtime_minutes"] == 0
    assert "id" not in item
    assert "_id" not in item
