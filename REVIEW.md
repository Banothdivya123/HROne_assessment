# REVIEW.md

List every defect found in the starter `app/main.py` (helpers and endpoints):

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | No actual MongoDB ping — just returns `{"status": "ok"}` even if DB is down. | Start the app with MongoDB stopped; `/health` still returns 200. | Added `client.admin.command("ping")` with a try/except that raises `HTTPException(503)`. |
| 2 | `create_employee()` line 79 | Race condition: check-then-insert (`find_one` then `insert_one`) allows two concurrent creates with the same `emp_code` to both succeed. | Fire two simultaneous POST requests with the same `emp_code`; both get 201. | Use a unique index on `emp_code` and catch `DuplicateKeyError` for 409. |
| 3 | `create_employee()` line 82 | `created_at` uses `datetime.now()` (naive, local time) instead of UTC. | Compare stored `created_at` across servers in different time zones; values differ. | Use `datetime.now(timezone.utc)`. |
| 4 | `create_employee()` | No input validation: `emp_code` format, email pattern, shift times, `shift_start == shift_end`, string lengths. | Send `emp_code: "XYZ"` or `shift_start: "25:99"` — accepted without error. | Added regex validators for emp_code, email, shift times; length checks; shift_start != shift_end check. |
| 5 | `create_employee()` | Returns raw MongoDB doc with `created_at` as a datetime, not epoch milliseconds. `_id` removal is fragile. | Response `created_at` is an ISO string instead of an integer. | Added `_format_employee()` that converts `created_at` to epoch ms and excludes `_id`. |
| 6 | `list_employees()` line 93 | Skip is `page * page_size` instead of `(page - 1) * page_size`. | Request page 1 → first `page_size` results are skipped. | Fixed to `(page - 1) * page_size`. |
| 7 | `list_employees()` line 94 | `total` counts all documents (`count_documents({})`) instead of the filtered set. | Filter by department; `total` still shows the full count. | Changed to `count_documents(q)` with the filter applied. |
| 8 | `list_employees()` | No sort order. Contract requires ascending by `emp_code`. | Results come in insertion order, not alphabetical by `emp_code`. | Added `.sort("emp_code", ASCENDING)`. |
| 9 | `list_employees()` | Returns raw MongoDB docs with `created_at` as datetime and `_id` visible. | `_id` ObjectId appears in responses. | Project `_id: 0` and format through `_format_employee()`. |
| 10 | `punch_in()` line 102 | `datetime.fromtimestamp(…)` returns naive local time, not UTC. | Punch times shift by the server's UTC offset; IST attendance date is wrong. | Use `datetime.fromtimestamp(ms/1000, tz=timezone.utc)` and truncate to seconds (R1). |
| 11 | `punch_in()` line 103 | Attendance date uses the server-local date (`ts.date()`), not the IST date per R1. Overnight shifts are not handled. | An overnight-shift employee (22:00–06:00) punching in at 23:30 UTC gets the wrong date. | Implemented `_compute_attendance_date()` with IST conversion and overnight logic. |
| 12 | `punch_in()` line 104 | Race condition: check-then-insert for duplicate punch-ins. | Two concurrent punch-ins for the same date both get 201. | Use the unique index `(emp_code, date)` and catch `DuplicateKeyError`. |
| 13 | `punch_in()` | No 404 check for unknown employee. | Punch in with a non-existent `emp_code` → unhandled `NoneType` error on `emp["shift_start"]`. | Added `if not emp: raise HTTPException(404)`. |
| 14 | `punch_in()` line 119 | Response includes `"id"` (stringified ObjectId) — not part of the contract. | Grader sees an extra field and the response schema doesn't match `AttendanceRecord`. | Removed; return through `_format_attendance()`. |
| 15 | `punch_in()` | `punch_in`/`punch_out` returned as datetimes, not epoch milliseconds. | Response contains ISO-formatted strings instead of integers. | `_format_attendance()` converts all instants to epoch ms. |
| 16 | `compute_late_minutes()` line 35-36 | Uses `punch_in.replace(hour=…)` which only works when `punch_in` is in IST. The code uses naive (or UTC) datetimes — so the shift-start comparison is against the wrong hour. | Employee with shift 09:30 IST punches in at 09:28 IST (03:58 UTC). `replace(hour=9, minute=30)` on the UTC datetime computes 09:30 UTC, yielding negative diff. | Rewrote: convert `punch_in` to IST first, then compare against shift start. |
| 17 | `compute_overtime()` line 46 | Same IST issue: `fromisoformat(date_str).replace(hour=…, tzinfo=IST)` constructs the correct IST time, but `punch_out` might be in a different timezone (naive or UTC), causing wrong diff. Does not handle overnight shifts (shift_end on next day). | Overnight shift employee: shift_end 06:00 → `end` is set to 06:00 on the start date instead of the next day. | Rewrote with proper IST handling and overnight-shift detection. |
| 18 | `compute_overtime()` line 47 | No 30-minute minimum threshold — returns any positive overtime. | Employee leaves 10 min late → `overtime_minutes` = 10, but R3 says < 30 → 0. | Added `>= 30` check. |
| 19 | `compute_work_hours()` line 41 | Uses Python's `round()` which is banker's rounding, not half-up. | `round(8.425, 2)` → 8.42, but R4 requires 8.43. | Use `Decimal` with `ROUND_HALF_UP`. |
| 20 | `list_attendance()` lines 144-149 | Loads entire collection into memory, then sorts/paginates in Python. | With 100k records, the endpoint is very slow and uses excessive memory. | Use MongoDB `.sort().skip().limit()` with an index on `(date DESC, emp_code ASC)`. |
| 21 | `list_attendance()` line 149 | Exposes `_id` as `"id"` — not in the contract. | Response has an extra `id` field. | Removed; project `_id: 0` and use `_format_attendance()`. |
| 22 | `list_attendance()` | No validation that `date_from <= date_to`. | Sending `date_from > date_to` silently returns empty, instead of 422. | Added validation with 422. |

**Not a defect:**
- Using `load_dotenv()` at module level: acceptable since the spec says "a local .env is fine" and real env vars override.
- `PunchInIn.status` as plain `str` with default `"PRESENT"`: not a defect per se, but needs runtime validation; the starter just didn't validate.
