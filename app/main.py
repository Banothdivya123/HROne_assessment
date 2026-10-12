"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import math
import os
import re
import calendar
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from contextlib import asynccontextmanager

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EPOCH_MIN = 100000000000
EPOCH_MAX = 4102444800000


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
def create_indexes():
    try:
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index("department")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index("date")
    except Exception:
        pass


create_indexes()


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is UTC timezone-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _round2(val: float) -> float:
    """Round to 2 decimals using ROUND_HALF_UP."""
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round4(val: float) -> float:
    """Round to 4 decimals using ROUND_HALF_UP."""
    return float(Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _trunc_to_seconds(dt: datetime) -> datetime:
    """Truncate a datetime to whole seconds (R1)."""
    dt = _ensure_utc(dt)
    return dt.replace(microsecond=0)


def _epoch_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime, truncated to seconds."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return _trunc_to_seconds(dt)


def _dt_to_epoch(dt: datetime) -> int:
    """Convert datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _validate_epoch(val: Any, field_name: str = "punched_at") -> int:
    """Validate that an epoch value is a proper integer in range."""
    if val is None:
        return None
    if isinstance(val, float):
        raise HTTPException(422, detail=f"{field_name} must be an integer, not a float")
    if isinstance(val, str):
        raise HTTPException(422, detail=f"{field_name} must be an integer, not a string")
    if not isinstance(val, int):
        raise HTTPException(422, detail=f"{field_name} must be an integer")
    if val < EPOCH_MIN or val > EPOCH_MAX:
        raise HTTPException(422, detail=f"{field_name} out of range")
    return val


def _compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """Determine the attendance date per R1. Returns YYYY-MM-DD string."""
    punch_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_ist.date()

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    # Overnight shift: shift_end <= shift_start
    is_overnight = (eh, em) <= (sh, sm)
    if is_overnight:
        # If punch-in IST time is before shift_end, it belongs to the previous day's shift
        punch_time = punch_ist.time()
        end_time = punch_ist.replace(hour=eh, minute=em, second=0, microsecond=0).time()
        if punch_time < end_time:
            ist_date = ist_date - timedelta(days=1)

    return ist_date.isoformat()


def _compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """R2: late_minutes. Grace 10 min. Late only if strictly > 10:00 after shift_start."""
    punch_ist = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start = punch_ist.replace(hour=h, minute=m, second=0, microsecond=0)

    # For overnight shifts where punch is before midnight but shift starts next concept
    # Actually: if punch_ist < start (which can happen for overnight shifts where punch is before midnight
    # and start is say 22:00, but punch is 21:55) — we handle that via the diff:
    diff_seconds = (punch_ist - start).total_seconds()

    if diff_seconds < 0:
        # For overnight: punch before shift start on same calendar day means
        # the start was actually yesterday. But _compute_attendance_date handles
        # the date assignment. For late calc, if punch is before shift start, not late.
        return 0

    # Grace: strictly more than 10:00 (600 seconds)
    if diff_seconds > 600:
        return int(diff_seconds // 60)  # floor of total minutes since shift_start
    return 0


def _compute_overtime(punch_out_utc: datetime, shift_end: str, att_date: str, shift_start: str) -> int:
    """R3: overtime_minutes. Only counted if >= 30 min past shift_end."""
    punch_out_ist = punch_out_utc.astimezone(IST)
    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))

    # Determine the calendar day of the shift end
    att = date_type.fromisoformat(att_date)
    is_overnight = (eh, em) <= (sh, sm)

    if is_overnight:
        # Shift end is on the next calendar day
        end_day = att + timedelta(days=1)
    else:
        end_day = att

    end_dt = datetime(end_day.year, end_day.month, end_day.day, eh, em, 0, tzinfo=IST)
    diff_seconds = (punch_out_ist - end_dt).total_seconds()

    if diff_seconds < 0:
        return 0

    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def _compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: work_hours = seconds / 3600, rounded 2 dec half-up."""
    punch_in = _ensure_utc(punch_in)
    punch_out = _ensure_utc(punch_out)
    seconds = (punch_out - punch_in).total_seconds()
    return _round2(seconds / 3600)


def _compute_half_day(work_hours: float) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    return work_hours < 4.50


def _format_attendance(doc: dict) -> dict:
    """Convert a MongoDB attendance document to API response format."""
    result = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _dt_to_epoch(doc.get("punch_in")),
        "punch_out": _dt_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _format_history(doc.get("history", [])),
    }
    return result


