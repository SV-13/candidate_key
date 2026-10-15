# DECISIONS.md

1. **Indexes.** Employees has a unique `emp_code` index to prevent duplicate employees. Attendance has a unique `(emp_code, date)` index to enforce one record per employee per day. The `(emp_code, punch_in descending)` index supports finding the latest eligible punch-in for punch-out. The `(date descending, emp_code ascending)` index supports attendance-list sorting. The `joined_on` index supports monthly eligibility filters for department summaries, while `(department, joined_on)` supports the same filter when a department is specified. The attendance `(emp_code, date)` index also supports monthly aggregation lookups.

2. **Punch-in race.** Two requests can find the same employee and try to insert attendance for the same date. The unique `(emp_code, date)` index allows only one insert. The other raises `DuplicateKeyError`, which the endpoint catches and returns as `409 Conflict`. This prevents duplicate daily records even when requests arrive together.

3. **Ties.** Pending until the late-arrival leaderboard is implemented.

4. **Headcount.** The department summary begins with employees whose `joined_on` is on or before the month's last day, then looks up attendance records. It preserves employees with no logs and groups by employee before department to avoid counting an employee multiple times. Employees who join after the month ends are excluded.

5. **One thing I would change for 100x the data.** I would measure query performance with MongoDB explain plans and consider precomputed summaries for frequently requested analytics, rather than repeatedly recalculating them from raw attendance records.
