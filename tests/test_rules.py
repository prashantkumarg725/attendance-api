"""Unit tests for the pure business rules (no database needed): pytest tests/test_rules.py"""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.main import (
    IST, EmployeeIn, PunchInIn, RegularizeIn, attendance_date, compute_late_minutes, compute_overtime,
    count_weekdays, derive, dt_to_ms, half_up_div, ms_to_dt, month_bounds, work_hours_hundredths,
)


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)


def test_late_grace_and_floor():  # R2 examples from the contract (shift 09:30)
    assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 0), "2026-07-06", "09:30") == 0
    assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 1), "2026-07-06", "09:30") == 10
    assert compute_late_minutes(ist(2026, 7, 6, 10, 15, 59), "2026-07-06", "09:30") == 45
    assert compute_late_minutes(ist(2026, 7, 6, 9, 0, 0), "2026-07-06", "09:30") == 0  # early


def test_overtime_threshold_and_overnight():  # R3
    assert compute_overtime(ist(2026, 7, 6, 18, 59, 59), "2026-07-06", "09:30", "18:30") == 0  # 29 min
    assert compute_overtime(ist(2026, 7, 6, 19, 0, 0), "2026-07-06", "09:30", "18:30") == 30
    assert compute_overtime(ist(2026, 7, 6, 19, 10, 0), "2026-07-06", "09:30", "18:30") == 40
    # overnight 22:00-06:00, shift date 07-06, out 06:40 next day -> 40 (sample document 7)
    assert compute_overtime(ist(2026, 7, 7, 6, 40), "2026-07-06", "22:00", "06:00") == 40


def test_work_hours_half_up():  # R4 / R5
    assert work_hours_hundredths(ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 12, 30)) == 350
    # 3h 0m 18s = 3.005 h exactly -> half-up 3.01 (banker's/float round gives 3.0)
    assert work_hours_hundredths(ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 12, 0, 18)) == 301
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    d = derive(emp, "2026-07-06", "WFH", ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 13, 29, 40))  # 4.4944 -> 4.49
    assert d["work_hours"] == 4.49 and d["half_day"] is True
    d = derive(emp, "2026-07-06", "WFH", ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 13, 30, 0))  # exactly 4.50
    assert d["work_hours"] == 4.5 and d["half_day"] is False


def test_derive_samples_from_data_model():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    d = derive(emp, "2026-07-06", "PRESENT", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10))
    assert (d["work_hours"], d["late_minutes"], d["overtime_minutes"], d["half_day"]) == (9.08, 35, 40, False)
    d = derive(emp, "2026-07-06", "PRESENT", ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35))
    assert (d["work_hours"], d["late_minutes"], d["overtime_minutes"]) == (9.12, 0, 0)
    night = {"shift_start": "22:00", "shift_end": "06:00"}
    d = derive(night, "2026-07-06", "PRESENT", ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40))
    assert (d["work_hours"], d["late_minutes"], d["overtime_minutes"]) == (8.75, 0, 40)
    assert derive(emp, "2026-07-06", "ABSENT", None, None) == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_attendance_date_r1():
    # normal shift: IST date, even when the UTC date differs (00:30 IST is still the previous UTC day)
    assert attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == "2026-07-07"
    # overnight: 03:00 IST is earlier than shift_end 06:00 -> belongs to the shift that started yesterday
    assert attendance_date(ist(2026, 7, 7, 3, 0), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"  # not strictly earlier
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"
    # late for an overnight shift is measured from the shift's own start
    assert compute_late_minutes(ist(2026, 7, 7, 3, 0), "2026-07-06", "22:00") == 300


def test_epoch_ms_roundtrip():
    assert dt_to_ms(ms_to_dt(1783312500123)) == 1783312500123
    assert ms_to_dt(1783312500999).replace(microsecond=0) == ms_to_dt(1783312500000)


def test_half_up_div():
    assert half_up_div(5, 2) == 3 and half_up_div(4, 3) == 1 and half_up_div(1, 2) == 1 and half_up_div(0, 7) == 0


def test_calendar_helpers():
    from datetime import date
    assert count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15  # mid-month joiner (Mon 13th)
    assert count_weekdays(date(2026, 8, 1), date(2026, 7, 31)) == 0
    assert month_bounds("2026-02")[:2] == ("2026-02-01", "2026-02-28")
    with pytest.raises(Exception):
        month_bounds("2026-13")


def test_validation():
    ok = dict(emp_code="EMP0001", name="A", email="a@b.co", department="X", joined_on="2026-01-05")
    assert EmployeeIn(**ok).shift_start == "09:30"
    for bad in (dict(emp_code="E1"), dict(email="nope"), dict(joined_on="2026-02-30"), dict(joined_on="20260105"),
                dict(shift_start="9:30"), dict(shift_start="10:00", shift_end="10:00")):
        with pytest.raises(ValidationError):
            EmployeeIn(**{**ok, **bad})
    for bad in (1783312500, 1783312500000.5, "1783312500000", True, 4102444800001):  # seconds, float, str, bool, range
        with pytest.raises(ValidationError):
            PunchInIn(emp_code="EMP0001", punched_at=bad)
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", status="ABSENT")
    with pytest.raises(ValidationError):
        RegularizeIn(reason="abcde", regularized_by="hr", punch_in=None)
    assert RegularizeIn(reason="abcde", regularized_by="hr", work_hours=99).punch_in is None  # derived ignored
