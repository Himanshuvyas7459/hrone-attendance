# Employee Attendance & Analytics API

A FastAPI and MongoDB application for employee management, attendance tracking, attendance regularization, and analytics.

## Features

- Employee management with filtering and pagination
- Attendance punch-in and punch-out
- Attendance regularization and audit history
- Monthly employee attendance analytics
- Department summaries, late leaderboard, and daily department trends
- MongoDB indexes and query explain endpoints

## Local Setup

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
pip install -r requirements.txt
```

Create a `.env` file using `.env.example` and configure `MONGO_URI` and `MONGO_DB`.

Start the API:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

API documentation: `http://127.0.0.1:8000/docs`

Health check: `http://127.0.0.1:8000/health`

MongoDB 6.0 or newer is required for the aggregation features.

## Repository Contents

- `app/main.py` — API implementation
- `openapi.yaml` — API contract and business rules
- `DATA_MODEL.md` — data model reference
- `sample_data/` and `sample_seed.py` — sample data and seeding utility
- `REVIEW.md` — implementation review
- `DECISIONS.md` — design decisions
- `requirements.txt` — Python dependencies

## Configuration

See `.env.example` for the required environment variable names. Do not commit `.env` or credentials.