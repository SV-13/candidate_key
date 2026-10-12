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

    result = main.health()

    assert result == {"status": "ok"}
    assert fake_db.commands == ["ping"]


def test_health_returns_503_when_database_fails(monkeypatch):
    fake_db = FakeDatabase(
        error=PyMongoError("database unavailable")
    )
    monkeypatch.setattr(main, "db", fake_db)

    with pytest.raises(HTTPException) as exc:
        main.health()

    assert exc.value.status_code == 503
    assert exc.value.detail == "MongoDB unavailable"
    assert fake_db.commands == ["ping"]
