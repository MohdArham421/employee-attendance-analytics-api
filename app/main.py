"""
Employee Attendance & Analytics API - Production Implementation

Contract: OpenAPI 3.0.3 (v2.0.0)
Rules: R1 - R10
Start command: uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import calendar
import os
from contextlib import asynccontextmanager
from datetime import date as dt_date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Environment variables take priority over .env
MONGO_URI = os.environ.get("MONGO_URI", os.getenv("MONGO_URI", "mongodb://localhost:27017"))
MONGO_DB = os.environ.get("MONGO_DB", os.getenv("MONGO_DB", "attendance_db"))

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def ensure_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1)])
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs collection
    # Unique compound index enforcing at most one record per (emp_code, date) & race safety
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # Compound index for listing attendance sorted by date desc, emp_code asc
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    # Compound index for punch-out lookup
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    # Compound index for late leaderboard covered index scan
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure indexes are created idempotently
    try:
        ensure_indexes()
    except Exception as e:
        print(f"Warning: error creating indexes on startup: {e}")
    yield
    # Shutdown
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions (Rules R1 - R8)
# --------------------------------------------------------------------------- #
def quantize_half_up(value: Union[float, Decimal], decimals: int = 2) -> float:
    """Round a number half-up to specified decimals (Rule R8)."""
    d = Decimal(str(value))
    target = Decimal("0." + "0" * decimals) if decimals > 0 else Decimal("1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime to whole seconds (Rule R1)."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime_truncated(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds (Rule R1)."""
    whole_seconds = epoch_ms // 1000
    return datetime.fromtimestamp(whole_seconds, tz=UTC)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC or aware datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def parse_time_str(time_str: str) -> time:
    """Parse HH:MM string into time object."""
    h, m = map(int, time_str.split(":"))
    return time(hour=h, minute=m)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    Compute attendance date per Rule R1:
    - IST calendar date of punch_in.
    - Exception: overnight shift (shift_end <= shift_start), punch-in earlier than shift_end
      belongs to shift starting previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    ist_time = punch_in_ist.time()
    ist_date = punch_in_ist.date()

    start_t = parse_time_str(shift_start)
    end_t = parse_time_str(shift_end)

    if end_t <= start_t:
        # Overnight shift
        if ist_time < end_t:
            return (ist_date - timedelta(days=1)).isoformat()

    return ist_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    Compute late minutes per Rule R2:
    - Grace period 10 minutes (600 seconds).
    - Late only if strictly more than 10:00 after shift_start (> 600s).
    - late_minutes = whole minutes (floor) since shift_start, otherwise 0.
    """
    h, m = map(int, shift_start.split(":"))
    att_date = dt_date.fromisoformat(attendance_date_str)
    start_ist = datetime(att_date.year, att_date.month, att_date.day, h, m, 0, tzinfo=IST)
    punch_in_ist = punch_in_dt.astimezone(IST)

    diff_seconds = (punch_in_ist - start_ist).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    Compute work hours per Rule R4:
    (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up.
    """
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    hours_dec = Decimal(str(diff_sec)) / Decimal("3600")
    return float(hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_dt: datetime, shift_end: str, attendance_date_str: str, shift_start: str) -> int:
    """
    Compute overtime per Rule R3:
    Whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    For overnight shifts, shift_end is on the calendar day after attendance date.
    """
    h, m = map(int, shift_end.split(":"))
    att_date = dt_date.fromisoformat(attendance_date_str)
    start_t = parse_time_str(shift_start)
    end_t = parse_time_str(shift_end)

    if end_t <= start_t:
        # Overnight shift: ends next day
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_ist = datetime(end_date.year, end_date.month, end_date.day, h, m, 0, tzinfo=IST)
    punch_out_ist = punch_out_dt.astimezone(IST)

    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    ot_minutes = int(diff_sec // 60)
    return ot_minutes if ot_minutes >= 30 else 0


def is_half_day(work_hours: Optional[float]) -> bool:
    """Check if work_hours qualifies as half_day (< 4.50) per Rule R5."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    Count Mon-Fri working days for a month from joined_on (Rule R7).
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    first_of_month = dt_date(year, month, 1)
    last_of_month = dt_date(year, month, num_days)

    joined_date = dt_date.fromisoformat(joined_on_str)
    start_date = max(first_of_month, joined_date)
    if start_date > last_of_month:
        return 0

    working_days = 0
    curr = start_date
    while curr <= last_of_month:
        if curr.weekday() < 5:  # Monday=0 ... Friday=4
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format employee document for HTTP response (convert created_at to epoch ms)."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc.get("created_at")),
    }


def format_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Format history entry for HTTP response."""
    changes = {}
    for field, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = datetime_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = datetime_to_epoch_ms(to_val)
        changes[field] = {"from": from_val, "to": to_val}

    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = datetime_to_epoch_ms(at_val)

    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format attendance document for HTTP response (remove _id, epoch ms timestamps)."""
    history = [format_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas with Strict Validation
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    def model_post_init(self, __context: Any) -> None:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")


def validate_epoch_millis(v: Optional[int]) -> Optional[int]:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("Must be an integer")
    if v < 100000000000 or v > 4102444800000:
        raise ValueError("Instant must be epoch milliseconds between 100000000000 and 4102444800000")
    return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_millis(v)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_millis(v)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_punches(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_millis(v)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee. Enforced unique emp_code by MongoDB unique index.
    Server sets created_at. Concurrent duplicate creates yield 409.
    """
    now = datetime.now(tz=UTC)
    doc = body.model_dump()
    doc["created_at"] = now

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending. Total reflects filter."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates the day's record (R1, R2).
    Concurrency safe: unique compound index on (emp_code, date) ensures exactly one 201, rest 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime_truncated(body.punched_at)
    else:
        punch_in_dt = truncate_to_seconds(datetime.now(tz=UTC))

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes most recent record whose punch_in is <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3) and half_day (R5).
    Two simultaneous punch-outs yield exactly one 200, rest 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime_truncated(body.punched_at)
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(tz=UTC))

    # Find most recent record for this employee where punch_in is <= punch_out_dt
    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_dt},
        },
        sort=[("punch_in", -1)],
    )

    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    # Validation: punched_at <= punch_in or > 24 hours after punch_in -> 422
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punch_out must be after punch_in and within 24 hours",
        )

    # Compute derived fields
    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime(punch_out_dt, emp["shift_end"], rec["date"], emp["shift_start"])
    half_d = is_half_day(work_hrs)

    # Atomic update ensuring only an unpunched record is updated
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half_d,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        # Another request already punched out this record
        raise HTTPException(status_code=409, detail="record is already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Uses index scan and pagination cursor directly in MongoDB.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    q: Dict[str, Any] = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize one day's record: recompute derived fields, append to history audit trail.
    Concurrency safe: history is preserved without lost updates.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for that date")

    target_status = body.status if body.status is not None else rec["status"]

    # Rule: setting status ABSENT or LEAVE clears both punch times
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_datetime_truncated(body.punch_in)
        else:
            new_punch_in = rec.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_datetime_truncated(body.punch_out)
        else:
            new_punch_out = rec.get("punch_out")

    # If punch_in changed or maintained for presence status, must stay on the record's attendance date (R1)
    if new_punch_in is not None:
        derived_att_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_att_date != date:
            raise HTTPException(status_code=422, detail="punch_in must belong to the record's attendance date")

    # punch_out must be after punch_in and within 24 hours
    if new_punch_in is not None and new_punch_out is not None:
        pi = new_punch_in if new_punch_in.tzinfo else new_punch_in.replace(tzinfo=UTC)
        po = new_punch_out if new_punch_out.tzinfo else new_punch_out.replace(tzinfo=UTC)
        sec_diff = (po - pi).total_seconds()
        if sec_diff <= 0 or sec_diff > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = is_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compare changes
    changes: Dict[str, Any] = {}

    def normalize_dt(val: Optional[datetime]) -> Optional[datetime]:
        if val is None:
            return None
        return val.replace(tzinfo=UTC) if val.tzinfo is None else val.astimezone(UTC)

    if rec.get("status") != target_status:
        changes["status"] = {"from": rec.get("status"), "to": target_status}

    old_pi = normalize_dt(rec.get("punch_in"))
    new_pi_norm = normalize_dt(new_punch_in)
    if old_pi != new_pi_norm:
        changes["punch_in"] = {"from": old_pi, "to": new_pi_norm}

    old_po = normalize_dt(rec.get("punch_out"))
    new_po_norm = normalize_dt(new_punch_out)
    if old_po != new_po_norm:
        changes["punch_out"] = {"from": old_po, "to": new_po_norm}

    if rec.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hours}

    if rec.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late_minutes}

    if rec.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_overtime_minutes}

    if rec.get("half_day", False) != new_half_day:
        changes["half_day"] = {"from": rec.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected in regularization request")

    # Construct history entry
    now = datetime.now(tz=UTC)
    hist_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrent regularizations: ensure history array length matches old history
    current_history_len = len(rec.get("history", []))
    update_filter = {
        "_id": rec["_id"],
        f"history.{current_history_len}": {"$exists": False},
    }

    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": hist_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict")

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by MongoDB aggregation.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    first_date = f"{month}-01"
    last_date = f"{month}-{num_days:02d}"

    working_days = count_working_days(emp["joined_on"], month)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_date, "$lte": last_date},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = quantize_half_up((present_days / working_days) * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    """Build the cross-collection aggregation pipeline for department summary."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    first_date = f"{month}-01"
    last_date = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {
        "joined_on": {"$lte": last_date}
    }
    if department:
        match_stage["department"] = department

    pipeline: List[Dict[str, Any]] = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", first_date]},
                                    {"$lte": ["$date", last_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]},
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]},
                        }
                    }
                },
                "work_hours_list": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "wl",
                        "in": "$$wl.work_hours",
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes",
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {
            "$match": {
                "headcount": {"$gt": 0}
            }
        },
        {"$sort": {"department": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month via cross-collection MongoDB aggregation.
    Headcount includes people with zero logs and excludes joiners after period end.
    """
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        avg_wh = r.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = quantize_half_up(avg_wh, 2)
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {
        "month": month,
        "items": items,
    }


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    """Build the MongoDB aggregation pipeline for late leaderboard with standard competition ranking."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    first_date = f"{month}-01"
    last_date = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": first_date, "$lte": last_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers ranking with standard competition ranking ties (1, 2, 2, 4).
    Limit is applied after ranking.
    """
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items,
    }


