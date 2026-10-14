"""Employee Attendance & Analytics API."""
from __future__ import annotations
import json
from bson import json_util
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Body
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
load_dotenv()
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
MONGO_DB = os.getenv('MONGO_DB', 'attendance_db')
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ('PRESENT', 'WFH', 'ON_DUTY')
STATUSES = (*PRESENCE, 'ABSENT', 'LEAVE')
app = FastAPI(title='Employee Attendance & Analytics API', version='2.0.0')

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)

def to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0, tzinfo=None)

def epoch_ms_to_utc(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, 'Timestamp must be an integer in milliseconds')
    if value < 100000000000 or value > 4102444800000:
        raise HTTPException(422, 'Timestamp is outside the supported range')
    try:
        return datetime.fromtimestamp(value // 1000, UTC)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, 'Invalid timestamp')

def utc_to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)

def parse_date(value: str, field: str='date') -> date:
    try:
        if not isinstance(value, str) or not re.fullmatch('\\d{4}-\\d{2}-\\d{2}', value):
            raise ValueError
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(422, f'{field} must use YYYY-MM-DD format')

def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch('\\d{4}-\\d{2}', value or ''):
        raise HTTPException(422, 'month must use YYYY-MM format')
    try:
        start = date.fromisoformat(value + '-01')
    except ValueError:
        raise HTTPException(422, 'month must use YYYY-MM format')
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    return (start, next_month - timedelta(days=1))

def is_workday_expression(date_expr: str='$date') -> dict:
    return {'$and': [{'$gte': [{'$dayOfWeek': date_expr}, 2]}, {'$lte': [{'$dayOfWeek': date_expr}, 6]}]}

def shift_datetime(date_str: str, shift_time: str) -> datetime:
    return datetime.combine(parse_date(date_str), time.fromisoformat(shift_time), tzinfo=IST)

def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = utc_to_ist(punch_in)
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < time.fromisoformat(shift_end):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()

