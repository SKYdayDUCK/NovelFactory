"""Low-level Redis group and PEL semantics on an isolated test instance."""

import os
from uuid import uuid4

import pytest
from redis import Redis

from novel_factory.infrastructure.event_streams import STREAMS, Streams

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("NF_TEST_REDIS_URL"), reason="requires disposable Redis Streams"
    ),
]


def test_broadcast_competition_and_pending_reclaim(monkeypatch):
    client = Redis.from_url(os.environ["NF_TEST_REDIS_URL"], decode_responses=True)
    api = Streams(client)
    marker = str(uuid4())
    stream = "nf:test:domain:" + marker
    monkeypatch.setitem(STREAMS, "domain", stream)
    # The legacy database-only suite does not require a Redis server.
    with client:
        groups = ["world", "log"]
        # A dedicated DB selected by NF_TEST_REDIS_URL is required.
        for g in groups:
            api.register("domain", g)
        sid = client.xadd(stream, {"probe": marker})
        for group in groups:
            assert (sid, {"probe": marker}) in api.messages("domain", group, "a", 100, 60000)
            assert not api.messages("domain", group, "b", 100, 60000)
            assert client.xpending(stream, group)["pending"] >= 1
            assert (sid, {"probe": marker}) in api.messages("domain", group, "b", 100, 0)
            assert client.xack(stream, group, sid) == 1
        client.delete(stream)
