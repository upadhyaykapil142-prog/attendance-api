# Employee Attendance & Analytics API

A FastAPI + MongoDB service for managing employees, tracking daily attendance (punch-in / punch-out), regularizing records with an audit trail, and producing attendance analytics.

## Features

- Employee management with unique `emp_code` and per-employee shift timings
- Punch-in / punch-out with race-safe writes (unique indexes and conditional updates)
- Overnight shift support (attendance date follows the punch-in, in IST)
- Regularization (`PATCH`) with a full change history (who, when, why, what changed)
- Analytics: monthly employee report, department summary, late-arrival leaderboard

## Requirements

- Python 3.10+
- MongoDB 5.0+ (6.0+ recommended). The leaderboard uses `$setWindowFields`, which needs 5.0 or newer.
- Dependencies listed in `requirements.txt`

## Configuration

Configuration is read from environment variables. A local `.env` file is loaded for convenience, and real environment variables take precedence.

| Variable    | Default                     | Description               |
|-------------|-----------------------------|---------------------------|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB`  | `attendance_db`             | Database name             |

Example `.env`:

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Never commit credentials or `.env` files.

## Run locally

```bash
# 1. Create and activate a virtual environment
python -m venv venv
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# macOS / Linux:
source venv/bin/activate

# 2. Install dependencies
python -m pip install -r requirements.txt

# 3. Start MongoDB (example using Docker)
docker run -d --name mongo -p 27017:27017 mongo:7

# 4. Start the API
python -m uvicorn app.main:app --reload --port 8000
```

Then open:

- Interactive docs: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

> Tip: using `python -m pip` and `python -m uvicorn` also works on machines where Windows Application Control blocks the `.exe` launchers.

Indexes are created automatically at startup.

## Sample data

```bash
python sample_seed.py
```

Sample files are in `sample_data/` (`employees.json`, `attendance_logs.json`).

## API overview

| Method | Path                                      | Description                                  |
|--------|-------------------------------------------|----------------------------------------------|
| GET    | `/health`                                 | Service and MongoDB connectivity check       |
| POST   | `/employees`                              | Create an employee                           |
| GET    | `/employees`                              | List employees (filter by department, paged)|
| POST   | `/attendance/punch-in`                    | Record a punch-in                            |
| POST   | `/attendance/punch-out`                   | Record a punch-out                           |
| GET    | `/attendance`                             | List attendance (emp_code, date range, status, paged) |
| PATCH  | `/attendance/{emp_code}/{day}`            | Regularize a record (audited)                |
| GET    | `/analytics/employees/{emp_code}/monthly` | Monthly report for one employee              |
| GET    | `/analytics/departments/summary`          | Monthly summary per department               |
| GET    | `/analytics/leaderboard/late`             | Top late arrivals for a month                |

The full contract is in `openapi.yaml` and served live at `/openapi.json`.

### Example requests

Create an employee:

```bash
curl -X POST http://127.0.0.1:8000/employees \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","name":"Test User","email":"test@example.com","department":"Engineering","joined_on":"2026-01-01"}'
```

Punch in (current time) and punch out:

```bash
curl -X POST http://127.0.0.1:8000/attendance/punch-in  -H "Content-Type: application/json" -d '{"emp_code":"EMP0001"}'
curl -X POST http://127.0.0.1:8000/attendance/punch-out -H "Content-Type: application/json" -d '{"emp_code":"EMP0001"}'
```

Monthly analytics:

```bash
curl "http://127.0.0.1:8000/analytics/employees/EMP0001/monthly?month=2026-11"
```

## Business rules

- **Timestamps** are exchanged as epoch milliseconds (UTC). Dates are computed in IST (UTC+5:30).
- **Attendance date** is the IST date of the punch-in. For overnight shifts, a punch-in before the shift end belongs to the previous day.
- **Late**: counted only when the punch-in is more than 10 minutes after shift start; late minutes are then measured from shift start.
- **Overtime**: counted only when the punch-out is at least 30 minutes after shift end.
- **Half day**: worked hours below 4.5.
- **Punch-out** must be after punch-in and within 24 hours.
- **Working days** are Monday to Friday, counted from the join date for mid-month joiners. Half days count as 0.5 present.
- **Statuses**: `PRESENT`, `WFH`, `ON_DUTY` (presence); `ABSENT`, `LEAVE` (no punch times allowed).
- **Regularization** recomputes all derived fields, rejects no-op changes, and uses optimistic concurrency (`409` if the record changed underneath).

## Testing

```bash
python -m pytest
```

## Deployment

The app is a stateless web service, so it can run on any host that supports Python. It needs a reachable MongoDB, and MongoDB Atlas (free tier) works well.

### 1. Database (MongoDB Atlas)

1. Create a cluster and a database user.
2. Under Network Access, allow your host's outbound IPs (or `0.0.0.0/0` for testing only).
3. Copy the connection string for use as `MONGO_URI`.

### 2. Web service (example: Render)

1. Push the repository to GitHub (make sure `.env` and `venv/` are in `.gitignore`).
2. Create a new **Web Service** from the repo.
3. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Health check path:** `/health`
4. Add environment variables `MONGO_URI` and `MONGO_DB`.
5. Deploy, then open `https://<your-service>.onrender.com/docs`.

The same build and start commands work on Railway, Fly.io, Azure App Service and similar platforms.

### Docker (optional)

```dockerfile
FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t attendance-api .
docker run -p 8000:8000 -e MONGO_URI="<your-uri>" -e MONGO_DB=attendance_db attendance-api
```

## Project structure

```
attendance-api/
├── app/
│   ├── __init__.py
│   └── main.py          # FastAPI app, models, endpoints, analytics pipelines
├── sample_data/         # Sample employees and attendance logs
├── sample_seed.py       # Loads sample data into MongoDB
├── test_helpers.py      # Unit tests for helper functions
├── openapi.yaml         # API contract
├── DATA_MODEL.md        # Collections, fields and indexes
├── DECISIONS.md         # Design decisions
├── requirements.txt
└── README.md
```
