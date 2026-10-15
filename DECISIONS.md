# DECISIONS.md

1. **Indexes.** On `attendance_logs`: unique `(emp_code, date)` stops duplicate days and serves per-employee reads, monthly stats and the trend; `(date desc, emp_code)` serves the list sort and leaderboard date range; `(status, date desc, emp_code)` serves the status filter; `(emp_code, punch_in desc)` finds the record to punch out. On `employees`: unique `emp_code`, `(department, joined_on)` and `joined_on` for headcount. I rejected a separate `late_minutes` index: it is not selective and the date range already narrows the scan.
2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique index lets MongoDB accept one; the other gets `DuplicateKeyError`, which I turn into 409. There is no read before the write.
3. **Ties.** I rank with `$rank` first and cut after (`rank <= limit`), so everybody tied at the cutoff is returned, even if that is more than `limit` rows.
4. **Headcount.** The summary starts from `employees` (joined on or before month end), looks up each person's logs, and keeps people with no logs, so each still adds 1.
5. **100x data.** Pre-aggregate daily/monthly totals into a summary collection updated on write, and use keyset pagination instead of `skip`.
