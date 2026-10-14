import os
import pytest
from datetime import datetime, timezone, timedelta
from bson import json_util
from fastapi.testclient import TestClient
from pymongo import MongoClient
from app.main import app, db, IST, UTC, ensure_indexes

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_database():
    # Run before tests
    ensure_indexes()
    yield

def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_employee_crud():
    test_emp = {
        "emp_code": "EMP9901",
        "name": "Test User",
        "email": "test9901@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-06-01",
    }
    # Clean up if existed
    db.employees.delete_one({"emp_code": "EMP9901"})

    # Create
    res = client.post("/employees", json=test_emp)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["emp_code"] == "EMP9901"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data
    assert "id" not in data

    # Duplicate create -> 409
    res_dup = client.post("/employees", json=test_emp)
    assert res_dup.status_code == 409

    # Equal shift_start and shift_end -> 422
    bad_shift = dict(test_emp)
    bad_shift["emp_code"] = "EMP9902"
    bad_shift["shift_end"] = "09:30"
    res_bad = client.post("/employees", json=bad_shift)
    assert res_bad.status_code == 422

    # List employees
    res_list = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["page"] == 1
    assert any(e["emp_code"] == "EMP9901" for e in list_data["items"])

def test_punch_in_and_punch_out():
    emp_code = "EMP9901"
    date_str = "2026-06-05"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": date_str})

    # Punch in at 09:40:01 IST (601 seconds after 09:30 -> 10 min late per R2)
    # 2026-06-05 09:40:01 IST = 2026-06-05 04:10:01 UTC
    dt_in = datetime(2026, 6, 5, 4, 10, 1, 950000, tzinfo=UTC)
    ms_in = int(dt_in.timestamp() * 1000)

    res_in = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_in,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201, res_in.text
    rec = res_in.json()
    assert rec["date"] == date_str
    assert rec["late_minutes"] == 10
    assert rec["work_hours"] is None
    assert rec["half_day"] is False
    assert "_id" not in rec
    assert "id" not in rec

    # Duplicate punch in for same day -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_in,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # Punch out at 19:15:00 IST (45 min after 18:30 shift end -> 45 min overtime per R3)
    # 2026-06-05 19:15:00 IST = 2026-06-05 13:45:00 UTC
    dt_out = datetime(2026, 6, 5, 13, 45, 0, tzinfo=UTC)
    ms_out = int(dt_out.timestamp() * 1000)

    res_out = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": ms_out
    })
    assert res_out.status_code == 200, res_out.text
    rec_out = res_out.json()
    assert rec_out["overtime_minutes"] == 45
    # Work hours: (13:45:00 - 04:10:01) = 34499 seconds = 9.58305... -> 9.58
    assert rec_out["work_hours"] == 9.58
    assert rec_out["half_day"] is False

    # Second punch out -> 409
    res_out_dup = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": ms_out
    })
    assert res_out_dup.status_code == 409

def test_overnight_shift():
    # Create overnight shift employee (22:00 to 06:00)
    emp_code = "EMP9903"
    db.employees.delete_one({"emp_code": emp_code})
    db.employees.insert_one({
        "emp_code": emp_code,
        "name": "Night Owl",
        "email": "night@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(tz=UTC)
    })

    # Punch in at 05:30 IST on 2026-06-10 (earlier than shift_end 06:00)
    # Belongs to shift that started previous day: 2026-06-09
    # 2026-06-10 05:30:00 IST = 2026-06-10 00:00:00 UTC
    dt_in = datetime(2026, 6, 10, 0, 0, 0, tzinfo=UTC)
    ms_in = int(dt_in.timestamp() * 1000)
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": "2026-06-09"})

    res = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_in,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    assert res.json()["date"] == "2026-06-09"

    # Punch out at 06:40 IST on 2026-06-10 (40 min overtime after 06:00)
    # 2026-06-10 06:40:00 IST = 2026-06-10 01:10:00 UTC
    dt_out = datetime(2026, 6, 10, 1, 10, 0, tzinfo=UTC)
    ms_out = int(dt_out.timestamp() * 1000)
    res_out = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": ms_out
    })
    assert res_out.status_code == 200
    assert res_out.json()["overtime_minutes"] == 40

def test_regularize_attendance():
    emp_code = "EMP9901"
    date_str = "2026-06-05"

    # Request with no changes -> 422
    res_no_change = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "reason": "Accidental patch",
        "regularized_by": "admin"
    })
    assert res_no_change.status_code == 422

    # Change punch-in time to 09:30:00 IST (04:00:00 UTC) -> late_minutes becomes 0
    dt_in_new = datetime(2026, 6, 5, 4, 0, 0, tzinfo=UTC)
    ms_in_new = int(dt_in_new.timestamp() * 1000)

    res_reg = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "punch_in": ms_in_new,
        "reason": "Biometric scanner delay",
        "regularized_by": "hr_manager"
    })
    assert res_reg.status_code == 200, res_reg.text
    reg_data = res_reg.json()
    assert reg_data["late_minutes"] == 0
    assert len(reg_data["history"]) == 1
    h = reg_data["history"][0]
    assert h["by"] == "hr_manager"
    assert h["reason"] == "Biometric scanner delay"
    assert "punch_in" in h["changes"]
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["to"] == 0

