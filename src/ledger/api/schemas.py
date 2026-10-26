"""HTTP contracts. Money is always serialized as a fixed two-place string."""
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer


def clean_text(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("must be nonblank text without NUL")
    return value.strip()


Text = Annotated[str, BeforeValidator(clean_text)]


def money_input(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,2})?", value):
        raise ValueError("amount must be a nonnegative decimal string with at most two fractional places")
    return Decimal(value)


Money = Annotated[Decimal, PlainSerializer(lambda value: format(value, ".2f"), return_type=str, when_used="json")]
ReceiptMoney = Annotated[Money, BeforeValidator(money_input, json_schema_input_type=str), Field(ge=0, lt=Decimal("10000000000000000"))]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCreate(Input):
    name: Text = Field(max_length=255)


class AccountOut(AccountCreate):
    id: UUID
    created_at: datetime


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    return value


class ReceiptCreate(Input):
    merchant: Text = Field(max_length=255)
    receipt_date: Annotated[date, BeforeValidator(iso_date)]
    amount: ReceiptMoney
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    source: Text = Field(max_length=100)
    source_receipt_id: Text = Field(max_length=255)


class ReceiptOut(BaseModel):
    id: UUID
    account_id: UUID | None
    merchant: str
    receipt_date: date
    amount: Money
    currency: str
    source: str
    source_receipt_id: str
    status: str
    created_at: datetime
    updated_at: datetime


class IngestOut(BaseModel):
    receipt_id: UUID
    created: bool


class Assignment(Input):
    receipt_ids: list[UUID] = Field(min_length=1, max_length=100)


class AssignmentOut(BaseModel):
    assigned: int


class CSVImport(Input):
    csv_text: str = Field(min_length=1, max_length=2_000_000)


class ImportOut(BaseModel):
    inserted: int
    skipped: int


class TransactionOut(BaseModel):
    id: UUID
    account_id: UUID
    source_transaction_id: str
    transaction_date: date
    merchant: str
    amount: Money
    currency: str
    created_at: datetime


class ReconcileOut(BaseModel):
    matched: int
    needs_review: int
    unmatched: int
    unmatched_transactions: int
    decisions_written: int


class DecisionOut(BaseModel):
    id: UUID
    account_id: UUID
    receipt_id: UUID
    bank_transaction_id: UUID | None
    status: str
    revision: int
    details: dict
    created_at: datetime
    updated_at: datetime


class ReviewCaseOut(BaseModel):
    account_id: UUID
    receipt_id: UUID
    revision: int
    reasons: list[str]
    receipt: dict
    candidates: list[dict]
    rejected_candidates: list[dict]


class ReviewRequest(Input):
    bank_transaction_id: UUID
    request_id: UUID
    expected_revision: int = Field(strict=True, ge=1)
    reason: Text = Field(max_length=2000)


class ConfirmRequest(ReviewRequest):
    acknowledge_amount_mismatch: bool = Field(default=False, strict=True)


class ReviewOut(BaseModel):
    action_id: UUID
    receipt_id: UUID
    revision: int
    status: str
    reviewed_at: datetime
    repeated: bool


class AuditOut(BaseModel):
    id: UUID
    receipt_id: UUID | None
    bank_transaction_id: UUID | None
    review_action_id: UUID | None
    event_type: str
    payload: dict
    created_at: datetime


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list[dict] | dict | None = None


class ErrorOut(BaseModel):
    error: ErrorDetail


class JobSubmit(Input):
    request_key: Text = Field(max_length=255)


class JobFailure(BaseModel):
    code: str
    message: str


class JobOut(BaseModel):
    id: UUID
    account_id: UUID
    request_key: str
    status: Literal["queued", "running", "succeeded", "failed"]
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result_summary: ReconcileOut | None
    failure_details: JobFailure | None
