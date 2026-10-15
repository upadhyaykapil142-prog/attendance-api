"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience, real env vars win)
"""

import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from bson.json_util import dumps
from pymongo.errors import DuplicateKeyError

load_dotenv()  # override=False by default: real environment variables win

# tz_aware=True: Mongo datetimes come back with UTC tzinfo (no naive datetimes)
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Indexes created at startup, idempotent
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]
    )

    yield



app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_ist(dt):
    if dt.tzinfo is None:                      # naive datetime from Mongo is UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).replace(microsecond=0)


def to_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms):
    # truncated to whole seconds (R1)
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def attendance_date(ts, shift_start, shift_end):
    """R1: IST date of punch-in; overnight shift punched in before shift_end belongs to previous day."""
    p = to_ist(ts)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    d = p.date()
    if (eh, em) <= (sh, sm) and (p.hour, p.minute, p.second) < (eh, em, 0):
        d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in, shift_start, date_str=None):
    p = to_ist(punch_in)
    h, m = map(int, shift_start.split(":"))
    if date_str:
        start = datetime.fromisoformat(date_str).replace(tzinfo=IST, hour=h, minute=m)
    else:
        start = p.replace(hour=h, minute=m, second=0)
    seconds = int((p - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    secs = int((to_ist(punch_out) - to_ist(punch_in)).total_seconds())
    hours = Decimal(secs) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, shift_end, date_str, shift_start=None):
    po = to_ist(punch_out)
    eh, em = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(tzinfo=IST, hour=eh, minute=em)
    if shift_start:
        sh, sm = map(int, shift_start.split(":"))
        if (eh, em) <= (sh, sm):               # overnight shift: end is next day
            end += timedelta(days=1)
    minutes = int((po - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def serialize_employee(doc):
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = to_ms(out.get("created_at"))
    return out


def serialize_attendance(doc):
    """Mongo document -> API shape (no _id, epoch ms, defaults for legacy records)."""
    history = []
    for h in doc.get("history") or []:
        changes = {}
        for field, ch in (h.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                ch = {"from": to_ms(ch.get("from")), "to": to_ms(ch.get("to"))}
            changes[field] = ch
        history.append({"at": to_ms(h.get("at")), "by": h.get("by"),
                        "reason": h.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000)]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_date(cls, v):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)          # unique index decides, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = (db.employees.find(q).sort("emp_code", ASCENDING)
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = from_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)    # unique (emp_code, date) index decides the race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = (db.attendance_logs.find(q)
              .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


# TODO next steps: punch-out, PATCH, analytics, explain


# --------------------------------------------------------------------------- #
# Step 3: punch-out
# --------------------------------------------------------------------------- #
from pymongo import ReturnDocument


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = from_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    # most recent record whose punch_in is at or before ts (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pin = rec["punch_in"]
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    hours = compute_work_hours(pin, ts)
    changes = {
        "punch_out": ts,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(ts, emp["shift_end"], rec["date"], emp["shift_start"]),
        "half_day": hours < 4.5,
    }
    # filter has punch_out None: only one of two simultaneous requests can match
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Step 4: PATCH /attendance/{emp_code}/{date}  (regularize)
# --------------------------------------------------------------------------- #
class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: date, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    d = day.isoformat()
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": d})
    if not rec:
        raise HTTPException(404, "no record for that date")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }

    new_status = body.status or old["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in, new_out = None, None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != d:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # recompute derived fields from the final values (R2-R5)
    if new_status in ("ABSENT", "LEAVE"):
        new = {"status": new_status, "punch_in": None, "punch_out": None, "work_hours": None,
               "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        late = compute_late_minutes(new_in, emp["shift_start"], d)
        if new_out is not None:
            hours = compute_work_hours(new_in, new_out)
            ot = compute_overtime(new_out, emp["shift_end"], d, emp["shift_start"])
            half = hours < 4.5
        else:
            hours, ot, half = None, 0, False
        new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": hours,
               "late_minutes": late, "overtime_minutes": ot, "half_day": half}

    changes = {}
    for field, new_val in new.items():
        old_val = old[field]
        if field in ("punch_in", "punch_out"):
            same = to_ms(old_val) == to_ms(new_val)
        else:
            same = old_val == new_val
        if not same:
            changes[field] = {"from": old_val, "to": new_val}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": datetime.now(timezone.utc).replace(microsecond=0),
             "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # optimistic concurrency: only apply if the record is still exactly as we read it.
    old_history = rec.get("history")
    flt = {"_id": rec["_id"], "status": old["status"],
           "punch_in": old["punch_in"], "punch_out": old["punch_out"]}
    flt["history"] = {"$size": len(old_history)} if old_history is not None else {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        flt, {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Step 5a: GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
import calendar

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]


def half_up(value, places=2):
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def month_bounds(month):
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def count_working_days(first, last, joined_on):
    """R7: Mon-Fri only, counted from joined_on for mid-month joiners."""
    start = max(first, date.fromisoformat(joined_on))
    n, cur = 0, start
    while cur <= last:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def employee_monthly_pipeline(emp_code, month):
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code,
                    "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$addFields": {
            "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
            "_half": {"$ifNull": ["$half_day", False]},
            "_late": {"$ifNull": ["$late_minutes", 0]},
            "_ot": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$group": {
            "_id": None,
            # present on Mon-Fri only (dayOfWeek: 1=Sunday ... 7=Saturday); half day = 0.5
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE]}, {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
                {"$cond": ["$_half", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late"},
            "total_overtime_minutes": {"$sum": "$_ot"},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    working_days = count_working_days(first, last, emp["joined_on"])
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    present = r.get("present_days", 0)
    pct = half_up(Decimal(str(present)) / Decimal(working_days) * 100) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present),
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# ---------------------------------------------------------------------------
# Step 5b: GET /analytics/departments/summary
# ---------------------------------------------------------------------------

def department_summary_pipeline(month: str, department: Optional[str] = None):
    first, last = month_bounds(month)

    employee_match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", first.isoformat()]},
                                    {"$lte": ["$date", last.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$set": {
                "dow": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$logs.date"
                                }
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employee_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE]},
                                    {"$gte": ["$dow", 2]},
                                    {"$lte": ["$dow", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$logs.half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$logs.late_minutes", 0]},
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
                "hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE]},
                                    {"$ne": ["$logs.work_hours", None]},
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
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employee_codes"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$hours_count", 0]},
                        {"$divide": ["$hours_sum", "$hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    pipeline = department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for row in rows:
        row["present_days"] = half_up(row["present_days"])

        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = half_up(row["avg_work_hours"])

        items.append(row)

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Step 5c: GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------

def late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    first, last = month_bounds(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat(),
                },
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
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Step 6: GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------

def department_trend_pipeline(department: str, first: date, last: date):
    day_count = (last - first).days + 1

    return [
        # Find employees belonging to this department.
        {"$match": {"department": department}},

        # Keep employee information together so we can calculate
        # the correct headcount for each calendar day.
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },

        {
            "$project": {
                "_id": 0,
                "employees": 1,
                "days": {"$range": [0, day_count]},
                "start": {
                    "$dateFromString": {
                        "dateString": first.isoformat(),
                        "timezone": "UTC",
                    }
                },
            }
        },

        # Generate one row for every calendar day, including weekends.
        {"$unwind": "$days"},

        {
            "$set": {
                "date_obj": {
                    "$dateAdd": {
                        "startDate": "$start",
                        "unit": "day",
                        "amount": "$days",
                    }
                }
            }
        },

        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_obj",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$isoDayOfWeek": "$date_obj"}, 1]},
                        {"$lte": [{"$isoDayOfWeek": "$date_obj"}, 5]},
                    ]
                },
            }
        },

        # An employee counts toward headcount only from their join date.
        {
            "$set": {
                "active_employees": {
                    "$filter": {
                        "input": "$employees",
                        "as": "employee",
                        "cond": {
                            "$lte": [
                                {
                                    "$ifNull": [
                                        "$$employee.joined_on",
                                        "9999-12-31",
                                    ]
                                },
                                "$date",
                            ]
                        },
                    }
                }
            }
        },

        {
            "$set": {
                "active_codes": {
                    "$map": {
                        "input": "$active_employees",
                        "as": "employee",
                        "in": "$$employee.code",
                    }
                },
                "headcount": {"$size": "$active_employees"},
            }
        },

        # Fetch attendance only for this department's active employees
        # on the current date.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "codes": "$active_codes",
                    "day": "$date",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "emp_code": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },

        # Presence statuses use the same PRESENCE constant as the
        # existing monthly and department-summary analytics.
        # A half-day presence contributes 0.5.
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", PRESENCE]},
                                    {
                                        "$cond": [
                                            {
                                                "$ifNull": [
                                                    "$$log.half_day",
                                                    False,
                                                ]
                                            },
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {
                                        "$ifNull": [
                                            "$$log.late_minutes",
                                            0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },

        # Round non-negative values to 4 decimal places using
        # half-up rounding: floor(value * 10000 + 0.5) / 10000.
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_count",
                                                            "$headcount",
                                                        ]
                                                    },
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },

        # Average the current row and up to the previous six rows.
        # Null attendance rates are ignored by MongoDB's $avg.
        {
            "$setWindowFields": {
                "sortBy": {"date_obj": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },

        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_7d_raw",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
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


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(
            status_code=404,
            detail="department not found",
        )

    pipeline = department_trend_pipeline(
        department,
        from_date,
        to_date,
    )

    # All daily calculations and moving averages run in MongoDB.
    items = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Step 7: GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------

EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


def run_mongodb_explain(
    collection_name: str,
    pipeline=None,
    query=None,
    sort=None,
    skip=None,
    limit=None,
):
    """
    Return MongoDB's real executionStats explain output.
    Uses the same filters, sorting, pagination, or aggregation pipeline
    as the corresponding endpoint.
    """
    if pipeline is not None:
        command = {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
    else:
        find_command = {
            "find": collection_name,
            "filter": query or {},
            "sort": sort or {},
        }

        if skip is not None:
            find_command["skip"] = skip
        if limit is not None:
            find_command["limit"] = limit

        command = {
            "explain": find_command,
            "verbosity": "executionStats",
        }

    return db.command(command)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must be <= date_to",
            )

        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from.isoformat()
            if date_to:
                query["date"]["$lte"] = date_to.isoformat()

        if status:
            query["status"] = status

        explain = run_mongodb_explain(
            "attendance_logs",
            query=query,
            sort={"date": -1, "emp_code": 1},
            skip=(page - 1) * page_size,
            limit=page_size,
        )
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly",
            )

        if not db.employees.find_one({"emp_code": emp_code}):
            raise HTTPException(status_code=404, detail="employee not found")

        pipeline = employee_monthly_pipeline(emp_code, month)
        explain = run_mongodb_explain(
            "attendance_logs",
            pipeline=pipeline,
        )
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary",
            )

        pipeline = department_summary_pipeline(month, department)
        explain = run_mongodb_explain(
            "employees",
            pipeline=pipeline,
        )
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )

        pipeline = late_leaderboard_pipeline(
            month=month,
            limit=limit,
            department=department,
        )
        explain = run_mongodb_explain(
            "attendance_logs",
            pipeline=pipeline,
        )
        collection = "attendance_logs"

    else:
        if not department or from_date is None or to_date is None:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required for department_trend",
            )

        if to_date < from_date:
            raise HTTPException(
                status_code=422,
                detail="'to' must be on or after 'from'",
            )

        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="date range must not exceed 92 days",
            )

        if not db.employees.find_one({"department": department}):
            raise HTTPException(
                status_code=404,
                detail="department not found",
            )

        pipeline = department_trend_pipeline(
            department,
            from_date,
            to_date,
        )
        explain = run_mongodb_explain(
            "employees",
            pipeline=pipeline,
        )
        collection = "employees"

    # Convert BSON values to Extended JSON-compatible Python values.
    # IMPORTANT: use standard json.loads, NOT bson.json_util.loads.
    response = {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain,
    }
    return json.loads(dumps(response))
