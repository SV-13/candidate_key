import pytest
from fastapi import HTTPException
from pymongo.errors import PyMongoError

import app.main as main


class FakeDatabase:
    def __init__(self, error=None):
        self.error = error
        self.commands = []

    def command(self, name):
        self.commands.append(name)

        if self.error is not None:
            raise self.error

        return {"ok": 1}


def test_health_pings_database(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(main, "db", fake_db)
    monkeypatch.setattr(main, "indexes_ready", True)

    result = main.health()

    assert result == {"status": "ok"}
    assert fake_db.commands == ["ping"]


def test_health_returns_503_when_database_fails(monkeypatch):
    fake_db = FakeDatabase(
        error=PyMongoError("database unavailable")
    )
    monkeypatch.setattr(main, "db", fake_db)
    monkeypatch.setattr(main, "indexes_ready", True)

    with pytest.raises(HTTPException) as exc:
        main.health()

    assert exc.value.status_code == 503
    assert exc.value.detail == "MongoDB unavailable"
    assert fake_db.commands == ["ping"]

def test_health_retries_index_creation_when_not_ready(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(main, "db", fake_db)
    monkeypatch.setattr(main, "indexes_ready", False)

    calls = []

    def fake_ensure_indexes():
        calls.append("called")

    monkeypatch.setattr(main, "ensure_indexes", fake_ensure_indexes)

    result = main.health()

    assert result == {"status": "ok"}
    assert calls == ["called"]
    assert main.indexes_ready is True


def test_health_returns_503_when_index_retry_fails(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(main, "db", fake_db)
    monkeypatch.setattr(main, "indexes_ready", False)

    def fail_index_creation():
        raise PyMongoError("index creation failed")

    monkeypatch.setattr(main, "ensure_indexes", fail_index_creation)

    with pytest.raises(HTTPException) as exc:
        main.health()

    assert exc.value.status_code == 503
    assert exc.value.detail == "MongoDB unavailable"
    assert main.indexes_ready is False