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
    """Send one request and return its status and response body."""
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


def run_race(label, path, payload):
    """Send 20 simultaneous requests and verify one winner."""
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
        if status not in (201, 409)
    ]

    if counts[201] != 1 or counts[409] != THREADS - 1 or unexpected:
        print("FAILED. Unexpected responses:")
        for status, body in results:
            if status not in (201, 409):
                print(f"  HTTP {status}: {body}")

        raise SystemExit(1)

    print("PASSED: exactly one 201, nineteen 409s, zero unexpected responses.")
    return results


def get_existing_employee_codes():
    """Read all employee codes through the paginated API."""
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
    # Confirm the API and MongoDB health check are working.
    response = httpx.get(f"{BASE_URL}/health", timeout=TIMEOUT)
    response.raise_for_status()
    print("Health check:", response.json())

    # Select an unused employee code for this run.
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

    # Race 1: only one request may create this employee.
    employee_results = run_race(
        "POST /employees",
        "/employees",
        employee,
    )

    # Use the exact same timestamp in every request so all 20
    # requests target the same attendance date, even near midnight.
    punched_at = int(time.time() * 1000)

    punch_payload = {
        "emp_code": emp_code,
        "punched_at": punched_at,
        "status": "PRESENT",
    }

    # Race 2: only one request may create attendance for that date.
    run_race(
        "POST /attendance/punch-in",
        "/attendance/punch-in",
        punch_payload,
    )

    print(f"\nTest employee: {emp_code}")
    print("Both concurrency tests passed.")
    print("The test employee and attendance record remain in MongoDB.")


if __name__ == "__main__":
    main()