def _format_history(history: list) -> list:
    """Convert history entries: BSON dates to epoch ms."""
    result = []
    for entry in history:
        formatted = {
            "at": _dt_to_epoch(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                formatted["changes"][field] = {
                    "from": _dt_to_epoch(change.get("from")),
                    "to": _dt_to_epoch(change.get("to")),
                }
            else:
                formatted["changes"][field] = {
                    "from": change.get("from"),
                    "to": change.get("to"),
                }
        result.append(formatted)
    return result


def _format_employee(doc: dict) -> dict:
    """Convert a MongoDB employee document to API response format."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": _dt_to_epoch(doc["created_at"]),
    }


def _validate_page_params(page: int, page_size: int):
    if page < 1:
        raise HTTPException(422, detail="page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, detail="page_size must be between 1 and 100")


def _validate_date(d: str, name: str = "date") -> str:
    if not DATE_RE.match(d):
        raise HTTPException(422, detail=f"Invalid {name} format, expected YYYY-MM-DD")
    try:
        date_type.fromisoformat(d)
    except ValueError:
        raise HTTPException(422, detail=f"Invalid {name}: not a valid date")
    return d


def _validate_month(m: str) -> str:
    if not MONTH_RE.match(m):
        raise HTTPException(422, detail="Invalid month format, expected YYYY-MM")
    return m


def _month_range(month_str: str) -> tuple:
    """Return (first_day, last_day) as YYYY-MM-DD strings for a YYYY-MM month."""
    year, mon = map(int, month_str.split("-"))
    first = f"{year:04d}-{mon:02d}-01"
    last_day = calendar.monthrange(year, mon)[1]
    last = f"{year:04d}-{mon:02d}-{last_day:02d}"
    return first, last


def _working_days_in_range(start_date: str, end_date: str) -> int:
    """Count Mon-Fri days between start_date and end_date inclusive."""
    sd = date_type.fromisoformat(start_date)
    ed = date_type.fromisoformat(end_date)
    count = 0
    d = sd
    while d <= ed:
        if d.weekday() < 5:  # Mon=0..Fri=4
            count += 1
        d += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    # Validate emp_code format
    if not EMP_CODE_RE.match(body.emp_code):
        raise HTTPException(422, detail="emp_code must match ^EMP\\d{4,6}$")

    # Validate name
    if not body.name or len(body.name) > 100:
        raise HTTPException(422, detail="name must be 1-100 characters")

    # Validate email
    if not EMAIL_RE.match(body.email) or len(body.email) > 120:
        raise HTTPException(422, detail="Invalid email format")

    # Validate department
    if not body.department or len(body.department) > 50:
        raise HTTPException(422, detail="department must be 1-50 characters")

    # Validate shift times
    if not SHIFT_RE.match(body.shift_start):
        raise HTTPException(422, detail="Invalid shift_start format")
    if not SHIFT_RE.match(body.shift_end):
        raise HTTPException(422, detail="Invalid shift_end format")
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start must differ from shift_end")

    # Validate joined_on
    _validate_date(body.joined_on, "joined_on")

    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": datetime.now(timezone.utc),
    }

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    doc.pop("_id", None)
    return _format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    _validate_page_params(page, page_size)
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [_format_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Punch-in
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    # Validate status
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {sorted(PRESENCE_STATUSES)}")

    # Validate punched_at if provided
    if body.punched_at is not None:
        _validate_epoch(body.punched_at, "punched_at")

    # Find employee
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Determine punch-in time
    if body.punched_at is not None:
        punch_dt = _epoch_to_dt(body.punched_at)
    else:
        punch_dt = _trunc_to_seconds(datetime.now(timezone.utc))

    # Determine attendance date (R1)
    att_date = _compute_attendance_date(punch_dt, emp["shift_start"], emp["shift_end"])

    # Compute late_minutes (R2)
    late_mins = _compute_late_minutes(punch_dt, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")

    doc.pop("_id", None)
    return _format_attendance(doc)


# --------------------------------------------------------------------------- #
# Punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    # Validate punched_at if provided
    if body.punched_at is not None:
        _validate_epoch(body.punched_at, "punched_at")

    # Find employee
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Determine punch-out time
    if body.punched_at is not None:
        punch_out_dt = _epoch_to_dt(body.punched_at)
    else:
        punch_out_dt = _trunc_to_seconds(datetime.now(timezone.utc))

    # Find the most recent record with punch_in <= punched_at and no punch_out
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_dt, "$ne": None},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, detail="No open punch-in found")

    # Check if already punched out (concurrency: use atomic update)
    punch_in_dt = _ensure_utc(record["punch_in"])
    punch_out_dt = _ensure_utc(punch_out_dt)

    # Validate: punch_out > punch_in
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, detail="punch_out must be after punch_in")

    # Validate: within 24 hours
    if (punch_out_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Compute derived fields
    work_hrs = _compute_work_hours(punch_in_dt, punch_out_dt)
    ot_mins = _compute_overtime(punch_out_dt, emp["shift_end"], record["date"], emp["shift_start"])
    hd = _compute_half_day(work_hrs)

    # Atomic update: only update if punch_out is still None (concurrency)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": hd,
            }
        },
    )

    if result.modified_count == 0:
        raise HTTPException(409, detail="Already punched out")

    # Fetch updated record
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return _format_attendance(updated)


# --------------------------------------------------------------------------- #
# List attendance
# --------------------------------------------------------------------------- #
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    _validate_page_params(page, page_size)

    if date_from:
        _validate_date(date_from, "date_from")
    if date_to:
        _validate_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"Invalid status: {status}")

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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [_format_attendance(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Regularize (PATCH)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeRequest = ...,
):
    _validate_date(date, "date")

    # Validate reason length
    if len(body.reason) < 5 or len(body.reason) > 200:
        raise HTTPException(422, detail="reason must be 5-200 characters")
    if len(body.regularized_by) < 1 or len(body.regularized_by) > 50:
        raise HTTPException(422, detail="regularized_by must be 1-50 characters")

    # Validate status
    if body.status is not None and body.status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"Invalid status: {body.status}")

    # Validate epoch fields
    if body.punch_in is not None:
        _validate_epoch(body.punch_in, "punch_in")
    if body.punch_out is not None:
        _validate_epoch(body.punch_out, "punch_out")

    # Employee must exist
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Record must exist
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]

    # If setting ABSENT/LEAVE, punch times must be cleared
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="Cannot supply punch times with ABSENT/LEAVE status")

    # Build the update
    new_punch_in = _ensure_utc(record.get("punch_in"))
    new_punch_out = _ensure_utc(record.get("punch_out"))

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        if body.punch_in is not None:
            new_punch_in = _ensure_utc(_epoch_to_dt(body.punch_in))
        if body.punch_out is not None:
            new_punch_out = _ensure_utc(_epoch_to_dt(body.punch_out))

        # Presence status requires punch_in
        if new_punch_in is None:
            raise HTTPException(422, detail="Presence status requires punch_in")

        # Validate punch_in stays on attendance date (R1)
        computed_date = _compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422, detail="punch_in must stay on the record's attendance date")

        # Validate punch_out constraints
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Compute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work_hours = None
        new_ot = 0
        new_hd = False
    elif new_punch_out is not None:
        new_late = _compute_late_minutes(new_punch_in, emp["shift_start"])
        new_work_hours = _compute_work_hours(new_punch_in, new_punch_out)
        new_ot = _compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
        new_hd = _compute_half_day(new_work_hours)
    else:
        new_late = _compute_late_minutes(new_punch_in, emp["shift_start"])
        new_work_hours = None
        new_ot = 0
        new_hd = False

    # Detect what actually changed
    changes = {}

    old_status = record["status"]
    old_punch_in = _ensure_utc(record.get("punch_in"))
    old_punch_out = _ensure_utc(record.get("punch_out"))
    old_work_hours = record.get("work_hours")
    old_late = record.get("late_minutes", 0)
    old_ot = record.get("overtime_minutes", 0)
    old_hd = record.get("half_day", False)

    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, detail="No changes detected")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update with version check using history length to prevent lost updates
    old_history_len = len(record.get("history", []))
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, old_history_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count == 0:
        raise HTTPException(409, detail="Concurrent modification detected")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return _format_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics: Employee Monthly
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    _validate_month(month)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first_day, last_day = _month_range(month)

    # Working days: Mon-Fri in the month, starting from joined_on if later
    joined = emp["joined_on"]
    eff_start = max(first_day, joined)
    if eff_start > last_day:
        # Employee hasn't joined yet during this month
        working_days = 0
    else:
        working_days = _working_days_in_range(eff_start, last_day)

    # Aggregation pipeline for attendance stats
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$group": {
                "_id": None,
                "present_full": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$eq": [
                                            {"$ifNull": ["$half_day", False]},
                                            False,
                                        ]
                                    },
                                    # Only Mon-Fri for present_days
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
                                            [2, 3, 4, 5, 6],  # Mon=2..Fri=6 in $dayOfWeek
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "present_half": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        r = results[0]
        present_days = r["present_full"] + r["present_half"] * 0.5
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late_minutes = r["total_late_minutes"]
        total_overtime_minutes = r["total_overtime_minutes"]
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = _round2(present_days / working_days * 100)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Analytics: Department Summary
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    _validate_month(month)
    first_day, last_day = _month_range(month)

    # Get headcount per department (R9: joined_on <= last_day)
    emp_match = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    emp_pipeline = [
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
    ]
    dept_data = {r["_id"]: r for r in db.employees.aggregate(emp_pipeline)}

    if not dept_data:
        return {"month": month, "items": []}

    # Attendance aggregation per department
    att_match = {"date": {"$gte": first_day, "$lte": last_day}}
    if department:
        # Only include emp_codes from the target department
        if department in dept_data:
            att_match["emp_code"] = {"$in": dept_data[department]["emp_codes"]}
        else:
            return {"month": month, "items": []}

    att_pipeline = [
        {"$match": att_match},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": {"path": "$emp_info", "preserveNullAndEmptyArrays": False}},
        {
            "$group": {
                "_id": "$emp_info.department",
                "present_full": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, False]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "present_half": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
    ]

    att_data = {r["_id"]: r for r in db.attendance_logs.aggregate(att_pipeline)}

    items = []
    for dept_name in sorted(dept_data.keys()):
        dd = dept_data[dept_name]
        hc = dd["headcount"]
        if hc == 0:
            continue

        ad = att_data.get(dept_name, {})
        present_full = ad.get("present_full", 0)
        present_half = ad.get("present_half", 0)
        present_days = present_full + present_half * 0.5

        wh_count = ad.get("work_hours_count", 0)
        if wh_count > 0:
            avg_wh = _round2(ad.get("work_hours_sum", 0) / wh_count)
        else:
            avg_wh = None

        items.append({
            "department": dept_name,
            "headcount": hc,
            "present_days": present_days,
            "avg_work_hours": avg_wh,
            "late_count": ad.get("late_count", 0),
            "total_late_minutes": ad.get("total_late_minutes", 0),
            "leave_count": ad.get("leave_count", 0),
            "on_duty_count": ad.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: Late Leaderboard
# --------------------------------------------------------------------------- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    _validate_month(month)
    first_day, last_day = _month_range(month)

    pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": {"path": "$emp_info", "preserveNullAndEmptyArrays": False}},
    ]

    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline.append(
        {"$sort": {"total_late_minutes": -1, "_id": 1}}
    )

    results = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking
    items = []
    if results:
        rank = 1
        for i, r in enumerate(results):
            if i > 0 and r["total_late_minutes"] < results[i - 1]["total_late_minutes"]:
                rank = i + 1
            if rank > limit:
                break
            items.append({
                "rank": rank,
                "emp_code": r["_id"],
                "name": r["emp_info"]["name"],
                "department": r["emp_info"]["department"],
                "total_late_minutes": r["total_late_minutes"],
                "late_count": r["late_count"],
            })

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: Department Trend
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    _validate_date(date_from, "from")
    _validate_date(date_to, "to")

    if date_to < date_from:
        raise HTTPException(422, detail="'to' must be >= 'from'")

    from_d = date_type.fromisoformat(date_from)
    to_d = date_type.fromisoformat(date_to)
    if (to_d - from_d).days > 92:
        raise HTTPException(422, detail="Range must not exceed 92 days")

    # Check department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, detail="Department not found")

    # Get all employees in department with their joined_on dates
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))
    emp_codes = [e["emp_code"] for e in emps]

    # Fetch attendance records for the date range
    att_records = list(db.attendance_logs.find(
        {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": date_from, "$lte": date_to},
        },
        {"_id": 0},
    ))

    # Group by date
    att_by_date = {}
    for rec in att_records:
        d = rec["date"]
        if d not in att_by_date:
            att_by_date[d] = []
        att_by_date[d].append(rec)

    # Build daily items
    items = []
    non_null_rates = []  # for moving_avg_7d within the window
    current = from_d
    while current <= to_d:
        d_str = current.isoformat()
        is_working_day = current.weekday() < 5  # Mon=0..Fri=4

        # Headcount: employees with joined_on <= current day (R9)
        headcount = sum(1 for e in emps if e["joined_on"] <= d_str)

        # Records for this day
        day_records = att_by_date.get(d_str, [])

        # Present count: presence status records. Half day = 0.5
        present_count = 0
        late_count = 0
        for rec in day_records:
            if rec["status"] in PRESENCE_STATUSES:
                if rec.get("half_day", False):
                    present_count += 0.5
                else:
                    present_count += 1
            if rec.get("late_minutes", 0) > 0:
                late_count += 1

        # Attendance rate: only on working days with headcount > 0
        if is_working_day and headcount > 0:
            attendance_rate = _round4(present_count / headcount)
        else:
            attendance_rate = None

        # Moving avg 7d: mean of non-null attendance_rate in current + up to 6 preceding rows
        non_null_rates.append(attendance_rate)
        # Window: last 7 entries (including current)
        window = non_null_rates[-7:]
        non_null_in_window = [r for r in window if r is not None]
        if non_null_in_window:
            moving_avg = _round4(sum(non_null_in_window) / len(non_null_in_window))
        else:
            moving_avg = None

        items.append({
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg,
        })

        current += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from"),
    date_to: Optional[str] = Query(None, alias="date_to"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, detail=f"endpoint must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
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
        skip_val = (page - 1) * page_size

        explain = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip_val).limit(page_size).explain()

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, detail="emp_code is required")
        if not month:
            raise HTTPException(422, detail="month is required")
        _validate_month(month)
        first_day, last_day = _month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {"$group": {"_id": None, "count": {"$sum": 1}}},
        ]
        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        _validate_month(month)
        first_day, last_day = _month_range(month)
        match_stage = {"date": {"$gte": first_day, "$lte": last_day}}
        pipeline = [
            {"$match": match_stage},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp_info"}},
            {"$unwind": "$emp_info"},
            {"$group": {"_id": "$emp_info.department", "count": {"$sum": 1}}},
        ]
        if department:
            pipeline[0]["$match"]["emp_code"] = {"$exists": True}

        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        _validate_month(month)
        first_day, last_day = _month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}},
            {"$match": {"total": {"$gt": 0}}},
        ]
        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, detail="department is required")
        if not from_date or not to_date:
            raise HTTPException(422, detail="from and to are required")

        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        pipeline = [
            {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
