"""A crash between indexed conflict detection and its transaction commit loses no work."""
import json
import multiprocessing
import os

import pytest
from redis import Redis
from sqlalchemy import text

from novel_factory.application.conflict_resolution import ConflictResolutionService
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.event_streams import STREAMS
from novel_factory.infrastructure.eventhub import EventHub
from novel_factory.orchestration.processors import handler_for
from scripts.pitr_streams import stage_semantic_conflict

from . import test_p55_eventhub as transport

server = transport.server
pytestmark = [
    pytest.mark.postgres, pytest.mark.recovery, pytest.mark.fault,
    pytest.mark.skipif(not os.environ.get("NF_TEST_REDIS_URL"), reason="requires disposable Redis"),
]


def exit_before_detection_commit(database_url, redis_port, changed):
    db = Database(database_url)
    client = Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    bus = EventHub(db, client)
    detect = handler_for("world")

    def handle(conn, event):
        detect(conn, event)
        if event.event_type == "world.accepted" and event.world_ref["world_revision_id"] == changed:
            if not conn.scalar(text("SELECT count(*) FROM nf_conflict_cases WHERE world_revision_id=:r"),
                               {"r": changed}):
                raise AssertionError("Crash must follow the actual case insert")
            os._exit(73)

    for _ in range(100):
        if not bus.consume_once("world", "crashing-detector", handle):
            break
    raise AssertionError("Target commit did not reach the detector")


def test_consumer_death_rolls_back_case_and_reclaims_the_original_event(kernel_env, server):
    kernel = kernel_env["kernel"]
    scope, task, result = stage_semantic_conflict(kernel)
    changed = result["world_ref"]["world_revision_id"]
    hub = EventHub(kernel.db, server.client)
    while hub.relay_once():
        pass
    child = multiprocessing.get_context("spawn").Process(
        target=exit_before_detection_commit, args=(kernel_env["url"], server.port, changed)
    )
    child.start()
    try:
        child.join(25)
        assert child.exitcode == 73
    finally:
        if child.is_alive():
            child.kill()
            child.join()
    stream = STREAMS["domain"]
    target = next(fields for _, fields in server.client.xrange(stream) if
                  (json.loads(fields["envelope"]).get("world_ref") or {}).get("world_revision_id") == changed
                  and json.loads(fields["envelope"])["event_type"] == "world.accepted")
    event_id = json.loads(target["envelope"])["event_id"]
    with kernel.db.uow(write=False) as repo:
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_conflict_cases WHERE task_id=:t"),
                                {"t": task["task_id"]}) == 0
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_semantic_scans WHERE event_id=:e"),
                                {"e": event_id}) == 1
    server.client.xadd(stream, target)
    while hub.consume_once("world", "restored-detector", handler_for("world"), reclaim_idle_ms=0):
        pass
    resolver = ConflictResolutionService(kernel)
    with kernel.db.uow(write=False) as repo:
        case_id = repo.conn.execute(text("SELECT case_id FROM nf_conflict_cases WHERE task_id=:t"),
                                    {"t": task["task_id"]}).scalar_one()
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_semantic_scans WHERE event_id=:e"),
                                {"e": event_id}) == 0
        assert repo.conn.scalar(text("""SELECT count(*) FROM nf_inbox
            WHERE message_id=:e AND consumer='p55:world'"""), {"e": event_id}) == 1
    receipt = resolver.resolve(*scope, case_id)
    assert resolver.resolve(*scope, case_id) == receipt
    assert resolver.resolve_one(*scope) is None
    with kernel.db.uow(write=False) as repo:
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_conflict_resolutions WHERE case_id=:c"),
                                {"c": case_id}) == 1
        assert kernel.head(repo, scope[1], scope[2]) == result["world_ref"]
