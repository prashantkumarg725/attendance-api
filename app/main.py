"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real env vars win)

Layout of this single file
  1. Config, database handles, indexes
  2. Time helpers + business rules R1-R5 (pure functions, no database)
  3. Request models (validation) and response serialisers
  4. Employees, attendance endpoints
  5. Aggregation pipeline builders + analytics endpoints
  6. Explain endpoint (re-uses the same builders as the real endpoints)

Design notes
  * Every instant is a tz-aware UTC datetime in MongoDB and epoch *milliseconds* in the API.
  * All "exactly once" guarantees come from the database (unique indexes, conditional updates),
    never from check-then-write.
  * Rounding is half-up and done with integer arithmetic (MongoDB's $round is banker's rounding,
    and float rounding is not exact), see half_up_div() and half_up_expr().
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time as Time, timedelta, timezone
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import IndexModel, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Config, database, indexes
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False (default): real environment variables take priority over .env

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns aware UTC datetimes instead of naive ones, so no accidental
# "naive local time" arithmetic anywhere in this file.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists with the same spec is a no-op."""
    global _indexes_ready
    employees.create_indexes(
        [
            # uniqueness of the business key (concurrent duplicate creates -> exactly one wins)
            IndexModel([("emp_code", 1)], unique=True, name="uniq_emp_code"),
            # GET /employees?department=X sorted by emp_code; also the department -> emp_codes lookup
            IndexModel([("department", 1), ("emp_code", 1)], name="dept_emp_code"),
            # headcount: department + joined_on <= end of period
            IndexModel([("department", 1), ("joined_on", 1)], name="dept_joined_on"),
            # same headcount filter when no department is given
            IndexModel([("joined_on", 1)], name="joined_on"),
        ]
    )
    logs.create_indexes(
        [
            # one record per (emp_code, date): the guard for racing punch-ins, and the access path
            # for per-employee reads, monthly analytics and the department trend ($in emp_codes)
            IndexModel([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date"),
            # GET /attendance without emp_code (sorted date desc, emp_code asc) and leaderboard date ranges
            IndexModel([("date", -1), ("emp_code", 1)], name="date_emp"),
            # GET /attendance?status=X, already in sort order
            IndexModel([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp"),
            # punch-out: "latest record of this employee with punch_in <= t"
            IndexModel([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in"),
        ]
    )
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # DB not reachable yet: /health reports 503 and retries index creation
        log.warning("could not create indexes at startup: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def _db_down(_: Request, exc: ConnectionFailure):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules (pure functions)
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ABSENCE = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_HUNDREDTHS = 450  # R5: rounded work_hours < 4.50
MAX_SHIFT = timedelta(hours=24)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime (exact integer arithmetic, no float)."""
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:  # defensive: treat naive values as UTC (Mongo always stores UTC)
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt - EPOCH
    return d.days * 86_400_000 + d.seconds * 1000 + d.microseconds // 1000


def ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else dt_to_ms(dt)


def now_utc() -> datetime:
    """Current time truncated to milliseconds, which is exactly what BSON can store."""
    n = datetime.now(timezone.utc)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def half_up_div(num: int, den: int) -> int:
    """round-half-up(num / den) for num >= 0, den > 0, in pure integer arithmetic."""
    return (2 * num + den) // (2 * den)


def parse_hhmm(s: str) -> Time:
    h, m = s.split(":")
    return Time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in; for overnight shifts a punch-in earlier than shift_end
    belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def _ist_instant(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    d = Date.fromisoformat(date_str) + timedelta(days=plus_days)
    return datetime.combine(d, parse_hhmm(hhmm), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor minutes since shift_start."""
    secs = int((punch_in - _ist_instant(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes from shift_end to punch_out, only if >= 30. Overnight shifts end the next day."""
    end = _ist_instant(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= MIN_OVERTIME_MINUTES else 0


def work_hours_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    """R4 in hundredths of an hour, rounded half-up: (seconds / 3600) * 100 = seconds * 100 / 3600."""
    secs = int((punch_out - punch_in).total_seconds())
    return half_up_div(secs * 100, 3600)


def derive(emp: dict, date_str: str, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All derived fields for a record, from the final status / times (see DATA_MODEL.md table)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hundredths = work_hours_hundredths(punch_in, punch_out)
    return {
        "work_hours": hundredths / 100,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"]),
        "half_day": hundredths < HALF_DAY_HUNDREDTHS,
    }


# --- calendar helpers ------------------------------------------------------ #
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def parse_date_strict(s: str) -> Date:
    """'YYYY-MM-DD' only (Python's fromisoformat alone would also accept '20260706')."""
    if not _DATE_RE.fullmatch(s):
        raise ValueError("date must be YYYY-MM-DD")
    return Date.fromisoformat(s)


def unprocessable(msg: str, loc: str = "body") -> HTTPException:
    """422 with the same body shape FastAPI uses for its own validation errors."""
    return HTTPException(422, [{"loc": [loc], "msg": msg, "type": "value_error"}])


def date_or_422(s: str, loc: str) -> Date:
    try:
        return parse_date_strict(s)
    except ValueError:
        raise unprocessable(f"{loc} must be a valid YYYY-MM-DD date", loc)


def month_bounds(month: str) -> tuple[str, str, Date, Date]:
    if not _MONTH_RE.fullmatch(month or ""):
        raise unprocessable("month must be YYYY-MM", "month")
    y, m = int(month[:4]), int(month[5:])
    if y < 1:
        raise unprocessable("month must be YYYY-MM", "month")
    first, last = Date(y, m, 1), Date(y, m, calendar.monthrange(y, m)[1])
    return first.isoformat(), last.isoformat(), first, last


def count_weekdays(start: Date, end: Date) -> int:
    """R7: Monday-Friday days in [start, end] (no holiday calendar)."""
    if end < start:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Models and serialisers
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# strict=True: JSON floats ("1.5", 1783312500000.0), strings and booleans are rejected with 422.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=_HHMM)
    shift_end: str = Field("18:30", pattern=_HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        parse_date_strict(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # derived fields (work_hours, ...) are not declared, so anything a client sends for them is ignored
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": ms_or_none(doc.get("created_at")),
    }


def _history_out(entries: Optional[list]) -> list:
    out = []
    for h in entries or []:
        changes = {}
        for field, ch in (h.get("changes") or {}).items():
            conv = ms_or_none if field in ("punch_in", "punch_out") else (lambda v: v)
            changes[field] = {"from": conv(ch.get("from")), "to": conv(ch.get("to"))}
        out.append({"at": ms_or_none(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return out


def record_out(doc: dict) -> dict:
    """Stored document -> API record. Legacy documents may lack late/overtime/half_day/history."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": _history_out(doc.get("history")),
    }


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    ts = ts.replace(microsecond=0)  # R1: truncate to whole seconds, store the truncated value
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): of N racing requests exactly one succeeds
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = (ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()).replace(microsecond=0)
    # most recent record whose punch_in <= punched_at (may belong to yesterday for overnight shifts)
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin:
        raise unprocessable("punched_at must be after punch_in", "punched_at")
    if ts - pin > MAX_SHIFT:
        raise unprocessable("punched_at is more than 24 hours after punch_in", "punched_at")
    derived = derive(emp, rec["date"], rec["status"], pin, ts)
    # Conditional update: only succeeds while the record is still open and unchanged. Of two
    # simultaneous punch-outs exactly one matches; the other gets None -> 409.
    updated = logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_in": pin, "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


ATT_SORT = [("date", -1), ("emp_code", 1)]


def attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]) -> dict:
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    d_from = date_or_422(date_from, "date_from") if date_from else None
    d_to = date_or_422(date_to, "date_to") if date_to else None
    if d_from and d_to and d_from > d_to:
        raise unprocessable("date_from must not be after date_to", "date_from")
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = date_from
        if d_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(q)
    cursor = logs.find(q).sort(ATT_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    date_or_422(date, "date")
    emp = get_employee(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    sent_in = ms_to_dt(body.punch_in).replace(microsecond=0) if body.punch_in is not None else None
    sent_out = ms_to_dt(body.punch_out).replace(microsecond=0) if body.punch_out is not None else None

    if new_status in ABSENCE:
        if sent_in or sent_out:
            raise unprocessable(f"{new_status} clears punch times; punch_in/punch_out cannot be supplied")
        new_in = new_out = None
    else:
        new_in = sent_in or old_in
        new_out = sent_out or old_out
        if new_in is None:
            raise unprocessable("a presence status requires a punch_in", "punch_in")
        if sent_in and attendance_date(sent_in, emp["shift_start"], emp["shift_end"]) != date:
            raise unprocessable("punch_in must stay on the record's attendance date", "punch_in")
        if new_out is not None:
            if new_out <= new_in:
                raise unprocessable("punch_out must be after punch_in", "punch_out")
            if new_out - new_in > MAX_SHIFT:
                raise unprocessable("punch_out must be within 24 hours of punch_in", "punch_out")

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise unprocessable("the request changes nothing")

    new_derived = derive(emp, date, new_status, new_in, new_out)
    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **new_derived}
    old_vals = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record is exactly as we read it
    # (including the length of its history). A concurrent correction makes this match nothing
    # -> 409, so no history entry is ever silently overwritten or lost.
    guard: dict[str, Any] = {"emp_code": emp_code, "date": date, "status": old_status, "punch_in": old_in, "punch_out": old_out}
    hist = rec.get("history")
    guard["history"] = {"$size": len(hist)} if isinstance(hist, list) else {"$exists": False}
    updated = logs.find_one_and_update(
        guard,
        {"$set": new_vals, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 5. Aggregation pipelines and analytics endpoints
# --------------------------------------------------------------------------- #
def half_up_expr(num: Any, den: Any) -> dict:
    """Aggregation expression for round-half-up(num / den), num >= 0 integer-valued, den > 0.
    Same formula as half_up_div(). Exact because integer-valued doubles divide exactly."""
    return {"$floor": {"$divide": [{"$add": [{"$multiply": [2, num]}, den]}, {"$multiply": [2, den]}]}}


_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}  # R6
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # 1=Sun .. 7=Sat
_IS_WEEKDAY = {"$and": [{"$gte": [_DOW, 2]}, {"$lte": [_DOW, 6]}]}  # R7
_LATE = {"$ifNull": ["$late_minutes", 0]}  # stored values are authoritative; missing means 0
_OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
_HALF_UNITS = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}  # half day = 0.5 -> 1 of 2 units
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def metric_accumulators() -> dict:
    """$group accumulators shared by the monthly and the department summaries.
    p2 = present days counted in half-day units (so everything stays an exact integer)."""
    return {
        "p2": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _HALF_UNITS, 0]}},
        "leave": _count_if({"$eq": ["$status", "LEAVE"]}),
        "on_duty": _count_if({"$eq": ["$status", "ON_DUTY"]}),
        "late_count": _count_if({"$gt": [_LATE, 0]}),
        "late_sum": {"$sum": _LATE},
        "ot_sum": {"$sum": _OVERTIME},
        # work hours in hundredths (each stored value has 2 decimals, so x100 is an exact integer)
        "wh_cents": {"$sum": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
        "wh_n": _count_if(_HAS_HOURS),
    }


def build_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None, **metric_accumulators()}},
    ]


def build_department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    emp_match: dict[str, Any] = {"joined_on": {"$lte": last}}  # R9
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        # per employee: one summary document of that employee's logs for the month (uses uniq_emp_date)
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": None, **metric_accumulators()}},
                ],
                "as": "s",
            }
        },
        # preserveNullAndEmptyArrays keeps employees with zero logs, so they still count in headcount
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{k: {"$sum": {"$ifNull": [f"$s.{k}", 0]}} for k in ("p2", "leave", "on_duty", "late_count", "late_sum", "wh_cents", "wh_n")},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$divide": ["$p2", 2]},
                # mean over records (sum of all hours / number of records), half-up to 2 decimals
                "avg_work_hours": {
                    "$cond": [{"$gt": ["$wh_n", 0]}, {"$divide": [half_up_expr("$wh_cents", "$wh_n"), 100]}, None]
                },
                "late_count": 1,
                "total_late_minutes": "$late_sum",
                "leave_count": "$leave",
                "on_duty_count": "$on_duty",
            }
        },
    ]


def build_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1,2,2,4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def build_trend_pipeline(codes: list, department: str, d_from: Date, d_to: Date) -> list:
    n_days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    fmt = "%Y-%m-%d"
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {"_id": "$date", "p2": {"$sum": {"$cond": [_IS_PRESENT, _HALF_UNITS, 0]}}, "late": _count_if({"$gt": [_LATE, 0]})}},
        # Gap filling: a zero row for every calendar day of the range, generated inside the database
        # (the sub-pipeline starts from the department's employees so it is index backed).
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "d": {
                                "$map": {
                                    "input": {"$range": [0, n_days]},
                                    "as": "i",
                                    "in": {
                                        "$dateToString": {
                                            "date": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}},
                                            "format": fmt,
                                        }
                                    },
                                }
                            },
                        }
                    },
                    {"$unwind": "$d"},
                    {"$project": {"_id": "$d", "p2": {"$literal": 0}, "late": {"$literal": 0}}},
                ],
            }
        },
        {"$group": {"_id": "$_id", "p2": {"$sum": "$p2"}, "late": {"$sum": "$late"}}},
        {"$addFields": {"date": "$_id", "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$_id", "format": fmt}}}}},
        # R9: headcount that day = department employees with joined_on <= day
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                "is_wd": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            }
        },
        # rate in 1/10000 units, half-up: round(10000 * (p2/2) / headcount) = round(5000 * p2 / headcount)
        {
            "$addFields": {
                "rate_units": {
                    "$cond": [
                        {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
                        half_up_expr({"$multiply": [5000, "$p2"]}, "$headcount"),
                        None,
                    ]
                }
            }
        },
        # moving average over the current row and up to 6 preceding rows *inside the range*
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "w_sum": {"$sum": "$rate_units", "window": {"documents": [-6, 0]}},
                    "w_n": {"$sum": {"$cond": [{"$isNumber": "$rate_units"}, 1, 0]}, "window": {"documents": [-6, 0]}},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": "$is_wd",
                "headcount": 1,
                "present_count": {"$divide": ["$p2", 2]},
                "late_count": "$late",
                "attendance_rate": {"$cond": [{"$isNumber": "$rate_units"}, {"$divide": ["$rate_units", 10000]}, None]},
                "moving_avg_7d": {
                    "$cond": [{"$gt": ["$w_n", 0]}, {"$divide": [half_up_expr("$w_sum", "$w_n"), 10000]}, None]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def run_pipeline(collection, pipeline: list) -> list:
    return list(collection.aggregate(pipeline, allowDiskUse=True))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")):
    first_s, last_s, first, last = month_bounds(month)
    emp = get_employee(emp_code)
    rows = run_pipeline(logs, build_monthly_pipeline(emp_code, first_s, last_s))
    r = rows[0] if rows else {}
    p2 = int(r.get("p2", 0))
    # R7: working days are counted from joined_on for mid-month joiners
    working_days = count_weekdays(max(first, Date.fromisoformat(emp["joined_on"])), last)
    # attendance_pct = (p2 / 2) / wd * 100; in hundredths of a percent that is 5000 * p2 / wd (half-up)
    pct = None if working_days == 0 else half_up_div(5000 * p2, working_days) / 100
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": p2 / 2,
        "leave_days": int(r.get("leave", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("late_sum", 0)),
        "total_overtime_minutes": int(r.get("ot_sum", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    first_s, last_s, _, _ = month_bounds(month)
    items = run_pipeline(employees, build_department_summary_pipeline(first_s, last_s, department))
    for it in items:
        it["late_count"] = int(it["late_count"])
        it["total_late_minutes"] = int(it["total_late_minutes"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_s, last_s, _, _ = month_bounds(month)
    items = run_pipeline(logs, build_leaderboard_pipeline(first_s, last_s, limit, department))
    for it in items:
        it["rank"] = int(it["rank"])
        it["total_late_minutes"] = int(it["total_late_minutes"])
    return {"month": month, "items": items}


def trend_range(from_: str, to_: str) -> tuple[Date, Date]:
    d_from, d_to = date_or_422(from_, "from"), date_or_422(to_, "to")
    if d_to < d_from:
        raise unprocessable("to must not be before from", "to")
    if (d_to - d_from).days + 1 > 92:
        raise unprocessable("range must not exceed 92 days", "to")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to_: str = Query(..., alias="to")):
    d_from, d_to = trend_range(from_, to_)
    codes = employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "unknown department")
    items = run_pipeline(logs, build_trend_pipeline(codes, department, d_from, d_to))
    for it in items:
        it["headcount"] = int(it["headcount"])
        it["late_count"] = int(it["late_count"])
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 6. Explain endpoint: same builders, same arguments as the real endpoints
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _required(value: Any, name: str) -> Any:
    if value is None or value == "":
        raise unprocessable(f"query parameter '{name}' is required for this endpoint", name)
    return value


def _explain(command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # explain output contains BSON types (dates, timestamps, ...): round-trip through extended JSON
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to_: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        cmd = {
            "find": logs.name,
            "filter": q,
            "sort": dict(ATT_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = logs.name
    elif endpoint == "employee_monthly":
        _required(emp_code, "emp_code")
        first_s, last_s, _, _ = month_bounds(_required(month, "month"))
        pipeline, collection = build_monthly_pipeline(emp_code, first_s, last_s), logs.name
    elif endpoint == "department_summary":
        first_s, last_s, _, _ = month_bounds(_required(month, "month"))
        pipeline, collection = build_department_summary_pipeline(first_s, last_s, department), employees.name
    elif endpoint == "late_leaderboard":
        first_s, last_s, _, _ = month_bounds(_required(month, "month"))
        pipeline, collection = build_leaderboard_pipeline(first_s, last_s, limit, department), logs.name
    else:  # department_trend
        _required(department, "department")
        d_from, d_to = trend_range(_required(from_, "from"), _required(to_, "to"))
        codes = employees.distinct("emp_code", {"department": department})
        pipeline, collection = build_trend_pipeline(codes, department, d_from, d_to), logs.name

    if endpoint != "attendance_list":
        cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
    return {"endpoint": endpoint, "collection": collection, "explain": _explain(cmd)}