def test_attendance_list():
    res = client.get("/attendance?page=1&page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert "total" in data

    # date_from > date_to -> 422
    res_bad = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res_bad.status_code == 422

def test_explain_endpoints():
    # 1. attendance_list
    res1 = client.get("/admin/explain/attendance_list")
    assert res1.status_code == 200
    assert res1.json()["collection"] == "attendance_logs"

    # 2. employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200

    # 3. department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    assert res3.json()["collection"] == "employees"

    # 4. late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200

    # 5. department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-08")
    assert res5.status_code == 200

    # Missing required param in explain -> 422
    res_bad = client.get("/admin/explain/employee_monthly")
    assert res_bad.status_code == 422


def test_analytics_endpoints_and_sample_data():
    # Reload sample seed to have fresh standard dataset
    from sample_seed import here, opts
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)

    # 1. Employee Monthly
    res_monthly = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res_monthly.status_code == 200, res_monthly.text
    m_data = res_monthly.json()
    assert m_data["emp_code"] == "EMP0001"
    assert m_data["working_days"] == 23  # Mon-Fri in July 2026
    assert m_data["present_days"] == 2.0  # 2 presence days in sample data
    assert m_data["late_count"] == 0
    assert m_data["attendance_pct"] == 8.70  # 2 / 23 * 100 = 8.6956... -> 8.70 half-up

    # 2. Department Summary
    res_dept = client.get("/analytics/departments/summary?month=2026-07")
    assert res_dept.status_code == 200, res_dept.text
    d_data = res_dept.json()
    assert "items" in d_data
    eng = next((d for d in d_data["items"] if d["department"] == "Engineering"), None)
    assert eng is not None
    assert eng["headcount"] == 2  # EMP0001 and EMP0002 (EMP0002 joined 2026-07-13 <= 2026-07-31)
    assert eng["present_days"] == 3.0
    assert eng["avg_work_hours"] == 9.04

    # 3. Leaderboard Late
    res_lb = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res_lb.status_code == 200, res_lb.text
    lb_data = res_lb.json()
    assert len(lb_data["items"]) >= 1
    top = lb_data["items"][0]
    assert top["emp_code"] == "EMP0003"
    assert top["total_late_minutes"] == 35
    assert top["rank"] == 1

    # 4. Department Trend
    res_trend = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-08")
    assert res_trend.status_code == 200, res_trend.text
    t_data = res_trend.json()
    assert len(t_data["items"]) == 8  # 8 calendar days
    # Check gap filling: 2026-07-01 is filled with 0 present_count
    d1 = t_data["items"][0]
    assert d1["date"] == "2026-07-01"
    assert d1["is_working_day"] is True
    assert d1["present_count"] == 0.0
    assert d1["attendance_rate"] == 0.0


def test_business_rules_boundaries():
    emp_code = "EMP9905"
    db.employees.delete_one({"emp_code": emp_code})
    db.employees.insert_one({
        "emp_code": emp_code,
        "name": "Boundary Tester",
        "email": "boundary@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(tz=UTC)
    })

    # Exact grace period: 09:40:00 IST -> late_minutes must be 0 (R2)
    dt_grace = datetime(2026, 8, 3, 4, 10, 0, tzinfo=UTC)  # 09:40:00 IST
    ms_grace = int(dt_grace.timestamp() * 1000)
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": "2026-08-03"})

    res = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms_grace})
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 0

    # Overtime threshold: 29 minutes after 18:30 -> 18:59:00 IST -> overtime must be 0 (R3)
    dt_ot29 = datetime(2026, 8, 3, 13, 29, 0, tzinfo=UTC)  # 18:59:00 IST
    ms_ot29 = int(dt_ot29.timestamp() * 1000)
    res_out = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms_ot29})
    assert res_out.status_code == 200
    assert res_out.json()["overtime_minutes"] == 0


def test_concurrency_punch_in_and_punch_out():
    import threading

    emp_code = "EMP9906"
    db.employees.delete_one({"emp_code": emp_code})
    db.employees.insert_one({
        "emp_code": emp_code,
        "name": "Concurrent Tester",
        "email": "concurrent@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(tz=UTC)
    })
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": "2026-08-04"})

    dt_in = datetime(2026, 8, 4, 4, 0, 0, tzinfo=UTC)
    ms_in = int(dt_in.timestamp() * 1000)

    results_in = []
    def do_punch_in():
        r = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms_in})
        results_in.append(r.status_code)

    threads = [threading.Thread(target=do_punch_in) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one 201, rest 409
    assert results_in.count(201) == 1
    assert results_in.count(409) == 4

    # Now concurrent punch-outs
    dt_out = datetime(2026, 8, 4, 13, 0, 0, tzinfo=UTC)
    ms_out = int(dt_out.timestamp() * 1000)

    results_out = []
    def do_punch_out():
        r = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms_out})
        results_out.append(r.status_code)

    threads_out = [threading.Thread(target=do_punch_out) for _ in range(5)]
    for t in threads_out:
        t.start()
    for t in threads_out:
        t.join()

    # Exactly one 200, rest 409
    assert results_out.count(200) == 1
    assert results_out.count(409) == 4
