# DECISIONS

1. **Indexes.** I created a unique index on `employees.emp_code` to prevent duplicate employee codes, and a unique compound index on `attendance_logs.emp_code` and `date` to guarantee one attendance record per employee per day. Other indexes support department and joining-date filters, attendance date and status queries, and employee punch history. I kept indexes aligned with the queries used by the API instead of adding indexes to every field.

2. **Punch-in race.** If two identical punch-in requests arrive together, both may check the employee and calculate the attendance date. The unique `(emp_code, date)` index allows only one insert to succeed. MongoDB rejects the other insert with a duplicate-key error, which the API converts to HTTP 409.

3. **Ties.** The late leaderboard uses MongoDB `$rank` on total late minutes. Employees tied at the cutoff receive the same rank, and all employees whose rank is within the requested limit are returned. Therefore, the response can contain more entries than the limit when there is a tie.

4. **Headcount.** Department summary starts from employees who joined on or before the reporting period ends, then looks up their attendance logs. Preserving employees without matching logs ensures employees with zero attendance records are still included in headcount.

5. **Scaling.** For 100 times more data, I would review query plans and index usage against realistic workloads, then optimize the slowest aggregations. I would also consider caching frequently requested analytics and processing large reports asynchronously.