def build_department_trend_pipeline(department: str, date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Build the MongoDB aggregation pipeline for daily department trend with gap filling ($densify)."""
    dt_from = datetime.strptime(date_from, "%Y-%m-%d")
    dt_to_plus1 = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "emp.department": department,
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "date": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to_plus1],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}
                },
                "dayOfWeek": {"$dayOfWeek": "$date_dt"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department with gap filling ($densify) and 7-day moving average.
    """
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = dt_date.fromisoformat(date_from)
    d_to = dt_date.fromisoformat(date_to)

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")

    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    pipeline = build_department_trend_pipeline(department, date_from, date_to)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        att_rate = r.get("attendance_rate")
        if att_rate is not None:
            att_rate = quantize_half_up(att_rate, 4)

        mov_avg = r.get("moving_avg_7d")
        if mov_avg is not None:
            mov_avg = quantize_half_up(mov_avg, 4)

        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(r["present_count"]),
            "late_count": r["late_count"],
            "attendance_rate": att_rate,
            "moving_avg_7d": mov_avg,
        })

    return {
        "department": department,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    date_from_alias: Optional[str] = Query(None, alias="from"),
    date_to_alias: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    """
    MongoDB explain output with executionStats verbosity for an endpoint's main query.
    """
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
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
        skip = (page - 1) * page_size
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly explain")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary explain")
        pipeline = build_department_summary_pipeline(month, department)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_doc,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard explain")
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    elif endpoint == "department_trend":
        d_from = date_from_alias or date_from
        d_to = date_to_alias or date_to
        if not department or not d_from or not d_to:
            raise HTTPException(status_code=422, detail="department, from and to are required for department_trend explain")
        pipeline = build_department_trend_pipeline(department, d_from, d_to)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    raise HTTPException(status_code=422, detail="Unknown endpoint for explain")
