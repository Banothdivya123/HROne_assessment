# Employee Attendance & Analytics API

A FastAPI + MongoDB REST API for managing employee attendance, built as an HROne engineering assignment.

## Features

- Employee CRUD with unique `emp_code` enforcement
- Punch-in / punch-out with IST timezone handling and overnight shift support
- Manual attendance corrections (regularize) with full audit trail
- Monthly analytics per employee and department (MongoDB aggregation pipelines)
- Late-comers leaderboard with standard competition ranking
- Daily department attendance trend with 7-day moving average
- Admin explain endpoint for query plan inspection
- Concurrency-safe operations using unique indexes and atomic updates

## Requirements

- Python 3.11+
- MongoDB 6.0+ (tested with 7.0)

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

Set environment variables (or create a `.env` file):
```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Start MongoDB (Docker):
```bash
docker run -d -p 27017:27017 mongo:7
```

Seed sample data:
```bash
python sample_seed.py
```

Run the server:
```bash
uvicorn app.main:app --port 8000 --reload
```

## Testing

```bash
pytest tests/ -v
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Readiness probe |
| POST | `/employees` | Create employee |
| GET | `/employees` | List employees (paginated) |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize attendance |
| GET | `/analytics/employees/{emp_code}/monthly` | Employee monthly summary |
| GET | `/analytics/departments/summary` | Department summary |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Department attendance trend |
| GET | `/admin/explain/{endpoint}` | MongoDB explain output |

## Business Rules

All rules (R1–R10) are implemented per the OpenAPI spec: IST timezone handling, 10-min grace period, 30-min overtime threshold, half-up rounding, half-day detection, Mon–Fri working days, and standard competition ranking for leaderboards.
