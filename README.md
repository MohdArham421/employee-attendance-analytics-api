# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service implementing employee attendance management, punch-in/punch-out with business rules (R1-R10), manual regularization with audit trails, and aggregation-driven analytics.

## Prerequisites

- Python 3.11+
- MongoDB 6.0 or newer (local or Atlas)

## Setup & Running

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment configuration**:
   Set `MONGO_URI` and `MONGO_DB` as environment variables (or in a local `.env`):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```
   *Note: Real environment variables take priority over `.env`.*

3. **Seed sample data (optional)**:
   ```bash
   python sample_seed.py
   ```

4. **Start the application**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

The application automatically creates all necessary unique and compound indexes idempotently at startup.

## Testing

Run the test suite:
```bash
pytest
```

## Structure & Deliverables

- `app/main.py`: Complete application code including models, helper calculations, endpoints, and aggregation pipelines.
- `requirements.txt`: Required packages and dependencies.
- `REVIEW.md`: Comprehensive defect review of the starter code.
- `DECISIONS.md`: Architectural decisions regarding indexing, concurrency, ranking, headcount, and scaling.
