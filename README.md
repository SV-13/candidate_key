# Employee Attendance & Analytics API

A FastAPI application backed by MongoDB for employee management, attendance tracking, manual attendance corrections, and analytics.

## Setup

Requirements: Python 3.12+, Docker, and MongoDB 6.0 or newer.

Start MongoDB locally:

```bash
docker run -d --name attendance-mongo -p 27017:27017 mongo:7
```

Create and activate the virtual environment, then install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create a `.env` file in the project root with:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Do not commit `.env` or other secrets.

Start the API:

```bash
uvicorn app.main:app --port 8000 --reload
```

API documentation is available at `http://127.0.0.1:8000/docs`. Check database readiness at `http://127.0.0.1:8000/health`.

## Testing

Install the test dependencies if needed:

```bash
python -m pip install pytest httpx
```

Run the automated tests:

```bash
pytest -q
```

For concurrency checks against a running API and real MongoDB:

```bash
python scripts/race_test.py
```

The concurrency script creates test records and leaves them in the database. Use a development or test database.

## Status

**Done**
- Employee creation and listing, including validation and pagination.
- Punch-in and punch-out, including timestamp validation and concurrency protection.
- Shared attendance response serialization and legacy-field defaults.
- `GET /attendance`, including query validation, database-side pagination, stable sorting, and regression tests.
- `PATCH /attendance/{emp_code}/{date}`, including validation, derived-field recomputation, no-op rejection, audit history, and atomic conflict protection.
- Database health checks and automated tests for implemented behavior.

**In progress**
- Final review of `REVIEW.md` and `DECISIONS.md` submission documentation.

**Not started**
- The four analytics endpoints.
- `/admin/explain/{endpoint}`.

## Design notes

- Instants are stored as timezone-aware UTC datetimes and returned as epoch milliseconds. Attendance dates and shift calculations follow IST, including overnight shifts.
- Punch-out selects the most recent record whose `punch_in` is at or before `punched_at`; if no record qualifies, it returns `404`, even if a future punch-in exists. The selected record is closed with a conditional atomic MongoDB update.
- If MongoDB or required index creation is unavailable at startup, the application can start in degraded mode. `/health` returns `503` until database connectivity and required indexes are established.
