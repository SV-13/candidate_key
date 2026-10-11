# REVIEW.md

This review records defects reproduced against the untouched starter API. The “How you fixed it” column currently records the planned correction; it must be updated after the code change and regression test have been completed.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` — pagination offset | The offset is calculated as `page * page_size`. Because pages start at 1, page 1 incorrectly skips the first page of results. | With the seeded employees, `GET /employees?page=1&page_size=2` returns `EMP0003` and `EMP0004`; the first page should return `EMP0001` and `EMP0002` when sorted by `emp_code` ascending. | Planned: calculate `skip` as `(page - 1) * page_size`; add a regression test for page 1. |
| 2 | `list_employees` — filtered count | `total` is computed with `count_documents({})`, ignoring the department filter used for `items`. | `GET /employees?department=Sales&page=1&page_size=2` returns `total: 6`, although only two seeded employees belong to Sales. Because of the offset bug, the `items` array is also empty. | Planned: call `count_documents(q)` with the same filter used by the listing query; test filtered and unfiltered totals. |
| 3 | `punch_in` — employee lookup | The endpoint doesn't return a `404` when `emp_code` doesn't identify an employee; it later indexes `emp["shift_start"]` when `emp` is `None`. | `POST /attendance/punch-in` with `{"emp_code":"EMP9999"}` returns `500 Internal Server Error`; the contract requires `404 Not Found`. | Planned: check `emp is None` immediately after lookup and raise `HTTPException(404, ...)`; add a regression test for an unknown employee. |
| 4 | `list_employees` — pagination validation | `page` and `page_size` do not enforce the contract's bounds (`page >= 1`, `1 <= page_size <= 100`). | `GET /employees?page=0&page_size=100` and `GET /employees?page=1&page_size=101` return `200`, although these inputs should receive `422`. | Planned: declare validated FastAPI query parameters with `page >= 1` and `1 <= page_size <= 100`; test the invalid boundaries. |
| 5 | `create_employee` — `created_at` response format | `created_at` is created with naive local `datetime.now()` and returned as a datetime string, rather than the required epoch-millisecond integer. The naive datetime can also be stored as the wrong instant when the host uses a non-UTC local timezone. | The employee listing returns values such as `"created_at": "2026-01-05T04:30:00"`; the contract requires an integer epoch timestamp in milliseconds. | Planned: use a consistent aware-UTC instant for storage, and convert API timestamp fields to epoch milliseconds in the response; add a response-format and timezone test. |

## Behaviours checked but not classified as defects

- `GET /` and `GET /favicon.ico` return `404`. This is not currently treated as a defect because the contract defines `/health` as the readiness endpoint and does not require a root page or favicon endpoint.
- `GET /health` returns `200` while MongoDB is running. That single successful response is not a defect by itself; the confirmed defect is that the current handler never pings MongoDB, so it would also return success if the database were unavailable.

## Follow-up review

These five rows are the issues reproduced so far, not the complete review. Add the remaining findings after testing the calculation helpers, timestamp conversion, race conditions, status/date validation, attendance response shape and query performance. After each fix is implemented, replace the planned-correction wording above with the actual fix and its regression test.
