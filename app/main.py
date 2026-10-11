"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient

load_dotenv()


client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)

db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def from_epoch_ms(value: int) -> datetime:
    """Convert a strict epoch-millisecond integer to whole-second UTC."""
    if type(value) is not int:
        raise ValueError("Timestamp must be an integer in epoch milliseconds")

    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("Timestamp is outside the permitted range")

    whole_seconds = value // 1000

    return EPOCH_UTC + timedelta(seconds=whole_seconds)


def to_epoch_ms(value: datetime) -> int:
    """Convert an aware datetime to whole-second epoch milliseconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime must be timezone-aware")

    normalized = truncate_to_seconds(value).astimezone(timezone.utc)
    elapsed = normalized - EPOCH_UTC

    total_seconds = elapsed.days * 86_400 + elapsed.seconds
    return total_seconds * 1000


def truncate_to_seconds(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC and discard microseconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime must be timezone-aware")

    return value.astimezone(timezone.utc).replace(microsecond=0)

def compute_attendance_date(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> date:
    """Return the IST attendance date, accounting for overnight shifts."""
    if punch_in.tzinfo is None or punch_in.utcoffset() is None:
        raise ValueError("Datetime must be timezone-aware")

    # Convert the instant to IST before determining its calendar date.
    local_punch_in = punch_in.astimezone(IST)
    attendance_date = local_punch_in.date()

    # If the shift crosses midnight, an instant earlier than shift_end
    # belongs to the shift that started on the previous calendar day.
    if shift_end <= shift_start:
        shift_end_time = datetime.strptime(shift_end, "%H:%M").time()

        if local_punch_in.time() < shift_end_time:
            attendance_date -= timedelta(days=1)

    return attendance_date

def get_shift_bounds(
    attendance_date: date,
    shift_start: str,
    shift_end: str,
) -> tuple[datetime, datetime]:
    """Return shift start and end as timezone-aware UTC datetimes."""

    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()

    # Overnight shifts end on the following calendar day.
    end_date = (
        attendance_date + timedelta(days=1)
        if shift_end <= shift_start
        else attendance_date
    )

    # Combine dates and times using IST, then convert to UTC.
    start_ist = datetime.combine(attendance_date, start_time, tzinfo=IST)
    end_ist = datetime.combine(end_date, end_time, tzinfo=IST)

    return (
        start_ist.astimezone(timezone.utc),
        end_ist.astimezone(timezone.utc),
    )

def compute_late_minutes(
    punch_in: datetime,
    shift_start: datetime,
) -> int:

    elapsed_seconds = int((punch_in - shift_start).total_seconds())

    # An employee is late only when more than 10 minutes have elapsed.
    if elapsed_seconds > 600:
        return elapsed_seconds // 60

    return 0

def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> Decimal:

    duration = punch_out - punch_in
    elapsed_seconds = duration.days * 86400 + duration.seconds

    hours = Decimal(elapsed_seconds) / Decimal(3600)

    return hours.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def compute_half_day(work_hours: Decimal) -> bool:
    """Return True when rounded work hours are below 4.50."""
    return work_hours < Decimal("4.50")


def compute_overtime(
    punch_out: datetime,
    shift_end: datetime,
) -> int:

    elapsed_seconds = int((punch_out - shift_end).total_seconds())

    # Early punch-outs and negative gaps never produce overtime.
    if elapsed_seconds < 0:
        return 0

    whole_minutes = elapsed_seconds // 60

    # R3: overtime is counted only when it reaches 30 whole minutes.
    return whole_minutes if whole_minutes >= 30 else 0



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = page * page_size
    total = db.employees.count_documents({})
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now(timezone.utc)
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
