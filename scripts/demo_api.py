"""Repeatable local HTTP walkthrough; the API must already be running."""
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    token = os.environ["LEDGER_DEMO_TOKEN"]
    with httpx.Client(base_url=os.environ.get("LEDGER_API_URL", "http://127.0.0.1:8000"),
                      headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:
        def request(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        def items(path):
            offset = 0
            while True:
                batch = request("GET", f"{path}?limit=100&offset={offset}")["items"]
                yield from batch
                if len(batch) < 100:
                    break
                offset += 100

        accounts = [item for item in items("/accounts") if item["name"] == "HTTP walkthrough"]
        account = accounts[0] if accounts else request("POST", "/accounts", {"name": "HTTP walkthrough"})
        aid = account["id"]
        base = f"/accounts/{aid}"
        receipt = {"merchant": "Blue Bottle Coffee", "receipt_date": "2026-10-22", "amount": "12.30",
                   "currency": "CAD", "source": "http-walkthrough", "source_receipt_id": "receipt-001"}
        ingested = request("POST", "/receipts", receipt)
        rid = ingested["receipt_id"]
        print("Receipt:", ingested)
        print("Assignment:", request("POST", base + "/receipts/assign", {"receipt_ids": [rid]}))
        csv = (ROOT / "data/samples/bank-transactions.csv").read_text()
        print("Import:", request("POST", base + "/transactions/import", {"csv_text": csv}))
        print("Repeated import:", request("POST", base + "/transactions/import", {"csv_text": csv}))
        print("Reconciliation:", request("POST", base + "/reconciliation"))
        cases = [item for item in items(base + "/review-cases") if item["receipt_id"] == rid]
        if cases:
            case = cases[0]
            transaction = next(item for item in items(base + "/transactions")
                               if item["source_transaction_id"] == "demo-bank-001")
            body = {"bank_transaction_id": transaction["id"], "expected_revision": case["revision"],
                    "request_id": str(uuid5(NAMESPACE_URL, "ledger:http-walkthrough:confirm")),
                    "reason": "Verified sample statement reference"}
            path = base + f"/review-cases/{rid}/confirm"
            print("Confirmation:", request("POST", path, body))
            assert request("POST", path, body)["repeated"] is True
        print("Outcomes:", list(items(base + "/outcomes")))
        audit = list(items(base + "/audit-events"))
        assert sum(item["event_type"] == "receipt.reviewed" for item in audit) == 1
        for item in audit:
            print("Audit:", item["event_type"], item["payload"])
        assert client.get("/docs").is_success and client.get("/openapi.json").is_success
        print("Swagger: http://127.0.0.1:8000/docs (Authorize using the environment token)")


if __name__ == "__main__":
    main()
