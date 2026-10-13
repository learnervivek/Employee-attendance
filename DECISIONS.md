# DECISIONS.md

1. **Indexes.**
   On `employees`, unique `{emp_code: 1}` guarantees identity and duplicate prevention; `{department: 1, emp_code: 1}` covers filtered listing; `{joined_on: 1, department: 1}` and `{department: 1, joined_on: 1}` back department summary and daily trend headcount lookups.
   On `attendance_logs`, unique `{emp_code: 1, date: 1}` enforces one record per employee-day and accelerates lookups for regularization and monthly summaries; `{date: -1, emp_code: 1}` backs paginated listing; `{date: 1, late_minutes: 1}` backs the late leaderboard; `{date: 1}` backs the trend match; `{emp_code: 1, punch_in: -1}` enables instant punch-out lookups.
   We considered and rejected a standalone index on `status` due to low cardinality (5 values), as queries are already selectively bound by date or employee.

2. **Punch-in race.**
   Both requests compute the attendance date and invoke `db.attendance_logs.insert_one(doc)`. WiredTiger enforces the unique constraint on `(emp_code, date)`: one write succeeds (201 Created), while the other raises `DuplicateKeyError`. Our handler catches this and returns 409 Conflict. No check-then-insert race window exists.

3. **Ties.**
   Our aggregation uses MongoDB's `$rank` window function for standard competition ranking (1, 2, 2, 4), followed by `{rank: {$lte: limit}}`. When employees tie at the cutoff rank, both are returned (e.g. 11 rows for limit 10), sorted by `emp_code` ascending. Truncating ties would be arbitrary and violates competition ranking rules.

4. **Headcount.**
   The aggregation originates on `employees` (filtering `joined_on <= end_of_month`), performing a `$lookup` into `attendance_logs` with `preserveNullAndEmptyArrays: true`. Employees with zero logs yield null log fields, but still contribute 1 to `$group: {headcount: {$sum: 1}}`.

5. **100x data.**
   At 10M+ attendance records, we would maintain pre-aggregated daily rollups via MongoDB change streams to keep analytics O(1), and shard `attendance_logs` on a hashed or compound key like `(date, emp_code)`.
