"""HTTP contracts exercised against migrated PostgreSQL, without mocked services."""
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text

from ledger.api.app import create_app
from ledger.persistence.models import AuditEvent, Receipt, ReviewAction


@pytest.fixture
def client(db_engine, monkeypatch):
    monkeypatch.setenv("LEDGER_DEMO_TOKEN", "test-only-token")
    monkeypatch.setenv("LEDGER_DEMO_IDENTITY", "demo-reviewer")
    with TestClient(create_app(engine=db_engine), headers={"Authorization": "Bearer test-only-token"}) as client:
        yield client


def post(client, path, body=None):
    response = client.post(path, json=body)
    assert response.is_success, response.text
    return response.json()


def setup_case(client):
    aid = post(client, "/accounts", {"name": "HTTP test"})["id"]
    receipt = {"merchant": "Blue Bottle", "receipt_date": "2026-10-22", "amount": "12.30",
               "currency": "CAD", "source": "http-test", "source_receipt_id": str(uuid4())}
    rid = post(client, "/receipts", receipt)["receipt_id"]
    assert post(client, f"/accounts/{aid}/receipts/assign", {"receipt_ids": [rid]}) == {"assigned": 1}
    csv = "source_transaction_id,date,merchant,amount,currency\na,2026-10-22,Blue Bottle,-12.30,CAD\nb,2026-10-22,Blue Bottle,-12.30,CAD\n"
    assert post(client, f"/accounts/{aid}/transactions/import", {"csv_text": csv}) == {"inserted": 2, "skipped": 0}
    assert post(client, f"/accounts/{aid}/reconciliation")["needs_review"] == 1
    case = client.get(f"/accounts/{aid}/review-cases").json()["items"][0]
    return aid, rid, receipt, csv, case


def test_end_to_end_ingest_reconcile_review_audit(client, db_engine):
    aid, rid, receipt, csv, case = setup_case(client)
    assert post(client, "/receipts", receipt) == {"receipt_id": rid, "created": False}
    assert post(client, f"/accounts/{aid}/receipts/assign", {"receipt_ids": [rid]}) == {"assigned": 0}
    assert post(client, f"/accounts/{aid}/transactions/import", {"csv_text": csv}) == {"inserted": 0, "skipped": 2}
    transactions = client.get(f"/accounts/{aid}/transactions").json()["items"]
    assert {item["amount"] for item in transactions} == {"-12.30"}
    assert client.get("/receipts", params={"account_id": aid}).json()["items"][0]["amount"] == "12.30"
    base = f"/accounts/{aid}/review-cases/{rid}"
    request = {"bank_transaction_id": case["candidates"][0]["bank_transaction_id"],
               "request_id": str(uuid4()), "expected_revision": case["revision"], "reason": "Duplicate authorization"}
    rejected = post(client, base + "/reject", request)
    assert rejected["revision"] == 2 and rejected["status"] == "needs_review"
    retry = post(client, base + "/reject", request)
    assert retry == {**rejected, "repeated": True}
    conflict = client.post(base + "/reject", json={**request, "reason": "changed"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "identity_conflict"
    stale = client.post(base + "/confirm", json={**request, "request_id": str(uuid4())})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_revision"
    assert stale.json()["error"]["details"]["actual_revision"] == 2
    remaining = client.get(f"/accounts/{aid}/review-cases").json()["items"][0]
    confirm = {**request, "request_id": str(uuid4()), "expected_revision": 2,
               "bank_transaction_id": remaining["candidates"][0]["bank_transaction_id"], "reason": "Checked statement"}
    accepted = post(client, base + "/confirm", confirm)
    assert accepted["status"] == "matched" and accepted["revision"] == 3
    # An old request must still return its original result after a later action.
    assert post(client, base + "/reject", request) == {**rejected, "repeated": True}
    assert post(client, base + "/confirm", confirm) == {**accepted, "repeated": True}
    assert post(client, f"/accounts/{aid}/reconciliation")["decisions_written"] == 0
    assert client.get(f"/accounts/{aid}/outcomes").json()["items"][0]["revision"] == 3
    assert client.get(f"/accounts/{aid}/review-cases").json()["items"] == []
    audits = client.get(f"/accounts/{aid}/audit-events").json()["items"]
    assert len(audits) == 7
    reviews = [item for item in audits if item["event_type"] == "receipt.reviewed"]
    assert len(reviews) == 2 and {item["payload"]["reviewer"] for item in reviews} == {"demo-reviewer"}
    assert {item["payload"]["receipt"]["amount"] for item in reviews} == {"12.30"}
    other = post(client, "/accounts", {"name": "Other"})["id"]
    assert client.get(f"/accounts/{other}/audit-events").json()["items"] == []
    assert client.get(f"/accounts/{other}/outcomes").json()["items"] == []
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ReviewAction)) == 2


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}])
def test_authentication_all_business_routes(client, headers):
    schema = client.get("/openapi.json").json()
    for path, methods in schema["paths"].items():
        path = path.replace("{account_id}", str(uuid4())).replace("{receipt_id}", str(uuid4()))
        for method in methods:
            with TestClient(client.app) as anonymous:
                response = anonymous.request(method, path, headers=headers)
            assert response.status_code == 401, response.text
            assert response.headers["www-authenticate"] == "Bearer"


