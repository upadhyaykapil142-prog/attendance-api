# DECISIONS.md

1. **Indexes.** I use a unique index on `employees.emp_code` to prevent duplicate employee codes, plus `(department, emp_code)` for department filtering and sorted employee lists. The `joined_on` index supports employee eligibility by date. On `attendance_logs`, the unique `(emp_code, date)` index guarantees one record per employee per attendance day and supports monthly lookups. The `(date, emp_code)` and `(status, date, emp_code)` indexes support attendance date and status filters. I would avoid adding overlapping indexes without measuring their benefit because every index increases write cost and storage.

2. **Punch-in race.** Both requests may initially attempt to create the same attendance record. The unique compound index on `(emp_code, date)` is the final authority: one insert succeeds with HTTP 201; the duplicate-key failure is translated to HTTP 409. A check-then-insert alone would not safely handle this race.

3. **Ties.** The leaderboard uses ranking semantics where equal late-minute totals share a rank and the next rank is skipped. Employees tied at the cutoff are handled according to ranking before the limit is applied, with `emp_code` providing deterministic ordering.

4. **Headcount.** The department summary must start from eligible employees, including those who joined on or before the period end, and preserve employees without matching attendance logs. Otherwise, a log-first aggregation would omit them.

5. **Scaling.** At 100 times the data, I would benchmark representative queries, inspect execution plans, and consider workload-based indexing, pagination limits, and archiving or partitioning strategy where appropriate.
