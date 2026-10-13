"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
from __future__ import annotations

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# Environment connection config
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create all required indexes at startup idempotently
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("date", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def truncate_ms(ms: int) -> int:
    """R1: Instants are truncated to whole seconds."""
    return (ms // 1000) * 1000


def ms_to_datetime(ms: int) -> datetime:
    """Converts epoch ms to timezone-aware UTC datetime with whole-second truncation."""
    ms_trunc = truncate_ms(ms)
    return datetime.fromtimestamp(ms_trunc / 1000, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(value: float | Decimal, decimals: int = 2) -> float:
    """R8: Standard half-up rounding."""
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    d_val = Decimal(str(value))
    return float(d_val.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date in IST.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous calendar day.
    """
    dt_ist = ms_to_datetime(punch_in_ms).astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        time_str = dt_ist.strftime("%H:%M")
        if time_str < shift_end:
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(attendance_date: str, shift_start: str, punch_in_ms: int) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start (> 600s);
    then whole minutes (floor) since shift_start.
    """
    sh_h, sh_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(attendance_date).replace(
        hour=sh_h, minute=sh_m, second=0, microsecond=0, tzinfo=IST
    )
    punch_in_ist = ms_to_datetime(punch_in_ms).astimezone(IST)
    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (truncate_ms(punch_out_ms) - truncate_ms(punch_in_ms)) // 1000
    val = Decimal(diff_sec) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    attendance_date: str, shift_start: str, shift_end: str, punch_out_ms: int
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end falls on next calendar day (attendance_date + 1).
    """
    end_h, end_m = map(int, shift_end.split(":"))
    base_date = datetime.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        base_date += timedelta(days=1)
    shift_end_dt = base_date.replace(hour=end_h, minute=end_m, second=0, microsecond=0, tzinfo=IST)
    punch_out_ist = ms_to_datetime(punch_out_ms).astimezone(IST)
    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    mins = int(diff_seconds // 60)
    return mins if mins >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when rounded work_hours < 4.50."""
    return bool(work_hours is not None and work_hours < 4.50)


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes_api = {}
    for k, v in entry.get("changes", {}).items():
        if k in ("punch_in", "punch_out"):
            changes_api[k] = {
                "from": datetime_to_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
                "to": datetime_to_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to"),
            }
        else:
            changes_api[k] = {"from": v.get("from"), "to": v.get("to")}
    return {
        "at": datetime_to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes_api,
    }


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_api = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_api,
    }


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("invalid joined_on date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unreachable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_at_ms = body.punched_at if body.punched_at is not None else now_ms
    punched_at_ms = truncate_ms(punched_at_ms)

    att_date = get_attendance_date(punched_at_ms, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(att_date, emp["shift_start"], punched_at_ms)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ms_to_datetime(punched_at_ms),
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
        raise HTTPException(
            status_code=409, detail="Already punched in for this date"
        )
    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_at_ms = body.punched_at if body.punched_at is not None else now_ms
    punched_at_ms = truncate_ms(punched_at_ms)
    punch_out_dt = ms_to_datetime(punched_at_ms)

    # Find employee's most recent presence record
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
            "punch_in": {"$ne": None},
        },
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409, detail="Record is already punched out"
        )

    punch_in_ms = datetime_to_ms(record["punch_in"])
    assert punch_in_ms is not None

    # Contract: 422 if punched_at <= punch_in or more than 24 hours after it
    if punched_at_ms <= punch_in_ms or punched_at_ms > punch_in_ms + 24 * 3600 * 1000:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_ms, punched_at_ms)
    overtime = compute_overtime(
        record["date"], emp["shift_start"], emp["shift_end"], punched_at_ms
    )
    half_day = compute_half_day(work_hours)

    # Concurrency-safe atomic update
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=409, detail="Record is already punched out"
        )
    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be greater than date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=404, detail="Attendance record not found"
        )

    # Status ABSENT or LEAVE clears both punch times: supplying punch_in or punch_out is 422
    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="Setting status ABSENT or LEAVE cannot include punch times",
        )

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in_ms = None
        target_punch_out_ms = None
    else:
        target_punch_in_ms = (
            truncate_ms(body.punch_in)
            if body.punch_in is not None
            else datetime_to_ms(record.get("punch_in"))
        )
        target_punch_out_ms = (
            truncate_ms(body.punch_out)
            if body.punch_out is not None
            else datetime_to_ms(record.get("punch_out"))
        )

        # Presence status requires punch_in
        if target_punch_in_ms is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in",
            )

        # punch_in must stay on record's attendance date
        computed_date = get_attendance_date(
            target_punch_in_ms, emp["shift_start"], emp["shift_end"]
        )
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must stay on the record's attendance date",
            )

        # punch_out must be after punch_in and within 24 hours
        if target_punch_out_ms is not None:
            if (
                target_punch_out_ms <= target_punch_in_ms
                or target_punch_out_ms > target_punch_in_ms + 24 * 3600 * 1000
            ):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    # Compute new derived values
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(date, emp["shift_start"], target_punch_in_ms)
        if target_punch_out_ms is not None:
            new_work_hours = compute_work_hours(target_punch_in_ms, target_punch_out_ms)
            new_overtime_minutes = compute_overtime(
                date, emp["shift_start"], emp["shift_end"], target_punch_out_ms
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Calculate actual changes
    old_punch_in_dt = record.get("punch_in")
    old_punch_out_dt = record.get("punch_out")
    new_punch_in_dt = (
        ms_to_datetime(target_punch_in_ms) if target_punch_in_ms is not None else None
    )
    new_punch_out_dt = (
        ms_to_datetime(target_punch_out_ms) if target_punch_out_ms is not None else None
    )

    changes_db: Dict[str, Any] = {}

    if record["status"] != target_status:
        changes_db["status"] = {"from": record["status"], "to": target_status}
    if old_punch_in_dt != new_punch_in_dt:
        changes_db["punch_in"] = {"from": old_punch_in_dt, "to": new_punch_in_dt}
    if old_punch_out_dt != new_punch_out_dt:
        changes_db["punch_out"] = {"from": old_punch_out_dt, "to": new_punch_out_dt}
    if record.get("work_hours") != new_work_hours:
        changes_db["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if record.get("late_minutes", 0) != new_late_minutes:
        changes_db["late_minutes"] = {
            "from": record.get("late_minutes", 0),
            "to": new_late_minutes,
        }
    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        changes_db["overtime_minutes"] = {
            "from": record.get("overtime_minutes", 0),
            "to": new_overtime_minutes,
        }
    if record.get("half_day", False) != new_half_day:
        changes_db["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    # Contract: A request that changes nothing is 422
    if not changes_db:
        raise HTTPException(
            status_code=422, detail="No changes detected"
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry_db = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Optimistic concurrency: ensure history hasn't been appended concurrently
    existing_history = record.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": existing_history,
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry_db},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict during regularization",
        )
    return serialize_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    start_of_month = f"{month}-01"
    end_of_month = f"{month}-{num_days:02d}"

    # Calculate working days (Mon-Fri, from joined_on if joined mid-month)
    joined_date = date.fromisoformat(emp["joined_on"])
    month_start_date = date(year, m_num, 1)
    month_end_date = date(year, m_num, num_days)

    effective_start = max(month_start_date, joined_date)
    working_days = 0
    if effective_start <= month_end_date:
        cur = effective_start
        while cur <= month_end_date:
            if cur.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_of_month, "$lte": end_of_month},
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
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
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
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            "$late_minutes",
                            0,
                        ]
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$overtime_minutes", 0]},
                            "$overtime_minutes",
                            0,
                        ]
                    }
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        stats = agg_res[0]
        present_days = float(stats["present_days"])
        leave_days = int(stats["leave_days"])
        late_count = int(stats["late_count"])
        total_late = int(stats["total_late_minutes"])
        total_ot = int(stats["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    start_of_month = f"{month}-01"
    end_of_month = f"{month}-{num_days:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_of_month}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
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
                                    {"$gte": ["$date", start_of_month]},
                                    {"$lte": ["$date", end_of_month]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$emp_code",
                "department": {"$first": "$department"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs", None]},
                            0.0,
                            {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$logs.date"
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
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
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]},
                                ]
                            },
                            "$logs.late_minutes",
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "total_work_hours": {"$sum": "$work_hours_sum"},
                "total_work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$round": ["$present_days", 2]},
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$total_work_hours_count", 0]},
                        {
                            "$round": [
                                {"$divide": ["$total_work_hours", "$total_work_hours_count"]},
                                2,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    start_of_month = f"{month}-01"
    end_of_month = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_of_month, "$lte": end_of_month},
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

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format"
        )

    if d_to < d_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must not be earlier than 'from' date",
        )

    if (d_to - d_from).days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range exceeds maximum allowed limit of 92 days",
        )

    # 404 if department has no employees
    if not db.employees.find_one({"department": department}):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Department not found"
        )

    from_dt = datetime.combine(d_from, datetime.min.time())
    to_dt = datetime.combine(d_to + timedelta(days=1), datetime.min.time())

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
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
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "d": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }
        },
        {"$sort": {"d": 1}},
        {
            "$project": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "d": 1,
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d_str": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d_str"]},
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
            "$set": {
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        {"$round": ["$moving_avg_7d_raw", 4]},
                        None,
                    ]
                },
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    if endpoint not in (
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown endpoint to explain"
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from must not be greater than date_to",
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and valid month (YYYY-MM) are required",
            )
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        start_of_month = f"{month}-01"
        end_of_month = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_of_month, "$lte": end_of_month},
                }
            },
            {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}},
        ]
        res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    elif endpoint == "department_summary":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Valid month (YYYY-MM) is required",
            )
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        start_of_month = f"{month}-01"
        end_of_month = f"{month}-{num_days:02d}"

        emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_of_month}}
        if department:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
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
                                        {"$gte": ["$date", start_of_month]},
                                        {"$lte": ["$date", end_of_month]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_res}

    elif endpoint == "late_leaderboard":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Valid month (YYYY-MM) is required",
            )
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        start_of_month = f"{month}-01"
        end_of_month = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_of_month, "$lte": end_of_month},
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
        ]
        res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    elif endpoint == "department_trend":
        if (
            not department
            or not from_date
            or not to_date
            or not re.match(r"^\d{4}-\d{2}-\d{2}$", from_date)
            or not re.match(r"^\d{4}-\d{2}-\d{2}$", to_date)
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from and to (YYYY-MM-DD) are required",
            )
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format"
            )
        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date range"
            )

        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown endpoint")
