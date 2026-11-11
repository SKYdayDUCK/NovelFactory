"""World and projection workers receive complete, genuine, out-of-order Redis entries."""

import json
import os

import pytest
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.event_streams import STREAMS
from novel_factory.orchestration.processors import TaskDispatcher, handler_for

from . import test_p55_eventhub as eventhub_cases

hub = eventhub_cases.hub
server = eventhub_cases.server
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.recovery,
    pytest.mark.skipif(not os.environ.get("NF_TEST_REDIS_URL"), reason="requires disposable Redis"),
]


def test_out_of_order_world_accepts_cannot_roll_back_a_branch(kernel_env, hub, server):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel, "white_bridge")
    principal, wr, ids = fixture["principal"], fixture["world_ref"], fixture["ids"]
    work, branch = wr["work_id"], wr["branch_id"]
    dispatcher = TaskDispatcher(
        RuntimeSupervisor(kernel, AgentGateway(kernel, {})), principal, work, branch
    )
    dispatcher.switch("redis")
    revisions = []
    for day in (30, 31):
        result = run_command(
            kernel,
            principal,
            wr,
            "RESOURCE_TRANSFER",
            {
                "from_account": ids["warehouse"],
                "to_account": ids["relief"],
                "resource_id": ids["grain"],
                "unit": "bag",
                "quantity": "1",
                "at": point(day),
            },
        )
        wr = result["world_ref"]
        revisions.append(wr["world_revision_id"])

    def authority():
        with kernel.db.uow(write=False) as repo:
            return kernel.head(repo, work, branch), repo.conn.scalar(
                text("SELECT count(*) FROM nf_revisions WHERE work_id=:w AND branch_id=:b"),
                {"w": work, "b": branch},
            )

    before = authority()
    while hub.relay_once():
        pass
    stream = STREAMS["domain"]
    by_revision = {}
    for rid, fields in server.client.xrange(stream):
        event = json.loads(fields["envelope"])
        if event["event_type"] == "world.accepted" and event["branch_id"] == branch:
            by_revision[event["world_ref"]["world_revision_id"]] = (rid, fields)
    assert set(by_revision) == set(revisions)
    server.client.xdel(stream, *(entry[0] for entry in by_revision.values()))
    for rev in (revisions[1], revisions[0], revisions[1]):
        server.client.xadd(stream, by_revision[rev][1])

    for group in ("world", "compute"):
        seen = []
        route = handler_for(group)

        def apply(conn, event, seen=seen, route=route):
            if event.event_type == "world.accepted" and event.branch_id == branch:
                seen.append(event.world_ref["world_revision_id"])
            route(conn, event)

        while hub.consume_once(group, "reorder", apply):
            pass
        assert seen == list(reversed(revisions))

    dispatcher.run_once("world", reconcile=True)
    assert [dispatcher.run_once("compute", reconcile=True)["status"] for _ in range(3)] == [
        "INDEXED",
        "INDEXED",
        "IDLE",
    ]
    assert authority() == before
    with kernel.db.uow(write=False) as repo:
        for name in ("timeline", "graph"):
            saved = repo.one("index_watermarks", {"branch_id": branch, "index_name": name})
            assert saved["world_revision_id"] == revisions[-1]
