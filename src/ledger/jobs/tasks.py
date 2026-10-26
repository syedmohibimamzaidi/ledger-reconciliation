"""Importable RQ entry point; establish database connections inside the worker."""
import os
from uuid import UUID

from sqlalchemy import create_engine

from .service import execute_job


def reconcile_job(job_id: str):
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    try:
        execute_job(engine, UUID(job_id))
    finally:
        engine.dispose()
