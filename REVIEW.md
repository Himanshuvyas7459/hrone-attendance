# REVIEW

The following defects were identified in the starter implementation and addressed in the completed API.

| # | Where (function / area) | What is wrong | How you'd notice it | How it was fixed |
|---|---|---|---|---|
| 1 | Employee listing pagination | The page offset was calculated incorrectly, which could skip or repeat records across pages. | Request page 1 and page 2 with the same page size and compare results. | Use `(page - 1) * page_size`, validate page values, and apply the limit in MongoDB. |
| 2 | Employee listing filters and totals | The total count could fail to reflect the active filters. | Filter by department and compare `total` with the matching employees. | Build the same query for `count_documents` and the paginated result. |
| 3 | Punch-in duplicate handling | A check-then-insert approach alone cannot prevent simultaneous requests from creating duplicate attendance records. | Send two punch-in requests for the same employee and date. | Add a unique compound index on `(emp_code, date)` and translate duplicate-key errors to HTTP 409. |
| 4 | Employee lookup in attendance operations | A missing employee could cause an unhandled error instead of a clear API response. | Punch in or out using an unknown employee code. | Check the employee lookup result and return HTTP 404 when it does not exist. |
| 5 | Attendance listing | Loading all matching logs and sorting them in Python does not scale well. | Test with a large number of attendance records. | Filter, sort, skip and limit records in MongoDB; return a filtered total. |
| 6 | Health endpoint | Returning success without checking MongoDB can report healthy when the database is unavailable. | Stop MongoDB and call `/health`. | Ping MongoDB and return HTTP 503 if the database is unavailable. |
| 7 | Time and shift calculations | Local dates, overnight shifts, grace-period boundaries and minute rounding can produce incorrect attendance values. | Test overnight shifts and timestamps around the 10-minute grace boundary. | Use IST for attendance dates and shift calculations, normalize instants to whole seconds, and apply the specified boundaries. |

## Reviewed but not treated as defects

- A punch-in more than ten minutes after shift start is late; exactly ten minutes is not late.
- Overtime is counted only when the time after shift end reaches at least 30 minutes.
- Weekend logs can contribute to late and overtime totals, while present-day counts use weekdays only.
- Employees without attendance logs must still contribute to department headcount.
