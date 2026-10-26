"""Repeatable HTTP demo: submit, receive job ID, poll, succeed.

Start `make api`, `make worker` and `make dispatcher` first. No direct database/Redis access here.
"""
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    with httpx.Client(base_url=os.environ.get("LEDGER_API_URL", "http://127.0.0.1:8000"),
                      headers={"Authorization": f"Bearer {os.environ['LEDGER_DEMO_TOKEN']}"}, timeout=15) as client:
        def request(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response

        offset, aid = 0, None
        while aid is None:
            accounts = request("GET", f"/accounts?limit=100&offset={offset}").json()["items"]
            aid = next((item["id"] for item in accounts if item["name"] == "Background HTTP walkthrough"), None)
            if aid is not None or len(accounts) < 100:
                break
            offset += 100
        if aid is None:
            aid = request("POST", "/accounts", {"name": "Background HTTP walkthrough"}).json()["id"]
        base = f"/accounts/{aid}"
        rid = request("POST", "/receipts", {"merchant": "Blue Bottle Coffee", "receipt_date": "2026-10-22",
            "amount": "12.30", "currency": "CAD", "source": "background-http-demo",
            "source_receipt_id": "receipt-001"}).json()["receipt_id"]
        request("POST", base + "/receipts/assign", {"receipt_ids": [rid]})
        request("POST", base + "/transactions/import", {
            "csv_text": (ROOT / "data/samples/bank-transactions.csv").read_text()})
        body = {"request_key": str(uuid4())}  # A new intended run; keep unchanged for its replay.
        submitted = request("POST", base + "/reconciliation-jobs", body)
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]
        print(f"HTTP 202: job_id={job_id}", flush=True)
        assert request("POST", base + "/reconciliation-jobs", body).json()["id"] == job_id
        location = submitted.headers["Location"]
        deadline, previous = time.monotonic() + 30, None
        while time.monotonic() < deadline:
            job = request("GET", location).json()
            if job["status"] != previous:
                print("Status:", job["status"], flush=True)
                previous = job["status"]
            if job["status"] == "succeeded":
                assert job["attempt_count"] == 1
                print("Result:", job["result_summary"])
                return
            if job["status"] == "failed":
                raise RuntimeError(f"Job failed: {job['failure_details']}")
            time.sleep(.2)
        raise RuntimeError(f"Job {job_id} did not finish in 30 seconds. Check the worker and dispatcher; inspect the persisted recovery timing.")


if __name__ == "__main__":
    main()
