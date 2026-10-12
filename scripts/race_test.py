from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import secrets
import time

import httpx

BASE_URL = "http://127.0.0.1:8000"
THREADS = 20
TIMEOUT = 15.0


def post_request(path, payload):
    """Send one request and return its HTTP status and response body."""
    try:
        response = httpx.post(
            f"{BASE_URL}{path}",
            json=payload,
            timeout=TIMEOUT,
        )
        try:
            body = response.json()
        except ValueError:
            body = response.text

        return response.status_code, body
    except Exception as exc:
        return 0, str(exc)


def run_race(label, path, payload, success_status):
    """Run concurrent requests and require exactly one successful update."""
    print(f"\n--- {label}: {THREADS} parallel requests ---")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [
            pool.submit(post_request, path, payload)
            for _ in range(THREADS)
        ]
        results = [future.result() for future in futures]

    counts = Counter(status for status, _ in results)
    print("HTTP status counts:", dict(sorted(counts.items())))

    unexpected = [
        (status, body)
        for status, body in results
        if status not in (success_status, 409)
    ]

    if (
        counts[success_status] != 1
        or counts[409] != THREADS - 1
        or unexpected
    ):
        print("FAILED. Unexpected responses:")
        for status, body in results:
            if status not in (success_status, 409):
                print(f"  HTTP {status}: {body}")

        raise SystemExit(1)

    print(
        f"PASSED: exactly one {success_status}, "
        f"nineteen 409s, zero unexpected responses."
    )


def get_existing_employee_codes():
    """Read employee codes using the paginated employee-list endpoint."""
    codes = set()
    page = 1
    page_size = 100

    while True:
        response = httpx.get(
            f"{BASE_URL}/employees",
            params={"page": page, "page_size": page_size},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        codes.update(item["emp_code"] for item in data["items"])

        if page * page_size >= data["total"]:
            return codes

        page += 1


def main():
    # Confirm the API and MongoDB are responding before the tests.
    response = httpx.get(f"{BASE_URL}/health", timeout=TIMEOUT)
    response.raise_for_status()
    print("Health check:", response.json())

    # Generate an unused employee code for this run.
    existing_codes = get_existing_employee_codes()

    while True:
        emp_code = f"EMP{secrets.randbelow(900000) + 100000}"
        if emp_code not in existing_codes:
            break

    employee = {
        "emp_code": emp_code,
        "name": "Concurrency Test Employee",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": date.today().isoformat(),
    }

    # Race 1: create the same employee concurrently.
    run_race(
        "POST /employees",
        "/employees",
        employee,
        success_status=201,
    )

    # Use a whole-second timestamp shared by all punch-in requests.
    punched_at = int(time.time()) * 1000

    punch_payload = {
        "emp_code": emp_code,
        "punched_at": punched_at,
        "status": "PRESENT",
    }

    # Race 2: only one punch-in can create the employee's daily record.
    run_race(
        "POST /attendance/punch-in",
        "/attendance/punch-in",
        punch_payload,
        success_status=201,
    )

    # Punch out one hour later: valid and safely within the 24-hour limit.
    punch_out_payload = {
        "emp_code": emp_code,
        "punched_at": punched_at + 60 * 60 * 1000,
    }

    # Race 3: exactly one request may close the attendance record.
    run_race(
        "POST /attendance/punch-out",
        "/attendance/punch-out",
        punch_out_payload,
        success_status=200,
    )

    print(f"\nTest employee: {emp_code}")
    print("All three concurrency tests passed.")
    print("Test records remain in MongoDB.")


if __name__ == "__main__":
    main()
