"""Shared API/worker queue configuration. Redis holds IDs, never credentials."""
import os

from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer

QUEUE_NAME = "ledger-reconciliation"


def configured_queue():
    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("Set REDIS_URL to enable background reconciliation")
    connection = Redis.from_url(url, socket_connect_timeout=3, socket_timeout=5)
    return Queue(os.environ.get("LEDGER_QUEUE_NAME", QUEUE_NAME), connection=connection,
                 serializer=JSONSerializer, default_timeout=300)
