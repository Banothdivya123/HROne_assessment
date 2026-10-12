"""
Tests for the Employee Attendance & Analytics API.
Requires a running MongoDB instance (MONGO_URI env var or localhost:27017).
Uses a separate test database to avoid interfering with real data.
"""
import os
import time
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

# Override DB name before importing app
os.environ["MONGO_DB"] = "attendance_test_db"

from app.main import app, db, IST, create_indexes, _round2, _compute_late_minutes, _compute_attendance_date, _compute_overtime, _compute_work_hours, _compute_half_day, _epoch_to_dt, _dt_to_epoch

client = TestClient(app)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def clean_db():
    """Clear all collections before each test."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    create_indexes()
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def _make_epoch(year, month, day, hour, minute, second=0):
    """Helper: create epoch ms from IST components."""
    dt = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return int(dt.timestamp() * 1000)


def _create_emp(emp_code="EMP0001", name="Test User", department="Engineering",
                shift_start="09:30", shift_end="18:30", joined_on="2026-01-01"):
    """Helper: create an employee via API."""
    return client.post("/employees", json={
        "emp_code": emp_code,
        "name": name,
        "email": f"{emp_code.lower()}@test.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
    })


# --------------------------------------------------------------------------- #
# Unit tests for helper functions
# --------------------------------------------------------------------------- #
class TestHelpers:
    def test_round2_half_up(self):
        assert _round2(8.425) == 8.43  # half-up, not banker's
        assert _round2(8.424) == 8.42
        assert _round2(8.426) == 8.43
        assert _round2(9.115) == 9.12

    def test_epoch_conversion_roundtrip(self):
        ms = 1783312500000
        dt = _epoch_to_dt(ms)
        assert dt.tzinfo is not None
        assert _dt_to_epoch(dt) == ms

    def test_late_minutes_within_grace(self):
        # 09:40:00 IST — exactly 10 minutes, not late
        punch = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)  # 09:40 IST
        assert _compute_late_minutes(punch, "09:30") == 0

    def test_late_minutes_just_over_grace(self):
        # 09:40:01 IST — 10 min 1 sec after start, late_minutes = 10
        punch = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)  # 09:40:01 IST
        assert _compute_late_minutes(punch, "09:30") == 10

    def test_late_minutes_very_late(self):
        # 10:15:59 IST — 45 min 59 sec after start → floor = 45
        punch = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)  # 10:15:59 IST
        assert _compute_late_minutes(punch, "09:30") == 45

    def test_attendance_date_normal(self):
        # 09:28 IST on July 6 → date = 2026-07-06
        punch = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        assert _compute_attendance_date(punch, "09:30", "18:30") == "2026-07-06"

    def test_attendance_date_overnight_before_end(self):
        # Overnight shift 22:00-06:00, punch at 01:00 IST → belongs to previous day
        punch = datetime(2026, 7, 7, 19, 30, 0, tzinfo=timezone.utc)  # 01:00 IST Jul 8
        # Wait, that's 19:30 UTC Jul 7 = 01:00 IST Jul 8
        # Shift end is 06:00, punch IST time 01:00 < 06:00 → date = Jul 7
        assert _compute_attendance_date(punch, "22:00", "06:00") == "2026-07-07"

    def test_overtime_under_threshold(self):
        # Leave 5 min after shift end → OT = 0 (under 30 min threshold)
        punch_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)  # 18:35 IST
        assert _compute_overtime(punch_out, "18:30", "2026-07-06", "09:30") == 0

    def test_overtime_over_threshold(self):
        # Leave 40 min after shift end → OT = 40
        punch_out = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)  # 19:10 IST
        assert _compute_overtime(punch_out, "18:30", "2026-07-06", "09:30") == 40

    def test_work_hours(self):
        pin = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        pout = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        assert _compute_work_hours(pin, pout) == 9.12

    def test_half_day(self):
        assert _compute_half_day(3.5) is True
        assert _compute_half_day(4.49) is True
        assert _compute_half_day(4.50) is False
        assert _compute_half_day(9.0) is False


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
class TestHealth:
    def test_health_ok(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employee CRUD
# --------------------------------------------------------------------------- #
class TestEmployees:
    def test_create_employee(self):
        r = _create_emp()
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert isinstance(data["created_at"], int)
        assert data["created_at"] >= 100000000000
        assert "id" not in data
        assert "_id" not in data

    def test_duplicate_emp_code_409(self):
        _create_emp()
        r = _create_emp()
        assert r.status_code == 409

    def test_invalid_emp_code_422(self):
        r = client.post("/employees", json={
            "emp_code": "XYZ",
            "name": "Test",
            "email": "t@t.com",
            "department": "Eng",
            "joined_on": "2026-01-01",
        })
        assert r.status_code == 422

    def test_shift_start_equals_shift_end_422(self):
        r = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Test",
            "email": "t@t.com",
            "department": "Eng",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01",
        })
        assert r.status_code == 422

    def test_list_employees_pagination(self):
        _create_emp("EMP0001")
        _create_emp("EMP0002", name="User 2")
        _create_emp("EMP0003", name="User 3")

        r = client.get("/employees?page=1&page_size=2")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["items"]) == 2
        assert data["items"][0]["emp_code"] == "EMP0001"
        assert data["items"][1]["emp_code"] == "EMP0002"

    def test_list_employees_filter_department(self):
        _create_emp("EMP0001", department="Eng")
        _create_emp("EMP0002", department="Sales")

        r = client.get("/employees?department=Eng")
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["department"] == "Eng"


# --------------------------------------------------------------------------- #
# Punch-in
# --------------------------------------------------------------------------- #
class TestPunchIn:
    def test_punch_in_basic(self):
        _create_emp()
        ts = _make_epoch(2026, 7, 6, 9, 28)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201
        data = r.json()
        assert data["date"] == "2026-07-06"
        assert data["status"] == "PRESENT"
        assert data["late_minutes"] == 0
        assert data["punch_out"] is None
        assert data["work_hours"] is None

    def test_punch_in_late(self):
        _create_emp()
        ts = _make_epoch(2026, 7, 6, 10, 5)  # 35 min after 09:30
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 35

    def test_punch_in_unknown_employee_404(self):
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": _make_epoch(2026, 7, 6, 9, 30)})
        assert r.status_code == 404

    def test_punch_in_duplicate_409(self):
        _create_emp()
        ts = _make_epoch(2026, 7, 6, 9, 28)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 409

    def test_punch_in_invalid_status_422(self):
        _create_emp()
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": _make_epoch(2026, 7, 6, 9, 30),
            "status": "ABSENT",
        })
        assert r.status_code == 422

    def test_punch_in_wfh(self):
        _create_emp()
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": _make_epoch(2026, 7, 6, 9, 30),
            "status": "WFH",
        })
        assert r.status_code == 201
        assert r.json()["status"] == "WFH"


# --------------------------------------------------------------------------- #
# Punch-out
# --------------------------------------------------------------------------- #
class TestPunchOut:
    def test_punch_out_basic(self):
        _create_emp()
        pin = _make_epoch(2026, 7, 6, 9, 28)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})

        pout = _make_epoch(2026, 7, 6, 18, 35)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout})
        assert r.status_code == 200
        data = r.json()
        assert data["work_hours"] == 9.12
        assert data["overtime_minutes"] == 0  # 5 min < 30 threshold
        assert data["half_day"] is False

    def test_punch_out_with_overtime(self):
        _create_emp()
        pin = _make_epoch(2026, 7, 6, 10, 5)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})

        pout = _make_epoch(2026, 7, 6, 19, 10)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout})
        assert r.status_code == 200
        data = r.json()
        assert data["overtime_minutes"] == 40

    def test_punch_out_half_day(self):
        _create_emp()
        pin = _make_epoch(2026, 7, 7, 9, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})

        pout = _make_epoch(2026, 7, 7, 13, 0)  # 3.5 hours
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout})
        assert r.status_code == 200
        assert r.json()["half_day"] is True

    def test_punch_out_before_punch_in_422(self):
        _create_emp()
        pin = _make_epoch(2026, 7, 6, 9, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})

        pout = _make_epoch(2026, 7, 6, 9, 0)  # Before punch-in
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout})
        assert r.status_code in (404, 422)

    def test_punch_out_unknown_employee_404(self):
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": _make_epoch(2026, 7, 6, 18, 0)})
        assert r.status_code == 404

    def test_punch_out_no_open_record_404(self):
        _create_emp()
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": _make_epoch(2026, 7, 6, 18, 0)})
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# List attendance
# --------------------------------------------------------------------------- #
class TestListAttendance:
    def test_list_attendance_basic(self):
        _create_emp()
        pin = _make_epoch(2026, 7, 6, 9, 28)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})

        r = client.get("/attendance")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert "_id" not in data["items"][0]
        assert "id" not in data["items"][0]

    def test_list_attendance_date_range(self):
        _create_emp()
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": _make_epoch(2026, 7, 6, 9, 28)})
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": _make_epoch(2026, 7, 7, 9, 28)})

        r = client.get("/attendance?date_from=2026-07-06&date_to=2026-07-06")
        assert r.json()["total"] == 1

    def test_list_attendance_invalid_date_range_422(self):
        r = client.get("/attendance?date_from=2026-07-07&date_to=2026-07-06")
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Regularize
# --------------------------------------------------------------------------- #
class TestRegularize:
    def test_regularize_punch_in(self):
        _create_emp()
        pin = _make_epoch(2026, 7, 6, 10, 5)  # Late 35 min
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
        pout = _make_epoch(2026, 7, 6, 18, 30)
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout})

        new_pin = _make_epoch(2026, 7, 6, 9, 28)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": new_pin,
            "reason": "biometric glitch fixed",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["late_minutes"] == 0
        assert len(data["history"]) == 1
        assert "punch_in" in data["history"][0]["changes"]

    def test_regularize_no_changes_422(self):
        _create_emp()
        pin = _make_epoch(2026, 7, 6, 9, 28)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})

        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "testing no changes",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_regularize_absent_with_punch_422(self):
        _create_emp()
        pin = _make_epoch(2026, 7, 6, 9, 28)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})

        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": pin,
            "reason": "testing invalid",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_regularize_404_no_record(self):
        _create_emp()
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "reason": "testing not found",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Analytics: Employee Monthly
# --------------------------------------------------------------------------- #
class TestEmployeeMonthly:
    def test_monthly_basic(self):
        _create_emp(joined_on="2026-07-01")
        pin = _make_epoch(2026, 7, 6, 9, 28)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
        pout = _make_epoch(2026, 7, 6, 18, 35)
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout})

        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert data["month"] == "2026-07"
        assert data["present_days"] == 1
        assert data["working_days"] >= 1

    def test_monthly_unknown_employee_404(self):
        r = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Analytics: Department Summary
# --------------------------------------------------------------------------- #
class TestDepartmentSummary:
    def test_summary_basic(self):
        _create_emp(joined_on="2026-07-01")
        r = client.get("/analytics/departments/summary?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        assert data["month"] == "2026-07"
        assert len(data["items"]) >= 1


# --------------------------------------------------------------------------- #
# Analytics: Late Leaderboard
# --------------------------------------------------------------------------- #
class TestLateLeaderboard:
    def test_leaderboard_basic(self):
        _create_emp("EMP0001", joined_on="2026-07-01")
        _create_emp("EMP0002", name="User 2", joined_on="2026-07-01")

        # EMP0001 late
        pin1 = _make_epoch(2026, 7, 6, 10, 5)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin1})

        # EMP0002 on time
        pin2 = _make_epoch(2026, 7, 6, 9, 28)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": pin2})

        r = client.get("/analytics/leaderboard/late?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["emp_code"] == "EMP0001"
        assert data["items"][0]["rank"] == 1


# --------------------------------------------------------------------------- #
# Analytics: Department Trend
# --------------------------------------------------------------------------- #
class TestDepartmentTrend:
    def test_trend_basic(self):
        _create_emp(joined_on="2026-07-01")
        pin = _make_epoch(2026, 7, 6, 9, 28)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})

        r = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-06")
        assert r.status_code == 200
        data = r.json()
        assert data["department"] == "Engineering"
        assert len(data["items"]) == 1
        assert data["items"][0]["date"] == "2026-07-06"

    def test_trend_unknown_department_404(self):
        r = client.get("/analytics/departments/Nonexistent/trend?from=2026-07-06&to=2026-07-06")
        assert r.status_code == 404

    def test_trend_invalid_range_422(self):
        _create_emp()
        r = client.get("/analytics/departments/Engineering/trend?from=2026-07-07&to=2026-07-06")
        assert r.status_code == 422

    def test_trend_exceeds_92_days_422(self):
        _create_emp()
        r = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Admin: Explain
# --------------------------------------------------------------------------- #
class TestExplain:
    def test_explain_attendance_list(self):
        r = client.get("/admin/explain/attendance_list")
        assert r.status_code == 200
        data = r.json()
        assert data["endpoint"] == "attendance_list"
        assert data["collection"] == "attendance_logs"
        assert "explain" in data
