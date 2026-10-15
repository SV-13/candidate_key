from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main


UTC = timezone.utc
PUNCH_IN = datetime(2026, 10, 12, 4, 35, tzinfo=UTC)   # 10:05 IST
PUNCH_OUT = datetime(2026, 10, 12, 13, 5, tzinfo=UTC)  # 18:35 IST
CORRECTED_IN = datetime(2026, 10, 12, 3, 58, tzinfo=UTC)  # 09:28 IST


class FakeCollection:
    def __init__(self):
        self.documents = []
        self.conflict_on_update = False
        self.update_filters = []
        self.updates = []

    def create_index(self, *args, **kwargs):
        return kwargs.get("name", "test-index")

    @staticmethod
    def matches(document, query):
        for field, expected in query.items():
            if field == "$expr":
                expression = expected["$eq"]
                left_expression, right_expression = expression

                # Evaluate the $ifNull used to normalize missing history.
                history_expr = left_expression["$ifNull"]
                field_reference, fallback = history_expr
                stored_value = document.get(field_reference.lstrip("$"))
                left = fallback if stored_value is None else stored_value

                # Evaluate the literal snapshot used by the compare-and-set.
                right = right_expression["$literal"]
                if left != right:
                    return False
                continue

            actual = document.get(field)

            if expected is None:
                # MongoDB equality to null matches null or a missing field.
                if field in document and actual is not None:
                    return False
            elif isinstance(expected, dict):
                for operator, value in expected.items():
                    if operator == "$gte" and actual < value:
                        return False
                    if operator == "$lte" and actual > value:
                        return False
            elif actual != expected:
                return False

        return True

    def find_one(self, query):
        for document in self.documents:
            if self.matches(document, query):
                return deepcopy(document)
        return None

    def find_one_and_update(self, query, update, return_document=None):
        self.update_filters.append(deepcopy(query))
        self.updates.append(deepcopy(update))

        # Simulate another administrator committing a history entry after
        # this request read the original record but before its update.
        if self.conflict_on_update and self.documents:
            self.conflict_on_update = False
            self.documents[0].setdefault("history", []).append({
                "at": datetime(2026, 10, 15, 10, 0, tzinfo=UTC),
                "by": "other-admin",
                "reason": "Concurrent correction",
                "changes": {"status": {"from": "PRESENT", "to": "WFH"}},
            })

        for document in self.documents:
            if not self.matches(document, query):
                continue

            for field, value in update.get("$set", {}).items():
                document[field] = deepcopy(value)

            for field, value in update.get("$push", {}).items():
                document.setdefault(field, []).append(deepcopy(value))

            return deepcopy(document)

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


