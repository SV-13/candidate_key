# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.

## Development and testing

Install the development and testing dependencies in your virtual environment:

```bash
python -m pip install pytest httpx
```

Run the automated test suite:

```bash
pytest -q
```

Run the real-MongoDB concurrency tests with the API and MongoDB running:

```bash
python scripts/race_test.py
```

The concurrency script sends 20 simultaneous requests to each endpoint. Expected results are one `201` and nineteen `409` responses for employee creation, one `201` and nineteen `409` responses for punch-in, and one `200` and nineteen `409` responses for punch-out.

The concurrency script creates test employees and attendance records in the configured database and leaves them there. Run it against a development or test database, not production.

## Attendance implementation notes

API timestamps are integer epoch milliseconds. Datetimes used for storage and calculations are timezone-aware UTC instants; attendance dates and shift bounds follow IST, including overnight shifts.

Punch-out timestamps must be strictly later than punch-in and no more than 24 hours later. Exactly 24 hours is allowed. Invalid punch-out timestamps are validated before checking whether the attendance record is already closed, so an invalid timestamp returns `422` rather than `409`.

Punch-out uses a conditional atomic MongoDB update to prevent two simultaneous requests from closing the same attendance record successfully. Punch-in and punch-out do not write to the attendance `history` field.

The application uses a two-second MongoDB server-selection timeout. If MongoDB or required index creation is unavailable during startup, the application can start in degraded mode; `/health` returns `503` until database readiness and required indexes are established.