def test_swagger_openapi(client):
    assert client.get("/docs").status_code == 200
    assert "SwaggerUIBundle" in client.get("/docs").text
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    for methods in schema["paths"].values():
        for operation in methods.values():
            assert operation["security"] == [{"HTTPBearer": []}]
            assert "409" in operation["responses"]


@pytest.mark.parametrize("key,value", [("LEDGER_DEMO_TOKEN", ""), ("LEDGER_DEMO_IDENTITY", ""),
                                         ("LEDGER_DEMO_TOKEN", "  "), ("LEDGER_DEMO_IDENTITY", "x" * 256)])
def test_missing_auth_configuration_fails_closed(monkeypatch, key, value):
    monkeypatch.setenv("LEDGER_DEMO_TOKEN", "test-only-token")
    monkeypatch.setenv("LEDGER_DEMO_IDENTITY", "demo-reviewer")
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="LEDGER_DEMO"):
        create_app()


@pytest.mark.parametrize("amount", [12.30, "NaN", "Infinity", "1.001", "-1.00", "10000000000000000.00", True])
def test_receipt_validation(client, amount):
    body = {"merchant": "Shop", "receipt_date": "2026-10-22", "amount": amount,
            "currency": "CAD", "source": "http", "source_receipt_id": "bad"}
    response = client.post("/receipts", json=body)
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_input"


def test_validation_conflicts_missing_records_and_pagination(client, db_engine):
    aid, rid, receipt, csv, case = setup_case(client)
    for body in ({"name": " "}, {"name": "x\x00"}, {"name": "a", "extra": 1}):
        assert client.post("/accounts", json=body).status_code == 422
    assert client.post("/receipts", json={**receipt, "receipt_date": "2026-02-30"}).status_code == 422
    assert client.post("/receipts", json={**receipt, "amount": "12.31"}).json()["error"]["code"] == "identity_conflict"
    assert client.post(f"/accounts/{aid}/transactions/import", json={"csv_text": csv.replace("-12.30", "-13.00")}).status_code == 409
    invalid_csv = client.post(f"/accounts/{aid}/transactions/import", json={"csv_text": csv + "bad,row\n"})
    assert invalid_csv.status_code == 422 and invalid_csv.json()["error"]["details"][0]["row"] == 4
    request = {"bank_transaction_id": case["candidates"][0]["bank_transaction_id"],
               "request_id": str(uuid4()), "expected_revision": 1, "reason": "Checked"}
    route = f"/accounts/{aid}/review-cases/{rid}/confirm"
    for changes in ({"reviewer": "spoof"}, {"expected_revision": True}, {"expected_revision": 0},
                    {"expected_revision": "1"}, {"request_id": "bad"}, {"reason": " "},
                    {"acknowledge_amount_mismatch": "true"}):
        assert client.post(route, json={**request, **changes}).status_code == 422
    assert client.post(route, json={**request, "bank_transaction_id": str(uuid4())}).status_code == 404
    other = post(client, "/accounts", {"name": "Other"})["id"]
    assert client.post(f"/accounts/{other}/receipts/assign", json={"receipt_ids": [rid]}).status_code == 409
    assert client.post(f"/accounts/{aid}/receipts/assign", json={"receipt_ids": [str(uuid4())]}).status_code == 404
    assert client.get(f"/accounts/{uuid4()}/audit-events").status_code == 404
    assert client.post(f"/accounts/{uuid4()}/reconciliation").status_code == 404
    for route in ("/accounts", "/receipts", f"/accounts/{aid}/transactions", f"/accounts/{aid}/outcomes",
                  f"/accounts/{aid}/review-cases", f"/accounts/{aid}/audit-events"):
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 100001}):
            assert client.get(route, params=params).status_code == 422
        assert len(client.get(route, params={"limit": 1}).json()["items"]) == 1
        assert client.get(route, params={"offset": 100}).json()["items"] == []


