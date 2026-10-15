# Employee Attendance & Analytics API

FastAPI and MongoDB API for employee management, attendance tracking, and attendance analytics.

## Requirements
- Python 3.10+
- MongoDB 6.0+
- Dependencies in requirements.txt

## Configuration
Set MONGO_URI and MONGO_DB environment variables. Never commit credentials or .env files.

## Run locally
    pip install -r requirements.txt
    uvicorn app.main:app --port 8000

API documentation: /docs
