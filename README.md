# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or a .env file (not committed)
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). MongoDB 6.0+ is required (`$setWindowFields`, `$rank`, `$dateAdd`, `$lookup` with `localField` + `pipeline`).

## Tests
`pip install pytest && pytest tests/test_rules.py` checks the business rules R1-R5, rounding and validation (no database needed).

## Notes
- Instants are epoch milliseconds in the API and aware UTC datetimes in MongoDB. Rounding is half-up with integer arithmetic (MongoDB's `$round` is banker's rounding), also inside the pipelines.
- The department trend fills missing days inside the database with `$unionWith` (a generated day list), then `$group`, `$lookup` for headcount and `$setWindowFields` for the 7-day moving average.
- Concurrency: unique indexes for create / punch-in, a conditional `find_one_and_update` for punch-out, and a guarded update (history length must be unchanged) for PATCH; the loser of a concurrent correction gets 409.
- `/admin/explain/{endpoint}` runs `explain` (`executionStats`) on the same query / pipeline the endpoint builds.
- Choices where the contract was open: the trend range limit counts days inclusively (92 days allowed, 93 gives 422); the "request changes nothing" check compares status and punch times.