def test_service_rollback_survives_http_layer(client, db_engine):
    aid, rid, receipt, csv, case = setup_case(client)
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events ADD CONSTRAINT api_force_failure CHECK (event_type <> 'receipt.reviewed')"))
    request = {"bank_transaction_id": case["candidates"][0]["bank_transaction_id"],
               "request_id": str(uuid4()), "expected_revision": 1, "reason": "Checked"}
    with TestClient(client.app, raise_server_exceptions=False, headers=client.headers) as failing:
        assert failing.post(f"/accounts/{aid}/review-cases/{rid}/confirm", json=request).status_code == 500
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ReviewAction)) == 0
    assert client.get(f"/accounts/{aid}/review-cases").json()["items"][0]["revision"] == 1


def test_automatic_outcomes_and_mismatch_acknowledgement(client):
    aid = post(client, "/accounts", {"name": "Rules over HTTP"})["id"]
    base = f"/accounts/{aid}"
    rids = []
    for merchant in ("Exact", "Different", "Absent"):
        rids.append(post(client, "/receipts", {"merchant": merchant, "receipt_date": "2026-10-22",
            "amount": "12.30", "currency": "CAD", "source": "rules-http", "source_receipt_id": merchant})["receipt_id"])
    post(client, base + "/receipts/assign", {"receipt_ids": rids})
    csv = "source_transaction_id,date,merchant,amount,currency\nx,2026-10-22,Exact,-12.30,CAD\ny,2026-10-22,Different,-15.00,CAD\n"
    post(client, base + "/transactions/import", {"csv_text": csv})
    assert post(client, base + "/reconciliation") == {
        "matched": 1, "needs_review": 1, "unmatched": 1, "unmatched_transactions": 1, "decisions_written": 3}
    case = client.get(base + "/review-cases").json()["items"][0]
    body = {"bank_transaction_id": case["candidates"][0]["bank_transaction_id"], "request_id": str(uuid4()),
            "expected_revision": 1, "reason": "Verified tip"}
    path = base + f"/review-cases/{rids[1]}/confirm"
    assert client.post(path, json=body).status_code == 422
    assert post(client, path, {**body, "acknowledge_amount_mismatch": True})["status"] == "matched"
    reviewed = [item for item in client.get(base + "/audit-events").json()["items"]
                if item["event_type"] == "receipt.reviewed"][0]
    assert reviewed["payload"]["transaction"]["amount"] == "-15.00"
    assert reviewed["payload"]["receipt"]["amount"] == "12.30"
    assert post(client, base + "/reconciliation")["decisions_written"] == 0


def test_conflicting_import_is_atomic(client):
    aid, rid, receipt, csv, case = setup_case(client)
    base = f"/accounts/{aid}"
    conflicting = "source_transaction_id,date,merchant,amount,currency\n0-new,2026-10-22,Other,-1.00,CAD\na,2026-10-22,Blue Bottle,-13.00,CAD\n"
    assert client.post(base + "/transactions/import", json={"csv_text": conflicting}).status_code == 409
    assert len(client.get(base + "/transactions").json()["items"]) == 2
    assert len(client.get(base + "/audit-events").json()["items"]) == 5
