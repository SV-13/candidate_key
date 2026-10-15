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

    db.attendance_logs.create_index(
        [
            ("date", DESCENDING),
            ("emp_code", ASCENDING),
        ],
        name="idx_attendance_date_emp_code",
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


class RegularizeRequest(BaseModel):
    """Fields accepted when manually correcting an attendance record."""

    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


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
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate strict ISO calendar dates, including impossible dates.
    def validate_date(value: Optional[str], field_name: str):
        if value is None:
            return None

        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} must be a valid date in YYYY-MM-DD format",
            )

        if parsed.isoformat() != value:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} must be in YYYY-MM-DD format",
            )

        return parsed

    parsed_date_from = validate_date(date_from, "date_from")
    parsed_date_to = validate_date(date_to, "date_to")

    if (
        parsed_date_from is not None
        and parsed_date_to is not None
        and parsed_date_from > parsed_date_to
    ):
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    # Build one filter and use it for both the total and current page.
    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if parsed_date_from is not None or parsed_date_to is not None:
        query["date"] = {}

        if parsed_date_from is not None:
            query["date"]["$gte"] = parsed_date_from.isoformat()

        if parsed_date_to is not None:
            query["date"]["$lte"] = parsed_date_to.isoformat()

    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    page_docs = list(
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = [serialize_attendance(doc) for doc in page_docs]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    # 1. Validate the path date strictly as YYYY-MM-DD.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            422, "date must be a valid date in YYYY-MM-DD format"
        ) from None

    if parsed_date.isoformat() != date:
        raise HTTPException(
            422, "date must be in YYYY-MM-DD format"
        )

    supplied = body.model_fields_set

    if "status" in supplied and body.status is None:
        raise HTTPException(422, "status cannot be null")

    # 2. Both the employee and the natural-key attendance record must exist.
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if record is None:
        raise HTTPException(404, "attendance record not found")

    # 3. Resolve the final status, preserving the stored status when omitted.
    final_status = (
        body.status if "status" in supplied else record.get("status")
    )
    if final_status not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
        raise HTTPException(422, "record has an invalid status")

    punch_in_supplied = "punch_in" in supplied
    punch_out_supplied = "punch_out" in supplied

    # ABSENT and LEAVE clear both punch times. Supplying either time together
    # with a final non-presence status is invalid, even when status was omitted.
    if final_status in {"ABSENT", "LEAVE"}:
        if punch_in_supplied or punch_out_supplied:
            raise HTTPException(
                422,
                "punch_in and punch_out cannot be supplied with ABSENT or LEAVE",
            )
        final_punch_in = None
        final_punch_out = None
    else:
        try:
            if punch_in_supplied:
                final_punch_in = (
                    from_epoch_ms(body.punch_in)
                    if body.punch_in is not None
                    else None
                )
            else:
                final_punch_in = record.get("punch_in")

            if punch_out_supplied:
                final_punch_out = (
                    from_epoch_ms(body.punch_out)
                    if body.punch_out is not None
                    else None
                )
            else:
                final_punch_out = record.get("punch_out")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    # 4. Recompute every derived field from the final status and final times.
    if final_status in {"PRESENT", "WFH", "ON_DUTY"}:
        if final_punch_in is None:
            raise HTTPException(
                422, "a presence status requires a punch_in"
            )

        if not isinstance(final_punch_in, datetime):
            raise HTTPException(422, "punch_in must be a valid timestamp")

        shift_start, shift_end = get_shift_bounds(
            parsed_date,
            employee["shift_start"],
            employee["shift_end"],
        )

        if compute_attendance_date(
            final_punch_in,
            employee["shift_start"],
            employee["shift_end"],
        ) != parsed_date:
            raise HTTPException(
                422, "punch_in must belong to the record's attendance date"
            )

        if final_punch_out is not None:
            if not isinstance(final_punch_out, datetime):
                raise HTTPException(422, "punch_out must be a valid timestamp")

            duration = final_punch_out - final_punch_in
            if duration.total_seconds() <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if duration > timedelta(hours=24):
                raise HTTPException(
                    422, "punch_out must be within 24 hours of punch_in"
                )

            computed_hours = compute_work_hours(
                final_punch_in, final_punch_out
            )
            new_work_hours = float(computed_hours)
            new_overtime = compute_overtime(final_punch_out, shift_end)
            new_half_day = compute_half_day(computed_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(
            final_punch_in, shift_start
        )
    else:
        # Non-presence statuses have no punch times or derived work metrics.
        new_work_hours = None
        new_late_minutes = 0
        new_overtime = 0
        new_half_day = False

    new_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    # 5. Keep only real changes. Compare timestamps at whole-second precision
    #    and compare work_hours as floats. "from" values remain stored values.
    def equivalent(field, old_value, new_value):
        if field in {"punch_in", "punch_out"}:
            if old_value is None or new_value is None:
                return old_value is None and new_value is None
            if isinstance(old_value, datetime) and isinstance(new_value, datetime):
                return (
                    truncate_to_seconds(old_value)
                    == truncate_to_seconds(new_value)
                )

        if field == "work_hours":
            if old_value is None or new_value is None:
                return old_value is None and new_value is None
            try:
                return float(old_value) == float(new_value)
            except (TypeError, ValueError):
                return old_value == new_value

        return old_value == new_value

    changes = {}
    for field, new_value in new_values.items():
        old_value = record.get(field)
        if not equivalent(field, old_value, new_value):
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(422, "request changes nothing")

    # A missing history field represents an empty history. Other malformed
    # history values are not safe to append to.
    if "history" not in record:
        history_snapshot = []
    else:
        history_snapshot = record["history"]

    if not isinstance(history_snapshot, list):
        raise HTTPException(409, "attendance history is not an array")

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # 6. Atomically pin the original mutable state AND original history.
    #    $ifNull lets the history comparison match legacy documents where
    #    the field is missing; $literal treats the snapshot as a value.
    update_filter = {
        "_id": record["_id"],
        "emp_code": emp_code,
        "date": date,
        "$expr": {
            "$eq": [
                {"$ifNull": ["$history", []]},
                {"$literal": history_snapshot},
            ]
        },
    }

    for field in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        update_filter[field] = record.get(field)

    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            409,
            "attendance record was changed concurrently; retry the regularization",
        )

    # 7. Use the shared public representation, including nested history times.
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
