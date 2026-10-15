# DECISIONS.md

1. **Indexes.** Employees has a unique `emp_code` index to prevent duplicate employees. Attendance has a unique `(emp_code, date)` index to enforce one record per employee per day. The `(emp_code, punch_in descending)` index supports finding the latest eligible punch-in for punch-out. The `(date descending, emp_code ascending)` index supports the attendance list sort. I considered a standalone `status` index but rejected it because it would not support the required global sort.

2. **Punch-in race.** Two requests can find the same employee and try to insert attendance for the same date. The unique `(emp_code, date)` index allows only one insert. The other raises `DuplicateKeyError`, which the endpoint catches and returns as `409 Conflict`. This prevents duplicate daily records even when requests arrive together.

3. **Ties.** Pending until the late-arrival leaderboard is implemented.

4. **Headcount.** Pending until the department summary is implemented.

5. **One thing I would change for 100x the data.** I would measure query performance with MongoDB explain plans and consider precomputed summaries for frequently requested analytics, rather than repeatedly recalculating them from raw attendance records.
