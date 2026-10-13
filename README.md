# Employee Attendance & Analytics API

Backend service for an **engineering assignment submission**: record attendance (punch-in/out), apply IST shift and grace-period rules, allow HR **regularization** with an audit trail, and expose **analytics** built on MongoDB aggregation pipelines.

- **Stack:** Python 3.11+, FastAPI, PyMongo, MongoDB 6.0+
- **Contract:** [`candidate_kit/openapi.yaml`](candidate_kit/openapi.yaml) (paths, status codes, field names)
- **Data model:** [`candidate_kit/DATA_MODEL.md`](candidate_kit/DATA_MODEL.md) (collections, types, sample documents)

All application code lives in **`app/main.py`**, as required by the assignment.

---

## What it does

| Area | Endpoints | Notes |
|------|-----------|--------|
| System | `GET /health` | 200 only if MongoDB responds to `ping`; otherwise 503 |
| Employees | `POST /employees`, `GET /employees` | Unique `emp_code` (DB unique index); pagination |
| Attendance | `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, `PATCH /attendance/{emp_code}/{date}` | Rules R1–R5 on derived fields; `history` only for manual corrections |
| Analytics | `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend` | Uses **stored** `late_minutes`, `work_hours`, etc. (not recomputed from punches) |
| Admin | `GET /admin/explain/{endpoint}` | `executionStats` explain for the same query/aggregation each endpoint runs |

**Business rules (R1–R10)** are defined in the OpenAPI description (not repeated here). Highlights:

- Times in API requests/responses are **epoch milliseconds**; calendar dates are `YYYY-MM-DD` in **IST**.
- **R1:** Attendance `date` from punch-in IST; overnight shifts when `shift_end <= shift_start`.
- **R2–R5:** `late_minutes`, `work_hours` (half-up), `overtime_minutes` (30 min floor), `half_day` (< 4.50 h).
- **R6–R7:** Presence vs leave; working days Mon–Fri from `joined_on`.
- **R10:** Pagination `page` ≥ 1, `page_size` ≤ 100.

---

## Prerequisites

- **Python** 3.11 or newer
- **MongoDB** 6.0+ (local `mongod`, Docker, or Atlas)
- **Git** (for submission)

Optional for tests: `pytest`, `httpx` (see [Testing](hashtag#testing)).

---

## Quick start

```bash
git clone <your-repo-url>
cd employee-attendance # or your clone directory

python3 -m venv .venv
source .venv/bin/activate # Windows: .venv\Scripts\activate

pip install -r requirements.txt
pip install pytest httpx # only if you run test_api.py

export MONGO_URI=mongodb://localhost:27017
export MONGO_DB=attendance_db

python sample_seed.py # loads 6 employees + 9 attendance docs from candidate_kit/sample_data

uvicorn app.main:app --port 8000 --reload
```

- Interactive docs: [http://localhost:8000/docs](http://localhost:8000/docs)
- OpenAPI JSON: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

Indexes are created **once at startup** (idempotent).

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

You may use a local `.env` file for development; **`load_dotenv()` does not override** variables already set in the shell. Do **not** commit `.env` or secrets (see `.gitignore`).

---

## Try a few requests

After seeding and starting the server:

```bash
# Health
curl -s http://localhost:8000/health

# List attendance for one employee
curl -s "http://localhost:8000/attendance?emp_code=EMP0001&page=1&page_size=5"

# Monthly summary (July 2026 sample data)
curl -s "http://localhost:8000/analytics/employees/EMP0001/monthly?month=2026-07"

# Department trend (gap-filled days via $densify in aggregation)
curl -s "http://localhost:8000/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10"

# Explain plan for list query (grader expects IXSCAN, not COLLSCAN)
curl -s "http://localhost:8000/admin/explain/attendance_list?page=1&page_size=20"
```

Punch-in example (epoch ms required; omit `punched_at` to use server time):

```bash
curl -s -X POST http://localhost:8000/attendance/punch-in \
 -H "Content-Type: application/json" \
 -d '{"emp_code":"EMP0002","status":"PRESENT"}'
```

---

## Testing

[`test_api.py`](test_api.py) uses FastAPI’s **`TestClient`** (in-process). You do **not** need `uvicorn` running, but MongoDB must be up.

```bash
source .venv/bin/activate
export MONGO_URI=mongodb://localhost:27017
export MONGO_DB=attendance_db

python sample_seed.py # recommended before a full run; tests expect seeded July 2026 data

pytest test_api.py -v # 10 tests: CRUD, rules, analytics, explain, concurrency
pytest test_api.py -v -k health
pytest test_api.py -v --tb=short
```

Tests create/update employee **`EMP8888`** and some **August 2026** attendance rows. Re-seed with `python sample_seed.py` to reset the sample dataset.

---

## MongoDB layout

| Collection | Purpose |
|------------|---------|
| `employees` | Profile, shift times (`HH:MM` IST), `joined_on`, `created_at` |
| `attendance_logs` | One document per (`emp_code`, `date`); derived fields + `history` |

**Indexes created at startup:**

- `employees`: unique `emp_code`; `(department, joined_on)` for analytics filters
- `attendance_logs`: unique `(emp_code, date)` (punch-in concurrency); `(date ↓, emp_code ↑)` for listing; `(emp_code, date)` for per-employee ranges

---

## Repository layout

```
app/main.py # Full implementation (only code file required at submit)
requirements.txt
README.md
REVIEW.md # Review of starter defects (candidate_kit/app/main.py)
DECISIONS.md # Design choices (indexes, races, ties, headcount)
sample_seed.py # Loads candidate_kit/sample_data/*.json
test_api.py # Local integration tests (not required for submit)
candidate_kit/ # Official kit: openapi.yaml, DATA_MODEL.md, samples, PROBLEM_STATEMENT.docx
```

---

## Assignment deliverables

This project is structured as an assignment submission:

- Public Git repo with `app/main.py`, `requirements.txt`, `README.md`, `REVIEW.md`, `DECISIONS.md`
- No `.env`, secrets, or Dockerfile in the repo
- Run command: `uvicorn app.main:app --port 8000`

For grading details and evaluation criteria, see `candidate_kit/PROBLEM_STATEMENT.docx` and the assignment email.

---
