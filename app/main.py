"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Optional
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (BaseModel, Field, field_validator, model_validator)
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from contextlib import asynccontextmanager

load_dotenv()


client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=2000,
)

db = client[os.getenv("MONGO_DB", "attendance_db")]

logger = logging.getLogger(__name__)
indexes_ready = False


def ensure_indexes():
    """Create all indexes required for correct API behaviour."""
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employees_emp_code",
    )

    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
        ],
        unique=True,
        name="uq_attendance_emp_code_date",
    )

    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("punch_in", DESCENDING),
        ],
        name="idx_attendance_emp_code_punch_in",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start in degraded mode if MongoDB or index creation is unavailable."""
    global indexes_ready

    try:
        ensure_indexes()
        indexes_ready = True
    except PyMongoError:
        indexes_ready = False
        logger.warning(
            "MongoDB indexes could not be verified at startup; "
            "the application will start in degraded mode.",
            exc_info=True,
        )

    yield

IST = timezone(timedelta(hours=5, minutes=30))

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def serialize_mongo_value(value):
    """Convert datetimes in MongoDB documents to epoch milliseconds."""
    if isinstance(value, datetime):
        return to_epoch_ms(value)

    if isinstance(value, dict):
        return {
            key: serialize_mongo_value(item)
            for key, item in value.items()
            if key != "_id"
        }

    if isinstance(value, list):
        return [serialize_mongo_value(item) for item in value]

    return value

def serialize_attendance(doc):
    """Normalize an attendance document for the public API."""
    attendance = dict(doc)

    # Legacy documents may not contain these fields.
    attendance.setdefault("late_minutes", 0)
    attendance.setdefault("overtime_minutes", 0)
    attendance.setdefault("half_day", False)
    attendance.setdefault("history", [])

    # Recursively converts datetimes to epoch milliseconds
    # and removes MongoDB _id fields.
    return serialize_mongo_value(attendance)

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
    """Return late minutes after the 10-minute grace period."""
    elapsed_seconds = int((punch_in - shift_start).total_seconds())

    # An employee is late only when more than 10 minutes have elapsed.
    if elapsed_seconds > 600:
        return elapsed_seconds // 60

    return 0

def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> Decimal:
    """Return elapsed work hours rounded to two decimals using half-up rounding."""
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
    """Return whole overtime minutes when the 30-minute threshold is met."""
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

# Strict epoch-millisecond integer used at the API boundary.
EpochMillis = Annotated[
    int,
    Field(
        strict=True,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    ),
]

# Only these statuses are permitted when punching in.
PunchInStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        """Reject impossible calendar dates while preserving the string."""
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "joined_on must be a valid YYYY-MM-DD date"
            ) from exc

        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        return value

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PunchInStatus = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    global indexes_ready

    try:
        # Verify MongoDB can respond.
        db.command("ping")

        # If startup could not create the indexes, retry here.
        if not indexes_ready:
            ensure_indexes()
            indexes_ready = True

    except PyMongoError:
        indexes_ready = False
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        ) from None

    return {"status": "ok"}

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    # 1. Check for an existing employee for a friendly 409 response.
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    # 2. Prepare the document with a UTC timestamp at whole-second precision.
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))

    # 3. The unique index is the final protection against concurrent inserts.
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    # 4. Remove MongoDB's internal ID and convert created_at to epoch ms.
    doc.pop("_id", None)
    return serialize_mongo_value(doc)



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department is not None:
        query["department"] = department

    # Offset for 1-based pagination.
    skip = (page - 1) * page_size

    # Count only employees matching the filter.
    total = db.employees.count_documents(query)

    # Return a stable, sorted page of results.
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    items = [
        serialize_mongo_value(employee)
        for employee in cursor
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # 1. Find the employee before accessing shift details.
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(404, "employee not found")

    # 2. Resolve the timestamp and normalize it to whole seconds.
    if body.punched_at is not None:
        ts = from_epoch_ms(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))

    # 3. Calculate the attendance date in IST, including overnight shifts.
    attendance_date = compute_attendance_date(
        ts,
        emp["shift_start"],
        emp["shift_end"],
    )
    date_str = attendance_date.isoformat()

    # 4. Obtain the actual shift start and calculate lateness.
    shift_start, _ = get_shift_bounds(
        attendance_date,
        emp["shift_start"],
        emp["shift_end"],
    )
    late_minutes = compute_late_minutes(ts, shift_start)

    # 5. Create the new attendance record.
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # 6. Let MongoDB's unique index enforce race-safe uniqueness.
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "already punched in for this date",
        ) from None

    # 7. Return the public API representation; never expose MongoDB _id.
    return serialize_attendance(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # 1. Look up the employee.
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    # 2. Resolve and normalize the requested punch-out instant.
    if body.punched_at is not None:
        ts = from_epoch_ms(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))

    # 3. Find the newest record for this employee that was punched
    #    in at or before the requested punch-out time.
    candidate = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if candidate is None:
        raise HTTPException(404, "attendance record not found")

    # 4. Validate time before checking whether the record is closed.
    #    This means invalid time takes precedence over HTTP 409.
    original_punch_in = candidate["punch_in"]
    elapsed = ts - original_punch_in

    if elapsed.total_seconds() <= 0:
        raise HTTPException(
            422,
            "punched_at must be after punch_in",
        )

    if elapsed > timedelta(hours=24):
        raise HTTPException(
            422,
            "punched_at cannot be more than 24 hours after punch_in",
        )

    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already closed")

    # 5. Calculate values using the attendance record's stored date.
    #    This is important for shifts that cross midnight.
    attendance_date = date.fromisoformat(candidate["date"])

    _, shift_end = get_shift_bounds(
        attendance_date,
        employee["shift_start"],
        employee["shift_end"],
    )

    work_hours_decimal = compute_work_hours(original_punch_in, ts)
    work_hours = float(work_hours_decimal)

    overtime_minutes = compute_overtime(ts, shift_end)
    half_day = compute_half_day(work_hours_decimal)

    # 6. Atomically close the same record we calculated from.
    #    Pin the original punch_in to prevent overwriting a concurrent
    #    correction, and require the record to remain open.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "emp_code": body.emp_code,
            "date": candidate["date"],
            "punch_in": original_punch_in,
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            409,
            "attendance record was already closed or changed",
        )

    # 7. Convert datetimes to epoch milliseconds and hide MongoDB _id.
    #    Punch-out deliberately does not write to history.
    return serialize_attendance(updated)

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
    items = [serialize_attendance(doc) for doc in page_docs]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
