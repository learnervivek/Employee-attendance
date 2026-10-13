import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import app, db, client

test_client = TestClient(app)

def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_employees_crud_and_validation():
    # 422 on invalid emp_code
    res = test_client.post("/employees", json={
        "emp_code": "INVALID",
        "name": "Test",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # 422 on shift_start == shift_end
    res = test_client.post("/employees", json={
        "emp_code": "EMP9999",
        "name": "Test",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:00",
        "shift_end": "09:00",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # 409 on duplicate emp_code
    res = test_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Duplicate",
        "email": "dup@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 409

    # 201 on valid create
    db.employees.delete_one({"emp_code": "EMP8888"})
    res = test_client.post("/employees", json={
        "emp_code": "EMP8888",
        "name": "Valid Test",
        "email": "valid@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP8888"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data and "id" not in data

def test_list_employees():
    res = test_client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data and "total" in data
    assert data["page"] == 1
    assert data["page_size"] == 10
    # verify sorted by emp_code
    codes = [e["emp_code"] for e in data["items"]]
    assert codes == sorted(codes)

def test_attendance_list_and_validation():
    # 422 if date_from > date_to
    res = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res.status_code == 422

    res = test_client.get("/attendance?emp_code=EMP0001")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data["items"], list)
    for item in data["items"]:
        assert "_id" not in item and "id" not in item

def test_punch_in_out_and_rules():
    # 404 unknown employee
    res = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0000"})
    assert res.status_code == 404

    # Test punch-in for fresh date
    fresh_date = "2026-08-01"
    db.attendance_logs.delete_one({"emp_code": "EMP8888", "date": fresh_date})
    # Punch in 09:40:01 IST (late > 10 min grace: late = 10 min)
    # 2026-08-01 09:40:01 IST = 2026-08-01 04:10:01 UTC
    t_in = int(datetime(2026, 8, 1, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    res = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP8888",
        "punched_at": t_in,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    record = res.json()
    assert record["date"] == fresh_date
    assert record["late_minutes"] == 10
    assert record["punch_out"] is None
    assert record["work_hours"] is None
    assert record["history"] == []
    assert "_id" not in record and "id" not in record

    # Duplicate punch-in gives 409
    res_dup = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP8888",
        "punched_at": t_in,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # Punch-out: punched_at <= punch_in gives 422
    res_bad_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP8888",
        "punched_at": t_in - 1000
    })
    assert res_bad_out.status_code == 422

    # Punch-out: > 24 hours gives 422
    res_bad_out2 = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP8888",
        "punched_at": t_in + 25 * 3600 * 1000
    })
    assert res_bad_out2.status_code == 422

    # Valid punch out: out at 19:10:00 IST (13:40:00 UTC) -> 40 min overtime
    t_out = int(datetime(2026, 8, 1, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP8888",
        "punched_at": t_out
    })
    assert res_out.status_code == 200
    out_rec = res_out.json()
    assert out_rec["punch_out"] == t_out
    assert out_rec["overtime_minutes"] == 40
    assert out_rec["work_hours"] is not None
    assert out_rec["half_day"] is False

    # Second punch-out gives 409 (already punched out)
    res_out_dup = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP8888",
        "punched_at": t_out + 1000
    })
    assert res_out_dup.status_code == 409

def test_regularize_patch():
    # 404 on unknown employee
    res = test_client.patch("/attendance/EMP0000/2026-08-01", json={
        "reason": "Correcting error",
        "regularized_by": "admin"
    })
    assert res.status_code == 404

    # 422 when changing nothing
    res = test_client.patch("/attendance/EMP8888/2026-08-01", json={
        "status": "PRESENT",
        "reason": "Same status",
        "regularized_by": "admin"
    })
    assert res.status_code == 422

    # 422 when setting status ABSENT with punch times
    res = test_client.patch("/attendance/EMP8888/2026-08-01", json={
        "status": "ABSENT",
        "punch_in": 1783312500000,
        "reason": "Setting absent with punch time",
        "regularized_by": "admin"
    })
    assert res.status_code == 422

    # Valid regularization: change punch_in to 09:30:00 IST (04:00:00 UTC) -> late becomes 0
    t_in_corrected = int(datetime(2026, 8, 1, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = test_client.patch("/attendance/EMP8888/2026-08-01", json={
        "punch_in": t_in_corrected,
        "reason": "Biometric system delay fixed",
        "regularized_by": "hr_manager"
    })
    assert res.status_code == 200
    patched = res.json()
    assert patched["late_minutes"] == 0
    assert len(patched["history"]) == 1
    h = patched["history"][0]
    assert h["by"] == "hr_manager"
    assert h["reason"] == "Biometric system delay fixed"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 10
    assert h["changes"]["late_minutes"]["to"] == 0

def test_analytics_endpoints():
    # 1. Employee Monthly
    res = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["attendance_pct"] == 8.70

    # 2. Department Summary
    res = test_client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    summary = res.json()
    assert summary["month"] == "2026-07"
    assert len(summary["items"]) >= 3
    eng = next(i for i in summary["items"] if i["department"] == "Engineering")
    assert eng["headcount"] >= 2
    assert eng["present_days"] == 3.0
    assert eng["avg_work_hours"] == 9.04

    # 3. Late Leaderboard
    res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    lb = res.json()
    assert lb["month"] == "2026-07"
    assert len(lb["items"]) >= 1
    top = lb["items"][0]
    assert top["rank"] == 1
    assert top["emp_code"] == "EMP0003"
    assert top["total_late_minutes"] == 35

    # 4. Department Trend
    res = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10")
    assert res.status_code == 200
    trend = res.json()
    assert trend["department"] == "Engineering"
    assert len(trend["items"]) == 5 # 5 days
    for day in trend["items"]:
        assert "date" in day
        assert "is_working_day" in day
        assert "headcount" in day
        assert "present_count" in day
        assert "attendance_rate" in day
        assert "moving_avg_7d" in day

def test_admin_explain():
    endpoints = [
        ("attendance_list", {}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-05"}),
    ]
    for ep, params in endpoints:
        res = test_client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        exp_str = str(data["explain"])
        assert "IXSCAN" in exp_str
        assert "COLLSCAN" not in exp_str


def test_concurrency_punch_in():
    import concurrent.futures
    db.attendance_logs.delete_one({"emp_code": "EMP8888", "date": "2026-08-02"})
    t_in = int(datetime(2026, 8, 2, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch_in():
        with TestClient(app) as c:
            return c.post("/attendance/punch-in", json={
                "emp_code": "EMP8888",
                "punched_at": t_in,
                "status": "PRESENT"
            }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_in) for _ in range(5)]
        results = [f.result() for f in futures]

    assert results.count(201) == 1
    assert results.count(409) == 4


def test_concurrency_punch_out():
    import concurrent.futures
    db.attendance_logs.delete_one({"emp_code": "EMP8888", "date": "2026-08-03"})
    t_in = int(datetime(2026, 8, 3, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP8888",
        "punched_at": t_in,
        "status": "PRESENT"
    })
    t_out = t_in + 8 * 3600 * 1000

    def do_punch_out():
        with TestClient(app) as c:
            return c.post("/attendance/punch-out", json={
                "emp_code": "EMP8888",
                "punched_at": t_out
            }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        results = [f.result() for f in futures]

    assert results.count(200) == 1
    assert results.count(409) == 4