def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str]=None) -> int:
    local_punch = utc_to_ist(punch_in)
    attendance_date = date_str or local_punch.date().isoformat()
    start = shift_datetime(attendance_date, shift_start)
    if local_punch <= start + timedelta(minutes=10):
        return 0
    return int((local_punch - start).total_seconds() // 60)

def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    end = shift_datetime(attendance_date, shift_end)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    extra_seconds = (utc_to_ist(punch_out) - end).total_seconds()
    return int(extra_seconds // 60) if extra_seconds >= 1800 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def compute_attendance_values(punch_in, punch_out, status, shift_start, shift_end, date_str):
    if status in ('ABSENT', 'LEAVE'):
        return {'punch_in': None, 'punch_out': None, 'work_hours': None, 'late_minutes': 0, 'overtime_minutes': 0, 'half_day': False}
    if punch_in is None:
        raise HTTPException(422, 'Presence status requires punch_in')
    punch_in = to_utc_naive(punch_in)
    punch_out = to_utc_naive(punch_out) if punch_out else None
    late = compute_late_minutes(punch_in, shift_start, date_str)
    work_hours = None
    overtime = 0
    half_day = False
    if punch_out:
        if punch_out <= punch_in:
            raise HTTPException(422, 'Punch-out must be after punch-in')
        if punch_out - punch_in > timedelta(hours=24):
            raise HTTPException(422, 'Work duration cannot exceed 24 hours')
        work_hours = compute_work_hours(punch_in, punch_out)
        half_day = work_hours < 4.5
        overtime = compute_overtime(punch_out, shift_end, date_str, shift_start)
    return {'punch_in': punch_in, 'punch_out': punch_out, 'work_hours': work_hours, 'late_minutes': late, 'overtime_minutes': overtime, 'half_day': half_day}

def api_value(value):
    if isinstance(value, datetime):
        utc_value = to_utc_naive(value)
        epoch = datetime(1970, 1, 1)
        delta = utc_value - epoch
        seconds = delta.days * 86400 + delta.seconds
        return seconds * 1000
    if isinstance(value, dict):
        return {key: api_value(val) for key, val in value.items() if key != '_id'}
    if isinstance(value, list):
        return [api_value(item) for item in value]
    return value

def clean_doc(doc):
    if not doc:
        return doc
    doc.pop('_id', None)
    return api_value(doc)

def rounded(value: float, places: int=2) -> float:
    return float(Decimal(str(value)).quantize(Decimal('1.' + '0' * places), rounding=ROUND_HALF_UP))

def working_days_count(start: date, end: date) -> int:
    if end < start:
        return 0
    full_weeks, remaining = divmod((end - start).days + 1, 7)
    total = full_weeks * 5
    for offset in range(remaining):
        if (start.weekday() + offset) % 7 < 5:
            total += 1
    return total

def require_valid_status(status: str):
    if status not in STATUSES:
        raise HTTPException(422, 'Invalid attendance status')

class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = '09:30'
    shift_end: str = '18:30'
    joined_on: str

    @field_validator('emp_code')
    @classmethod
    def valid_code(cls, value):
        if not re.fullmatch('EMP\\d{4,6}', value):
            raise ValueError('emp_code must look like EMP1234')
        return value

    @field_validator('name', 'email', 'department')
    @classmethod
    def required_text(cls, value):
        if not value:
            raise ValueError('This field cannot be empty')
        return value

    @field_validator('email')
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch('[^@\\s]+@[^@\\s]+\\.[^@\\s]+', value):
            raise ValueError('Invalid email address')
        return value

    @field_validator('shift_start', 'shift_end')
    @classmethod
    def valid_shift_time(cls, value):
        if not re.fullmatch('(?:[01]\\d|2[0-3]):[0-5]\\d', value):
            raise ValueError('Shift time must use HH:MM format')
        return value

    @field_validator('joined_on')
    @classmethod
    def valid_joined_on(cls, value):
        parse_date(value, 'joined_on')
        return value

    @model_validator(mode='after')
    def shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError('shift_start and shift_end must be different')
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal['PRESENT', 'WFH', 'ON_DUTY'] = 'PRESENT'

    @field_validator('punched_at', mode='before')
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_utc(value)
        return value

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator('punched_at', mode='before')
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_utc(value)
        return value

class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal['PRESENT', 'WFH', 'ON_DUTY', 'ABSENT', 'LEAVE']] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator('reason', 'regularized_by')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('This field cannot be empty')
        return value.strip()

    @field_validator('punch_in', 'punch_out', mode='before')
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_utc(value)
        return value

@app.on_event('startup')
def create_indexes():
    db.employees.create_index([('emp_code', ASCENDING)], unique=True, name='uq_employee_code')
    db.employees.create_index([('department', ASCENDING), ('emp_code', ASCENDING)], name='idx_employee_department_code')
    db.employees.create_index([('department', ASCENDING), ('joined_on', ASCENDING)], name='idx_employee_department_joined')
    db.employees.create_index([('joined_on', ASCENDING)], name='idx_employee_joined_on')
    db.attendance_logs.create_index([('emp_code', ASCENDING), ('date', ASCENDING)], unique=True, name='uq_attendance_employee_date')
    db.attendance_logs.create_index([('date', DESCENDING), ('emp_code', ASCENDING)], name='idx_attendance_date_employee')
    db.attendance_logs.create_index([('status', ASCENDING), ('date', DESCENDING)], name='idx_attendance_status_date')
    db.attendance_logs.create_index([('emp_code', ASCENDING), ('punch_in', DESCENDING)], name='idx_attendance_employee_punchin')

@app.get('/health')
def health():
    try:
        client.admin.command('ping')
        return {'status': 'ok'}
    except PyMongoError:
        raise HTTPException(503, 'Database unavailable')

@app.post('/employees', status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc['created_at'] = to_utc_naive(now_utc())
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'emp_code already exists')
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    return clean_doc(doc)

@app.get('/employees')
def list_employees(department: Optional[str]=None, page: int=Query(1, ge=1), page_size: int=Query(20, ge=1, le=100)):
    query = {'department': department} if department is not None else {}
    try:
        total = db.employees.count_documents(query)
        items = list(db.employees.find(query, {'_id': 0}).sort('emp_code', ASCENDING).skip((page - 1) * page_size).limit(page_size))
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    return {'items': [api_value(item) for item in items], 'total': total, 'page': page, 'page_size': page_size}

@app.post('/attendance/punch-in', status_code=201)
def punch_in(body: PunchInIn):
    try:
        employee = db.employees.find_one({'emp_code': body.emp_code})
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    if not employee:
        raise HTTPException(404, 'Employee not found')
    punched_at = now_utc() if body.punched_at is None else epoch_ms_to_utc(body.punched_at)
    day = attendance_date_for_punch(punched_at, employee['shift_start'], employee['shift_end'])
    values = compute_attendance_values(punched_at, None, body.status, employee['shift_start'], employee['shift_end'], day)
    doc = {'emp_code': body.emp_code, 'date': day, 'status': body.status, **values, 'history': []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'Attendance already exists for this employee and date')
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    return clean_doc(doc)

@app.post('/attendance/punch-out')
def punch_out(body: PunchOutIn):
    try:
        employee = db.employees.find_one({'emp_code': body.emp_code})
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    if not employee:
        raise HTTPException(404, 'Employee not found')
    punched_at = now_utc() if body.punched_at is None else epoch_ms_to_utc(body.punched_at)
    punch_db = to_utc_naive(punched_at)
    try:
        record = db.attendance_logs.find_one({'emp_code': body.emp_code, 'punch_in': {'$ne': None, '$lte': punch_db}}, sort=[('punch_in', DESCENDING)])
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    if not record:
        raise HTTPException(404, 'No matching punch-in found')
    if record.get('punch_out') is not None:
        raise HTTPException(409, 'Attendance is already punched out')
    values = compute_attendance_values(record['punch_in'], punch_db, record['status'], employee['shift_start'], employee['shift_end'], record['date'])
    try:
        result = db.attendance_logs.update_one({'_id': record['_id'], 'punch_out': None}, {'$set': values})
        if result.modified_count != 1:
            raise HTTPException(409, 'Attendance was already updated')
        updated = db.attendance_logs.find_one({'_id': record['_id']}, {'_id': 0})
    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    return api_value(updated)

def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    query = {}
    if emp_code:
        query['emp_code'] = emp_code
    if date_from or date_to:
        query['date'] = {}
        if date_from:
            parse_date(date_from, 'date_from')
            query['date']['$gte'] = date_from
        if date_to:
            parse_date(date_to, 'date_to')
            query['date']['$lte'] = date_to
        if date_from and date_to and (date_from > date_to):
            raise HTTPException(422, 'date_from must be on or before date_to')
    if status:
        require_valid_status(status)
        query['status'] = status
    return query

@app.get('/attendance')
def list_attendance(emp_code: Optional[str]=None, date_from: Optional[str]=None, date_to: Optional[str]=None, status: Optional[str]=None, page: int=Query(1, ge=1), page_size: int=Query(20, ge=1, le=100)):
    query = attendance_query(emp_code, date_from, date_to, status)
    try:
        total = db.attendance_logs.count_documents(query)
        items = list(db.attendance_logs.find(query, {'_id': 0}).sort([('date', DESCENDING), ('emp_code', ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    return {'items': [api_value(item) for item in items], 'total': total, 'page': page, 'page_size': page_size}

@app.patch('/attendance/{emp_code}/{date}')
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn=Body(...)):
    date_str = date
    parse_date(date_str, 'date')
    try:
        employee = db.employees.find_one({'emp_code': emp_code})
        record = db.attendance_logs.find_one({'emp_code': emp_code, 'date': date_str})
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    if not employee or not record:
        raise HTTPException(404, 'Employee or attendance record not found')
    provided = body.model_fields_set - {'reason', 'regularized_by'}
    if not provided:
        raise HTTPException(422, 'At least one attendance field must be supplied')
    status = body.status if 'status' in provided and body.status is not None else record['status']
    punch_in = epoch_ms_to_utc(body.punch_in) if 'punch_in' in provided and body.punch_in is not None else record.get('punch_in')
    punch_out = epoch_ms_to_utc(body.punch_out) if 'punch_out' in provided and body.punch_out is not None else record.get('punch_out')
    if status in ('ABSENT', 'LEAVE'):
        if 'punch_in' in provided and body.punch_in is not None or ('punch_out' in provided and body.punch_out is not None):
            raise HTTPException(422, 'ABSENT and LEAVE cannot be supplied with punch times')
        punch_in = punch_out = None
    else:
        if punch_in is None:
            raise HTTPException(422, 'Presence status requires punch_in')
        if attendance_date_for_punch(punch_in, employee['shift_start'], employee['shift_end']) != date_str:
            raise HTTPException(422, 'punch_in must belong to the record attendance date')
    derived = compute_attendance_values(punch_in, punch_out, status, employee['shift_start'], employee['shift_end'], date_str)
    final_values = {'status': status, **derived}
    changed = {}
    for key, new_value in final_values.items():
        old_value = record.get(key)
        if isinstance(old_value, datetime) and isinstance(new_value, datetime):
            equal = to_utc_naive(old_value) == to_utc_naive(new_value)
        else:
            equal = old_value == new_value
        if not equal:
            changed[key] = {'from': old_value, 'to': new_value}
    if not changed:
        raise HTTPException(422, 'The correction does not change this record')
    history_entry = {'at': to_utc_naive(now_utc()), 'by': body.regularized_by, 'reason': body.reason, 'changes': changed}
    try:
        result = db.attendance_logs.update_one({'_id': record['_id'], 'history': record.get('history', [])}, {'$set': final_values, '$push': {'history': history_entry}})
        if result.modified_count != 1:
            raise HTTPException(409, 'Attendance was changed by another request; retry')
        updated = db.attendance_logs.find_one({'_id': record['_id']}, {'_id': 0})
    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    return api_value(updated)

def month_log_match(start: date, end: date, emp_code: Optional[str]=None, department: Optional[str]=None):
    match = {'date': {'$gte': start.isoformat(), '$lte': end.isoformat()}}
    if emp_code:
        match['emp_code'] = emp_code
    pipeline = [{'$match': match}]
    if department is not None:
        pipeline += [{'$lookup': {'from': 'employees', 'localField': 'emp_code', 'foreignField': 'emp_code', 'as': 'employee'}}, {'$unwind': '$employee'}, {'$match': {'employee.department': department}}]
    return pipeline

def employee_month_pipeline(start: date, end: date, emp_code: str):
    return month_log_match(start, end, emp_code) + [{'$group': {'_id': None, 'present_days': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', list(PRESENCE)]}, is_workday_expression({'$dateFromString': {'dateString': '$date'}})]}, {'$cond': ['$half_day', 0.5, 1]}, 0]}}, 'leave_days': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}, 'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}}, 'total_overtime_minutes': {'$sum': {'$ifNull': ['$overtime_minutes', 0]}}}}]

def department_summary_pipeline(start: date, end: date, department: Optional[str]=None):
    match = {'joined_on': {'$lte': end.isoformat()}}
    if department is not None:
        match['department'] = department
    return [{'$match': match}, {'$lookup': {'from': 'attendance_logs', 'let': {'code': '$emp_code'}, 'pipeline': [{'$match': {'$expr': {'$and': [{'$eq': ['$emp_code', '$$code']}, {'$gte': ['$date', start.isoformat()]}, {'$lte': ['$date', end.isoformat()]}]}}}], 'as': 'logs'}}, {'$unwind': {'path': '$logs', 'preserveNullAndEmptyArrays': True}}, {'$group': {'_id': '$department', 'headcount': {'$addToSet': '$emp_code'}, 'present_days': {'$sum': {'$cond': [{'$and': [{'$in': ['$logs.status', list(PRESENCE)]}, {'$ne': ['$logs.date', None]}, is_workday_expression({'$dateFromString': {'dateString': '$logs.date'}})]}, {'$cond': [{'$ifNull': ['$logs.half_day', False]}, 0.5, 1]}, 0]}}, 'work_hours_sum': {'$sum': {'$cond': [{'$and': [{'$in': ['$logs.status', list(PRESENCE)]}, {'$ne': ['$logs.work_hours', None]}]}, '$logs.work_hours', 0]}}, 'work_hours_count': {'$sum': {'$cond': [{'$and': [{'$in': ['$logs.status', list(PRESENCE)]}, {'$ne': ['$logs.work_hours', None]}]}, 1, 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$logs.late_minutes', 0]}, 0]}, 1, 0]}}, 'total_late_minutes': {'$sum': {'$ifNull': ['$logs.late_minutes', 0]}}, 'leave_count': {'$sum': {'$cond': [{'$eq': ['$logs.status', 'LEAVE']}, 1, 0]}}, 'on_duty_count': {'$sum': {'$cond': [{'$eq': ['$logs.status', 'ON_DUTY']}, 1, 0]}}}}, {'$project': {'_id': 0, 'department': '$_id', 'headcount': {'$size': '$headcount'}, 'present_days': 1, 'avg_work_hours': {'$cond': [{'$gt': ['$work_hours_count', 0]}, {'$divide': ['$work_hours_sum', '$work_hours_count']}, None]}, 'late_count': 1, 'total_late_minutes': 1, 'leave_count': 1, 'on_duty_count': 1}}, {'$sort': {'department': 1}}]

def leaderboard_pipeline(start: date, end: date, limit: int, department: Optional[str]=None):
    pipeline = month_log_match(start, end, department=department)
    pipeline += [{'$group': {'_id': '$emp_code', 'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}}}}, {'$match': {'total_late_minutes': {'$gt': 0}}}, {'$setWindowFields': {'sortBy': {'total_late_minutes': -1}, 'output': {'rank': {'$rank': {}}}}}, {'$match': {'rank': {'$lte': limit}}}, {'$lookup': {'from': 'employees', 'localField': '_id', 'foreignField': 'emp_code', 'as': 'employee'}}, {'$unwind': '$employee'}]
    if department is not None:
        pipeline.append({'$match': {'employee.department': department}})
    pipeline += [{'$project': {'_id': 0, 'emp_code': '$_id', 'name': '$employee.name', 'department': '$employee.department', 'total_late_minutes': 1, 'rank': 1}}, {'$sort': {'total_late_minutes': -1, 'emp_code': 1}}]
    return pipeline

def trend_pipeline(start: date, end: date, department: str):
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end, time.min, tzinfo=UTC)
    return [{'$match': {'department': department}}, {'$group': {'_id': None, 'emp_codes': {'$addToSet': '$emp_code'}}}, {'$set': {'days': {'$map': {'input': {'$range': [0, {'$add': [{'$dateDiff': {'startDate': start_dt, 'endDate': end_dt, 'unit': 'day'}}, 1]}]}, 'as': 'offset', 'in': {'$dateAdd': {'startDate': start_dt, 'unit': 'day', 'amount': '$$offset'}}}}}}, {'$unwind': '$days'}, {'$set': {'date_str': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$days'}}}}, {'$lookup': {'from': 'attendance_logs', 'let': {'day': '$date_str', 'codes': '$emp_codes'}, 'pipeline': [{'$match': {'$expr': {'$and': [{'$eq': ['$date', '$$day']}, {'$in': ['$emp_code', '$$codes']}]}}}], 'as': 'logs'}}, {'$set': {'present_count': {'$reduce': {'input': '$logs', 'initialValue': 0, 'in': {'$add': ['$$value', {'$cond': [{'$in': ['$$this.status', list(PRESENCE)]}, {'$cond': [{'$ifNull': ['$$this.half_day', False]}, 0.5, 1]}, 0]}]}}}, 'late_count': {'$reduce': {'input': '$logs', 'initialValue': 0, 'in': {'$add': ['$$value', {'$cond': [{'$gt': [{'$ifNull': ['$$this.late_minutes', 0]}, 0]}, 1, 0]}]}}}, 'is_working_day': is_workday_expression('$days')}}, {'$lookup': {'from': 'employees', 'let': {'day': '$date_str'}, 'pipeline': [{'$match': {'department': department}}, {'$match': {'$expr': {'$lte': ['$joined_on', '$$day']}}}, {'$count': 'n'}], 'as': 'hc'}}, {'$set': {'headcount': {'$ifNull': [{'$arrayElemAt': ['$hc.n', 0]}, 0]}}}, {'$set': {'attendance_rate': {'$cond': [{'$and': ['$is_working_day', {'$gt': ['$headcount', 0]}]}, {'$multiply': [{'$divide': ['$present_count', '$headcount']}, 100]}, None]}}}, {'$setWindowFields': {'sortBy': {'days': 1}, 'output': {'moving_avg_7d': {'$avg': '$attendance_rate', 'window': {'documents': [-6, 0]}}}}}, {'$project': {'_id': 0, 'date': '$date_str', 'is_working_day': 1, 'headcount': 1, 'present_count': 1, 'late_count': 1, 'attendance_rate': 1, 'moving_avg_7d': 1}}, {'$sort': {'date': 1}}]

@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code: str, month: str=Query(..., pattern='^\\d{4}-\\d{2}$')):
    start, end = parse_month(month)
    try:
        employee = db.employees.find_one({'emp_code': emp_code})
        if not employee:
            raise HTTPException(404, 'Employee not found')
        rows = list(db.attendance_logs.aggregate(employee_month_pipeline(start, end, emp_code)))
    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    working_start = max(start, parse_date(employee['joined_on'], 'joined_on'))
    working_days = working_days_count(working_start, end)
    stats = rows[0] if rows else {}
    present_days = float(stats.get('present_days', 0))
    rate = rounded(present_days / working_days * 100, 4) if working_days else None
    return {'emp_code': emp_code, 'month': month, 'working_days': working_days, 'present_days': rounded(present_days), 'leave_days': stats.get('leave_days', 0), 'late_count': stats.get('late_count', 0), 'total_late_minutes': stats.get('total_late_minutes', 0), 'total_overtime_minutes': stats.get('total_overtime_minutes', 0), 'attendance_pct': rate}

@app.get('/analytics/departments/summary')
def department_summary(month: str=Query(..., pattern='^\\d{4}-\\d{2}$'), department: Optional[str]=None):
    start, end = parse_month(month)
    try:
        result = list(db.employees.aggregate(department_summary_pipeline(start, end, department)))
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    for item in result:
        item['present_days'] = rounded(item.get('present_days', 0))
        if item.get('avg_work_hours') is not None:
            item['avg_work_hours'] = rounded(item['avg_work_hours'])
    return {'month': month, 'items': result}

@app.get('/analytics/leaderboard/late')
def late_leaderboard(month: str=Query(..., pattern='^\\d{4}-\\d{2}$'), limit: int=Query(10, ge=1, le=50), department: Optional[str]=None):
    start, end = parse_month(month)
    try:
        result = list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    return {'month': month, 'items': result}

@app.get('/analytics/departments/{department}/trend')
def department_trend(department: str, from_date: str=Query(..., alias='from'), to_date: str=Query(..., alias='to')):
    start = parse_date(from_date, 'from')
    end = parse_date(to_date, 'to')
    if end < start:
        raise HTTPException(422, 'to must be on or after from')
    if (end - start).days + 1 > 92:
        raise HTTPException(422, 'Date range cannot exceed 92 days')
    try:
        if db.employees.count_documents({'department': department}) == 0:
            raise HTTPException(404, 'Department not found')
        result = list(db.employees.aggregate(trend_pipeline(start, end, department)))
    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    items = []
    for row in result:
        if not from_date <= row.get('date', '') <= to_date:
            continue
        row['attendance_rate'] = rounded(row['attendance_rate'], 4) if row.get('attendance_rate') is not None else None
        row['moving_avg_7d'] = rounded(row['moving_avg_7d'], 4) if row.get('moving_avg_7d') is not None else None
        items.append(row)
    return {'department': department, 'from': from_date, 'to': to_date, 'items': items}

@app.get('/admin/explain/{endpoint}')
def explain_endpoint(endpoint: str, emp_code: Optional[str]=None, date_from: Optional[str]=None, date_to: Optional[str]=None, status: Optional[str]=None, month: Optional[str]=None, department: Optional[str]=None, limit: int=Query(10, ge=1, le=50), from_date: Optional[str]=Query(None, alias='from'), to_date: Optional[str]=Query(None, alias='to'), page: int=Query(1, ge=1), page_size: int=Query(20, ge=1, le=100)):
    try:
        if endpoint == 'attendance_list':
            query = attendance_query(emp_code, date_from, date_to, status)
            explanation = db.command('explain', {'find': 'attendance_logs', 'filter': query, 'sort': {'date': -1, 'emp_code': 1}, 'skip': (page - 1) * page_size, 'limit': page_size}, verbosity='executionStats')
        elif endpoint == 'employee_monthly':
            if not emp_code or not month:
                raise HTTPException(422, 'emp_code and month are required')
            start, end = parse_month(month)
            explanation = db.command('explain', {'aggregate': 'attendance_logs', 'pipeline': employee_month_pipeline(start, end, emp_code), 'cursor': {}}, verbosity='executionStats')
        elif endpoint == 'department_summary':
            if not month:
                raise HTTPException(422, 'month is required')
            start, end = parse_month(month)
            explanation = db.command('explain', {'aggregate': 'employees', 'pipeline': department_summary_pipeline(start, end, department), 'cursor': {}}, verbosity='executionStats')
        elif endpoint == 'late_leaderboard':
            if not month:
                raise HTTPException(422, 'month is required')
            start, end = parse_month(month)
            explanation = db.command('explain', {'aggregate': 'attendance_logs', 'pipeline': leaderboard_pipeline(start, end, limit, department), 'cursor': {}}, verbosity='executionStats')
        elif endpoint == 'department_trend':
            if not department or not from_date or (not to_date):
                raise HTTPException(422, 'department, from and to are required')
            start, end = (parse_date(from_date, 'from'), parse_date(to_date, 'to'))
            if end < start or (end - start).days + 1 > 92:
                raise HTTPException(422, 'Invalid trend date range')
            explanation = db.command('explain', {'aggregate': 'employees', 'pipeline': trend_pipeline(start, end, department), 'cursor': {}}, verbosity='executionStats')
        else:
            raise HTTPException(422, 'Unknown endpoint name')
    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(503, 'Database operation failed')
    collection = 'employees' if endpoint == 'department_summary' else 'employees' if endpoint == 'department_trend' else 'attendance_logs'
    return json.loads(json_util.dumps({'endpoint': endpoint, 'collection': collection, 'explain': explanation}))
