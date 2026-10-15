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

**Implemented**
- Employee creation and listing, including validation, filtering, pagination, and sorting.
- Punch-in and punch-out, including timestamp validation, concurrency protection, and attendance calculations.
- `GET /attendance`, including filtering, database-side pagination, stable sorting, and response serialization.
- `PATCH /attendance/{emp_code}/{date}`, including regularization validation, derived-field recomputation, atomic conflict protection, and audit history.
- `GET /analytics/employees/{emp_code}/monthly`, including working-day calculations and four-decimal attendance percentage rounding.
- `GET /analytics/departments/summary`, including cross-collection aggregation, correct headcount, and record-weighted work-hour averages.
- `GET /analytics/leaderboard/late`, including competition ranking and tie-safe limit handling.
- `GET /analytics/departments/{department}/trend`, including daily gap filling, date-dependent headcount, and the rolling 7-day average.
- `/admin/explain/{endpoint}`, returning raw MongoDB explain output for the five supported query/aggregation targets.
- Database readiness checks and regression tests. The latest full test run reported 141 passed, with one non-fatal Starlette deprecation warning.

**Scale validation**
- Explain plans were checked against a locally seeded dataset of 100,000 attendance records. All five supported targets returned HTTP 200, and expected indexes were observed in the main query and active lookup stages. No collection scans were reported in the tested lookup stages. This is a local validation, not a production benchmark.

## Design notes

- Instants are stored as timezone-aware UTC datetimes and returned as epoch milliseconds. Attendance dates and shift calculations follow IST, including overnight shifts.
- Punch-out selects the most recent record whose `punch_in` is at or before `punched_at`; if no record qualifies, it returns `404`, even if a future punch-in exists. The selected record is closed with a conditional atomic MongoDB update.
- If MongoDB or required index creation is unavailable at startup, the application can start in degraded mode. `/health` returns `503` until database connectivity and required indexes are established.
