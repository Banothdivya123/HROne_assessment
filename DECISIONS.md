# DECISIONS.md

1. **Indexes.** I created four indexes:
   - **`employees.emp_code`** (unique): enforces uniqueness at the database level so concurrent duplicate creates yield exactly one 201 and the rest 409 via `DuplicateKeyError`, and enables fast lookups by `emp_code` for every punch/attendance/analytics call.
   - **`employees.department`**: speeds up department-filtered employee queries and the department summary aggregation's `$lookup`.
   - **`attendance_logs.(emp_code, date)`** (unique compound): serves as the natural key for attendance records, prevents duplicate punch-ins atomically, and supports fast single-record lookups for punch-out and regularize. Also backs the `employee_monthly` aggregation's `$match`.
   - **`attendance_logs.(date DESC, emp_code ASC)`**: supports the `list_attendance` sort order and the date-range filters used by all analytics queries.
   I considered a separate index on `attendance_logs.status` but rejected it because status is low-cardinality and MongoDB wouldn't benefit from it; the compound date index already narrows results efficiently.

2. **Punch-in race.** Two identical requests arrive simultaneously. Both find the employee, both compute the same attendance date, both attempt `insert_one`. The unique compound index on `(emp_code, date)` guarantees exactly one insert succeeds. The loser hits a `DuplicateKeyError`, which my code catches and returns 409. No check-then-insert race is possible.

3. **Ties.** I use standard competition ranking: if two employees share the same `total_late_minutes`, they get the same rank, and the next rank is skipped (e.g. 1, 2, 2, 4). The `limit` filter checks `rank <= limit`, so if two people tie at the cutoff rank, both are included — the response can contain more rows than `limit`. Within the same rank, rows are sorted by `emp_code` ascending for determinism.

4. **Headcount.** The department summary starts by aggregating `employees` with `joined_on <= last_day_of_month`, grouped by department. This counts every employee regardless of whether they have any attendance logs. The attendance aggregation runs separately and is joined by department. Departments with headcount > 0 but zero logs still appear with zeroed-out attendance metrics.

5. **One thing I would change** at 100× scale: I would shard the `attendance_logs` collection by `emp_code` (hashed) to distribute writes and reads across multiple servers. The current single-node design handles ~100k records but would bottleneck at tens of millions. Sharding by `emp_code` keeps each employee's records on the same shard, so punch-in/out and per-employee analytics stay single-shard queries. Cross-shard department aggregations would use MongoDB's scatter-gather, which is acceptable for analytics workloads.
