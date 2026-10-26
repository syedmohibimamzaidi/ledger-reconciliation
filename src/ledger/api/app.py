"""Run with uvicorn ledger.api.app:create_app --factory (single-user demo)."""
from contextlib import asynccontextmanager
import hmac
import os
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine, create_engine

from ledger.persistence.bank_ingest import CSVValidationError, import_bank_transactions
from ledger.persistence.ingest import IdentityConflict, StructuredReceipt, ingest_receipt
from ledger.persistence.models import Account, BankTransaction, Receipt, ReconciliationDecision
from ledger.persistence.reconcile import assign_receipts_to_account, reconcile_account
from ledger.persistence.review import ReviewConflict, StaleReview, confirm_candidate, list_review_cases, reject_candidate
from . import schemas as s
from .store import MissingRecord, ScopeConflict, Store

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100_000)]
bearer = HTTPBearer(auto_error=False)


class APIError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details


def create_app(*, engine: Engine | None = None) -> FastAPI:
    """Read configuration once. Injected engines remain owned by the caller."""
    token = os.environ.get("LEDGER_DEMO_TOKEN", "")
    identity = os.environ.get("LEDGER_DEMO_IDENTITY", "").strip()
    if (not token or token != token.strip() or not token.isascii()
            or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in token)
            or not identity or len(identity) > 255 or "\x00" in identity):
        raise RuntimeError("Set LEDGER_DEMO_TOKEN and LEDGER_DEMO_IDENTITY to valid nonblank values")
    owned = engine is None
    if engine is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("Set DATABASE_URL")
        engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        raise RuntimeError("The API requires PostgreSQL")
    store = Store(engine)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if owned:
                engine.dispose()

    app = FastAPI(title="Ledger single-user demo", version="0.1.0", lifespan=lifespan)

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            raise APIError(401, "unauthorized", "Valid bearer token required")
        return identity

    @app.exception_handler(APIError)
    async def api_error(request, error):
        return JSONResponse(status_code=error.status, content={"error": {
            "code": error.code, "message": error.message, "details": error.details}},
            headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Never echo raw request bodies, credentials, or non-JSON exception contexts.
        details = [{"loc": list(item["loc"]), "message": item["msg"], "type": item["type"]}
                   for item in error.errors()]
        return await api_error(request, APIError(422, "invalid_input", "Request validation failed", details))

    @app.exception_handler(ValueError)
    @app.exception_handler(ReviewConflict)
    @app.exception_handler(MissingRecord)
    @app.exception_handler(ScopeConflict)
    async def service_error(request, error):
        status, code, details = 422, "invalid_input", None
        if isinstance(error, MissingRecord):
            status, code = 404, "not_found"
        elif isinstance(error, StaleReview):
            status, code = 409, "stale_revision"
            details = {"expected_revision": error.expected, "actual_revision": error.actual}
        elif isinstance(error, IdentityConflict) or (isinstance(error, ReviewConflict) and
                                                     str(error).startswith("request_id already used")):
            status, code = 409, "identity_conflict"
        elif isinstance(error, (ReviewConflict, ScopeConflict)) or str(error) == "a receipt is already assigned to another account":
            status, code = 409, "state_conflict"
        if isinstance(error, CSVValidationError):
            details = [{"row": item.row, "message": item.message} for item in error.errors]
        return await api_error(request, APIError(status, code, str(error), details))

    router = APIRouter(dependencies=[Depends(authenticate)], responses={
        status: {"model": s.ErrorOut} for status in (401, 404, 409, 422)})

    def page(items, limit, offset):
        return {"items": items, "limit": limit, "offset": offset}

    @router.post("/accounts", response_model=s.AccountOut, status_code=201)
    def create_account(body: s.AccountCreate):
        return store.create_account(body.name)

    @router.get("/accounts", response_model=s.Page[s.AccountOut])
    def accounts(limit: Limit = 50, offset: Offset = 0):
        return page(store.page(Account, limit, offset), limit, offset)

    @router.post("/receipts", response_model=s.IngestOut)
    def receipt(body: s.ReceiptCreate):
        return ingest_receipt(engine, StructuredReceipt(**body.model_dump()))

    @router.get("/receipts", response_model=s.Page[s.ReceiptOut])
    def receipts(limit: Limit = 50, offset: Offset = 0, account_id: UUID | None = None):
        if account_id is not None:
            store.account(account_id)
        return page(store.page(Receipt, limit, offset, account_id), limit, offset)

    @router.post("/accounts/{account_id}/receipts/assign", response_model=s.AssignmentOut)
    def assign(account_id: UUID, body: s.Assignment):
        store.assignment(account_id, body.receipt_ids)
        return {"assigned": assign_receipts_to_account(engine, account_id, body.receipt_ids)}

    @router.post("/accounts/{account_id}/transactions/import", response_model=s.ImportOut)
    def import_csv(account_id: UUID, body: s.CSVImport):
        store.account(account_id)
        return import_bank_transactions(engine, account_id, body.csv_text)

    @router.get("/accounts/{account_id}/transactions", response_model=s.Page[s.TransactionOut])
    def transactions(account_id: UUID, limit: Limit = 50, offset: Offset = 0):
        store.account(account_id)
        return page(store.page(BankTransaction, limit, offset, account_id), limit, offset)

    @router.post("/accounts/{account_id}/reconciliation", response_model=s.ReconcileOut)
    def reconcile(account_id: UUID):
        store.account(account_id)
        return reconcile_account(engine, account_id)

    @router.get("/accounts/{account_id}/outcomes", response_model=s.Page[s.DecisionOut])
    def outcomes(account_id: UUID, limit: Limit = 50, offset: Offset = 0):
        store.account(account_id)
        return page(store.page(ReconciliationDecision, limit, offset, account_id), limit, offset)

    @router.get("/accounts/{account_id}/review-cases", response_model=s.Page[s.ReviewCaseOut])
    def cases(account_id: UUID, limit: Limit = 50, offset: Offset = 0):
        store.account(account_id)
        return page(list_review_cases(engine, account_id)[offset:offset + limit], limit, offset)

    @router.post("/accounts/{account_id}/review-cases/{receipt_id}/confirm", response_model=s.ReviewOut)
    def confirm(account_id: UUID, receipt_id: UUID, body: s.ConfirmRequest,
                reviewer: Annotated[str, Depends(authenticate)]):
        store.review(account_id, receipt_id, body.bank_transaction_id)
        return confirm_candidate(engine, account_id, receipt_id, reviewer=reviewer, **body.model_dump())

    @router.post("/accounts/{account_id}/review-cases/{receipt_id}/reject", response_model=s.ReviewOut)
    def reject(account_id: UUID, receipt_id: UUID, body: s.ReviewRequest,
               reviewer: Annotated[str, Depends(authenticate)]):
        store.review(account_id, receipt_id, body.bank_transaction_id)
        return reject_candidate(engine, account_id, receipt_id, reviewer=reviewer, **body.model_dump())

    @router.get("/accounts/{account_id}/audit-events", response_model=s.Page[s.AuditOut])
    def audit(account_id: UUID, limit: Limit = 50, offset: Offset = 0):
        store.account(account_id)
        return page(store.audit(account_id, limit, offset), limit, offset)

    app.include_router(router)
    return app
