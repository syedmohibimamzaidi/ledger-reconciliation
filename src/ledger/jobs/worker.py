"""Separate single-process demo worker: python -m ledger.jobs.worker."""
import argparse
import os

from rq import SimpleWorker
from rq.serializers import JSONSerializer

from .queue import configured_queue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--burst", action="store_true", help="Exit when the queue is empty (tests/local checks)")
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        parser.error("Set DATABASE_URL")
    queue = configured_queue()
    try:
        SimpleWorker([queue], connection=queue.connection, serializer=JSONSerializer).work(burst=args.burst)
    finally:
        queue.connection.close()


if __name__ == "__main__":
    main()