def seed_record(db, *, status="PRESENT", with_history=True):
    db.employees.documents.append({
        "emp_code": "EMP0001",
        "name": "Test Employee",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
    })

    record = {
        "_id": "record-1",
        "emp_code": "EMP0001",
        "date": "2026-10-12",
        "status": status,
        "punch_in": PUNCH_IN if status not in {"ABSENT", "LEAVE"} else None,
        "punch_out": PUNCH_OUT if status not in {"ABSENT", "LEAVE"} else None,
        "work_hours": 8.5 if status not in {"ABSENT", "LEAVE"} else None,
        "late_minutes": 35 if status not in {"ABSENT", "LEAVE"} else 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if with_history:
        record["history"] = []
    db.attendance_logs.documents.append(record)
    return record


def regularize(client, payload, path_date="2026-10-12"):
    return client.patch(
        f"/attendance/EMP0001/{path_date}",
        json={
            "reason": "Corrected attendance",
            "regularized_by": "admin-1",
            **payload,
        },
    )


def test_patch_recomputes_derived_fields_and_records_real_changes(api):
    client, db = api
    seed_record(db)

    response = regularize(client, {
        "punch_in": main.to_epoch_ms(CORRECTED_IN),
    })

    assert response.status_code == 200
    item = response.json()

    assert item["punch_in"] == main.to_epoch_ms(CORRECTED_IN)
    assert item["punch_out"] == main.to_epoch_ms(PUNCH_OUT)
    assert item["late_minutes"] == 0
    assert item["work_hours"] == 9.12
    assert item["overtime_minutes"] == 0
    assert item["half_day"] is False
    assert len(item["history"]) == 1

    changes = item["history"][0]["changes"]
    assert set(changes) == {"punch_in", "work_hours", "late_minutes"}
    assert changes["punch_in"] == {
        "from": main.to_epoch_ms(PUNCH_IN),
        "to": main.to_epoch_ms(CORRECTED_IN),
    }
    assert changes["work_hours"] == {"from": 8.5, "to": 9.12}
    assert changes["late_minutes"] == {"from": 35, "to": 0}


def test_omitted_status_uses_stored_leave_status(api):
    client, db = api
    seed_record(db, status="LEAVE")

    response = regularize(client, {
        "punch_in": main.to_epoch_ms(CORRECTED_IN),
    })

    assert response.status_code == 422
    stored = db.attendance_logs.documents[0]
    assert stored["status"] == "LEAVE"
    assert stored["punch_in"] is None
    assert stored["history"] == []


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
@pytest.mark.parametrize("time_field", ["punch_in", "punch_out"])
def test_non_presence_status_rejects_supplied_punch_times(
    api, status, time_field
):
    client, db = api
    seed_record(db)

    response = regularize(client, {
        "status": status,
        time_field: main.to_epoch_ms(CORRECTED_IN),
    })

    assert response.status_code == 422
    assert db.attendance_logs.documents[0]["history"] == []


def test_leave_clears_punches_and_records_only_changed_fields(api):
    client, db = api
    seed_record(db)

    response = regularize(client, {"status": "LEAVE"})

    assert response.status_code == 200
    item = response.json()

    assert item["status"] == "LEAVE"
    assert item["punch_in"] is None
    assert item["punch_out"] is None
    assert item["work_hours"] is None
    assert item["late_minutes"] == 0
    assert item["overtime_minutes"] == 0
    assert item["half_day"] is False

    changes = item["history"][0]["changes"]
    assert set(changes) == {
        "status", "punch_in", "punch_out", "work_hours", "late_minutes"
    }


def test_same_second_punch_in_is_a_noop_even_with_milliseconds(api):
    client, db = api
    seed_record(db)

    response = regularize(client, {
        "punch_in": main.to_epoch_ms(PUNCH_IN) + 999,
    })

    assert response.status_code == 422
    assert "changes nothing" in response.json()["detail"]
    assert db.attendance_logs.documents[0]["history"] == []


def test_reason_only_change_is_a_noop(api):
    client, db = api
    seed_record(db)

    response = client.patch(
        "/attendance/EMP0001/2026-10-12",
        json={
            "reason": "Updated reason only",
            "regularized_by": "admin-1",
        },
    )

    assert response.status_code == 422
    assert db.attendance_logs.documents[0]["history"] == []


def test_derived_fields_sent_by_client_are_ignored(api):
    client, db = api
    seed_record(db)

    response = regularize(client, {
        "status": "ON_DUTY",
        "late_minutes": 999,
        "work_hours": 123.45,
        "overtime_minutes": 900,
        "half_day": True,
    })

    assert response.status_code == 200
    item = response.json()
    assert item["status"] == "ON_DUTY"
    assert item["late_minutes"] == 35
    assert item["work_hours"] == 8.5
    assert item["overtime_minutes"] == 0
    assert item["half_day"] is False


def test_legacy_record_without_history_can_be_regularized(api):
    client, db = api
    seed_record(db, with_history=False)

    response = regularize(client, {"status": "WFH"})

    assert response.status_code == 200
    item = response.json()
    assert item["status"] == "WFH"
    assert len(item["history"]) == 1
    assert item["history"][0]["by"] == "admin-1"

    # The conditional update must normalize a missing history field.
    expression = db.attendance_logs.update_filters[0]["$expr"]
    assert expression["$eq"][0]["$ifNull"] == ["$history", []]


def test_concurrent_history_change_returns_409(api):
    client, db = api
    seed_record(db)
    db.attendance_logs.conflict_on_update = True

    response = regularize(client, {"status": "WFH"})

    assert response.status_code == 409
    assert len(db.attendance_logs.documents[0]["history"]) == 1
    assert db.attendance_logs.documents[0]["history"][0]["by"] == "other-admin"


def test_invalid_path_date_returns_422(api):
    client, db = api
    seed_record(db)

    response = regularize(client, {"status": "WFH"}, path_date="2026-02-30")

    assert response.status_code == 422
    assert db.attendance_logs.documents[0]["history"] == []


def test_presence_status_requires_punch_in(api):
    client, db = api
    seed_record(db, status="LEAVE")

    response = regularize(client, {"status": "PRESENT"})

    assert response.status_code == 422
    assert db.attendance_logs.documents[0]["status"] == "LEAVE"


def test_unknown_employee_returns_404(api):
    client, db = api
    # No employee or attendance record is seeded.
    response = client.patch(
        "/attendance/EMP9999/2026-10-12",
        json={
            "status": "WFH",
            "reason": "Corrected attendance",
            "regularized_by": "admin-1",
        },
    )

    assert response.status_code == 404
