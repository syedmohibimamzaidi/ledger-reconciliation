"""Recover expired attempts and send due durable intents: run as a separate process."""
import argparse
import logging
import os
import time

from sqlalchemy import create_engine

from .queue import configured_queue
from .service import dispatch_due, recover_expired


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one bounded recovery/dispatch sweep")
    args = parser.parse_args()
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    queue = configured_queue()
    try:
        while True:
            try:
                recover_expired(engine)
                dispatch_due(engine, queue)
            except Exception:
                # Status details/logs do not expose SQL, credentials or input data.
                logging.error("Job dispatcher sweep failed; durable state will be checked again.")
                if args.once:
                    raise SystemExit(1)
            if args.once:
                break
            time.sleep(1)
    finally:
        engine.dispose()
        queue.connection.close()


if __name__ == "__main__":
    main()
