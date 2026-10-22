"""Read DATABASE_URL without storing credentials in Alembic config."""
import os

from alembic import context
from sqlalchemy import create_engine, pool

from ledger.persistence.models import Base


def configure(connection):
    context.configure(connection=connection, target_metadata=Base.metadata,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=os.environ["DATABASE_URL"], target_metadata=Base.metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
elif context.config.attributes.get("connection") is not None:
    configure(context.config.attributes["connection"])
else:
    engine = create_engine(os.environ["DATABASE_URL"], poolclass=pool.NullPool)
    with engine.connect() as connection:
        configure(connection)
    engine.dispose()
