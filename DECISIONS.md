# DECISIONS.md

1. **Indexes.**
   - `employees`: `{ emp_code: 1 }` (unique) guarantees business code uniqueness and O(1) lookups; `{ department: 1, joined_on: 1 }` and `{ joined_on: 1 }` optimize department filtering and monthly headcount queries.
   - `attendance_logs`: `{ emp_code: 1, date: 1 }` (unique) enforces the core constraint of one record per employee per day and eliminates punch-in race conditions; `{ date: -1, emp_code: 1 }` supports sorted attendance listing and trend date bounds via `IXSCAN`; `{ emp_code: 1, punch_in: -1 }` accelerates punch-out lookups; `{ date: 1, late_minutes: 1, emp_code: 1 }` enables a covered index scan for late leaderboards.
   - *Rejected:* An index on `{ "history.at": 1 }`, because history is an append-only audit log never queried independently; indexing nested array fields would introduce unnecessary write amplification.

2. **Punch-in race.**
   Both concurrent requests validate inputs, calculate attendance date, and attempt `insert_one`. The database unique compound index on `(emp_code, date)` allows only the first insert to commit (201). The second insert immediately encounters a `DuplicateKeyError`, which is caught and returned as HTTP 409, completely avoiding check-then-insert race conditions.

3. **Ties.**
   The leaderboard computes standard competition ranking (1, 2, 2, 4) using MongoDB's `$setWindowFields` with `$rank`. The cutoff filter `rank <= limit` is evaluated *after* ranking. If a tie occurs at the boundary, all tied employees are included (returning more than `limit` rows), ordered deterministically by `emp_code` ascending.

4. **Headcount.**
   The department summary aggregation originates from the `employees` collection rather than `attendance_logs`. It matches active employees (`joined_on <= end_of_month`) and `$lookup`s their monthly logs. Employees with zero logs are preserved during grouping with `headcount = 1` and an empty log array, correctly including them in headcount.

5. **One thing you would change for 100x data.**
   I would adopt an event-driven pre-aggregation / rollup architecture (incrementally updating daily department stats and monthly employee summaries on punch events or nightly rollups) rather than executing cross-collection pipelines on-the-fly, combined with sharding `attendance_logs` across cluster nodes by `{ emp_code: 1, date: 1 }`.
