"""Validate complete CSVs, then atomically import account-scoped transactions."""
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import StringIO
import re
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert

from .ingest import IdentityConflict
from .models import Account, AuditEvent, BankTransaction

CSV_COLUMNS = ("source_transaction_id", "date", "merchant", "amount", "currency")


@dataclass(frozen=True)
class RowError:
    row: int
    message: str


class CSVValidationError(ValueError):
    def __init__(self, errors: list[RowError]):
        self.errors = tuple(errors)
        super().__init__("; ".join(f"row {error.row}: {error.message}" for error in errors))


class TransactionIdentityConflict(IdentityConflict):
    def __init__(self, row: int, source_transaction_id: str):
        self.row = row
        self.source_transaction_id = source_transaction_id
        super().__init__(f"row {row}: source_transaction_id {source_transaction_id!r} "
                         "already exists with different transaction content")


@dataclass(frozen=True)
class ImportSummary:
    inserted: int
    skipped: int


@dataclass(frozen=True)
class _CSVRecord:
    row: int
    fields: dict


def _validate_csv(csv_text: str) -> list[_CSVRecord]:
    if not isinstance(csv_text, str):
        raise CSVValidationError([RowError(1, "CSV input must be decoded text")])
    reader = csv.reader(StringIO(csv_text.removeprefix("\ufeff"), newline=""), strict=True)
    records, errors, seen = [], [], {}
    try:
        header = next(reader, None)
        if header is None or len(header) != len(CSV_COLUMNS) or set(header) != set(CSV_COLUMNS):
            raise CSVValidationError([RowError(1, "header must contain exactly: " + ",".join(CSV_COLUMNS))])
        while True:
            row_number = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                break
            if len(row) != len(header):
                errors.append(RowError(row_number, f"expected {len(header)} columns, got {len(row)}"))
                continue
            raw = dict(zip(header, (value.strip() for value in row)))
            problems = []
            for name in ("source_transaction_id", "merchant"):
                if not raw[name] or len(raw[name]) > 255 or "\x00" in raw[name]:
                    problems.append(f"{name} must be nonblank, at most 255 characters, and contain no NUL")
            transaction_date = None
            try:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw["date"]):
                    raise ValueError
                transaction_date = date.fromisoformat(raw["date"])
            except ValueError:
                problems.append("date must be a valid YYYY-MM-DD date")
            amount = None
            if not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]{1,2})?", raw["amount"]):
                problems.append("amount must be a signed decimal with at most two fractional places")
            else:
                amount = Decimal(raw["amount"])
                if abs(amount) >= Decimal("10000000000000000"):
                    problems.append("amount must fit NUMERIC(18,2)")
            currency = raw["currency"].upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                problems.append("currency must contain three ASCII letters")
            if problems:
                errors.append(RowError(row_number, "; ".join(problems)))
                continue
            fields = {"source_transaction_id": raw["source_transaction_id"],
                      "transaction_date": transaction_date, "merchant": raw["merchant"],
                      "amount": amount.quantize(Decimal("0.01")), "currency": currency}
            previous = seen.get(fields["source_transaction_id"])
            if previous and previous.fields != fields:
                errors.append(RowError(row_number, f"source_transaction_id conflicts with row {previous.row}"))
            else:
                record = _CSVRecord(row_number, fields)
                seen.setdefault(fields["source_transaction_id"], record)
                records.append(record)
    except csv.Error as error:
        errors.append(RowError(max(reader.line_num, 1), f"malformed CSV: {error}"))
    if errors:
        raise CSVValidationError(errors)
    return records


def import_bank_transactions(engine: Engine, account_id: UUID, csv_text: str) -> ImportSummary:
    """Import all rows or none; return counts only after a successful commit.

    The caller supplies an existing account UUID and the complete CSV text.
    PostgreSQL READ COMMITTED isolation is required, as for receipt ingestion.
    """
    records = _validate_csv(csv_text)  # Complete file validation before engine access.
    if not isinstance(account_id, UUID):
        raise ValueError("account_id must be a UUID")
    if engine.dialect.name != "postgresql":
        raise ValueError("bank transaction ingestion requires PostgreSQL")
    # Collapse identical in-file repeats while retaining their skipped count.
    unique = {}
    for record in records:
        unique.setdefault(record.fields["source_transaction_id"], record)
    inserted = 0
    with engine.begin() as connection:
        if connection.scalar(select(Account.id).where(Account.id == account_id)) is None:
            raise ValueError("account_id does not identify an existing account")
        # Consistent lock ordering prevents reversed overlapping files from
        # deadlocking on the source-identity unique constraint.
        for source_id, record in sorted(unique.items()):
            fields = {**record.fields, "account_id": account_id}
            transaction_id = connection.execute(
                insert(BankTransaction).values(**fields)
                .on_conflict_do_nothing(constraint="uq_bank_transactions_source_identity")
                .returning(BankTransaction.id)
            ).scalar_one_or_none()
            if transaction_id is None:
                existing = connection.execute(select(BankTransaction.__table__).where(
                    BankTransaction.account_id == account_id,
                    BankTransaction.source_transaction_id == source_id,
                )).mappings().one()
                if any(existing[key] != value for key, value in fields.items()):
                    raise TransactionIdentityConflict(record.row, source_id)
                continue
            payload = {**record.fields, "account_id": str(account_id),
                       "transaction_date": fields["transaction_date"].isoformat(),
                       "amount": format(fields["amount"], ".2f")}
            connection.execute(insert(AuditEvent).values(
                bank_transaction_id=transaction_id, event_type="bank_transaction.created", payload=payload,
            ))
            inserted += 1
    return ImportSummary(inserted=inserted, skipped=len(records) - inserted)
