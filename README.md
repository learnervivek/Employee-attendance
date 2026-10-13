# Employee Attendance & Analytics API

A FastAPI and MongoDB service providing punch-in/out tracking with business rules, manual regularization audit trails, and MongoDB aggregation analytics.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (local instance, Docker container, or MongoDB Atlas cluster)

## Setup & Running

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd <repo-name>
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment variables (optional):**
   By default, the application connects to `mongodb://localhost:27017` and database `attendance_db`. You can override these using environment variables:
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

5. **Load sample data (optional):**
   ```bash
   python sample_seed.py
   ```

6. **Start the server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

All indexes are created automatically and idempotently on startup.

## Testing

Run tests with `pytest`:
```bash
pip install pytest httpx
pytest -v
```

## Structure
- `app/main.py`: Complete application containing models, business rules, CRUD endpoints, aggregation analytics, and explain endpoints.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed audit of defects identified and fixed from the starter template.
- `DECISIONS.md`: Design rationale covering indexes, race condition handling, ranking with ties, headcount calculations, and scaling strategy.
- `DATA_MODEL.md` & `openapi.yaml`: Schema specifications and API contract.
