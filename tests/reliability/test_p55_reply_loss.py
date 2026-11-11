"""A one-way TCP partition after Redis executes a request, with PostgreSQL still writable."""

import os
import select
import socket
import socketserver
import threading
import time
from contextlib import contextmanager

import pytest
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError
from redis.retry import Retry

from novel_factory.infrastructure.event_streams import STREAMS
from novel_factory.infrastructure.eventhub import EventHub

from . import test_p55_eventhub as eventhub_cases
from .test_p55_eventhub import handler, publish, receipt, value

# Register the same disposable Redis process and database fixtures used by the crash tests.
hub = eventhub_cases.hub
server = eventhub_cases.server

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.fault,
    pytest.mark.skipif(not os.environ.get("NF_TEST_REDIS_URL"), reason="requires disposable Redis"),
]


@contextmanager
def lose_reply(port, command):
    """Cut just the client path, after observing a reply from the private upstream."""
    marker = b"\r\n" + command + b"\r\n"
    cut = threading.Event()

    class Link(socketserver.BaseRequestHandler):
        def handle(self):
            if cut.is_set():
                return
            with socket.create_connection(("127.0.0.1", port), timeout=2) as upstream:
                tail = b""
                targeted = False
                while True:
                    readable, _, _ = select.select([self.request, upstream], [], [], 3)
                    if not readable:
                        return
                    for peer in readable:
                        data = peer.recv(65536)
                        if not data:
                            return
                        if peer is self.request:
                            targeted |= marker in tail + data
                            tail = (tail + data)[-len(marker) :]
                            upstream.sendall(data)
                        elif targeted:
                            cut.set()
                            return
                        else:
                            self.request.sendall(data)

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Link) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            with Redis(
                host="127.0.0.1",
                port=proxy.server_address[1],
                socket_timeout=2,
                retry=Retry(NoBackoff(), 0),
                decode_responses=True,
            ) as client:
                yield client, cut
        finally:
            proxy.shutdown()
            thread.join(timeout=3)


def test_lost_xadd_and_xack_replies_keep_one_committed_effect(hub, server):
    db = hub.db
    event = publish(db)
    stream = STREAMS["observability"]
    with lose_reply(server.port, b"XADD") as (client, cut):
        with pytest.raises(ConnectionError):
            EventHub(db, client).relay_once()
        assert cut.is_set() and server.client.xlen(stream) == 1
        # Other clients can reach Redis and the business database during the partition.
        assert server.client.ping() and receipt(db, event) == 0
        extra = publish(db)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and server.client.xlen(stream) < 3:
        hub.relay_once()
        time.sleep(0.02)
    assert server.client.xlen(stream) == 3  # first publication, retry, additional event
    with lose_reply(server.port, b"XACK") as (client, cut):
        with pytest.raises(ConnectionError):
            EventHub(db, client).consume_once("statistics", "first", handler, limit=1)
        assert cut.is_set() and receipt(db, event) == 1
        assert server.client.xpending(stream, "statistics")["pending"] == 0
    while hub.consume_once("statistics", "recovered", handler, reclaim_idle_ms=0):
        pass
    for ident in (event, extra):
        assert receipt(db, ident) == 1
        assert value(db, "SELECT count(*) FROM nf_test_bus_effect WHERE id=:id", id=ident) == 1
