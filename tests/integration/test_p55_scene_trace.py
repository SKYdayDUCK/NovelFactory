"""Verify scene correlations on P5's autonomous scenario through real Redis and PG."""
import json
import os

import pytest
from redis import Redis
from sqlalchemy import text

from novel_factory.application.audit_log import LogService
from novel_factory.application.authorization import Principal
from novel_factory.infrastructure.eventhub import EventHub

pytestmark = [pytest.mark.integration,pytest.mark.postgres,pytest.mark.skipif(
    not os.getenv("NF_TEST_REDIS_URL"), reason="requires disposable Redis Streams")]


def test_scene_trace_survives_worker_pause(kernel_env, tmp_path, monkeypatch):
    from test_p5_autonomous_banquet import test_one_author_resume_through_autonomous_banquet
    evidence = tmp_path / "scene.json"
    monkeypatch.setenv("NF_P5_BANQUET_EVIDENCE",str(evidence))
    test_one_author_resume_through_autonomous_banquet(kernel_env,tmp_path)
    data = json.loads(evidence.read_text())
    wr = data["accepted_world_ref"]
    kernel = kernel_env["kernel"]
    with kernel.db.uow(write=False) as repo:
        scene = repo.conn.scalar(text("""SELECT distinct tt.trace_id FROM nf_trace_tasks tt
            JOIN nf_tasks t USING(task_id) WHERE t.work_id=:w"""),{"w":wr["work_id"]})
        owner = repo.conn.execute(text("""SELECT grant_id,principal_id FROM nf_grants
            WHERE work_id=:w AND capabilities ? 'ADMIN' AND active LIMIT 1"""),{"w":wr["work_id"]}).mappings().one()
    with Redis.from_url(os.environ["NF_TEST_REDIS_URL"]) as redis:
        hub = EventHub(kernel.db,redis)
        for _ in range(100):
            published = hub.relay_once(100)
            handled = hub.consume_once("log","trace-test",LogService.handle,limit=100,reclaim_idle_ms=0)
            if not published and not handled:
                break
    trace = LogService.trace(kernel,Principal(str(owner["grant_id"]),str(owner["principal_id"])),
                             wr["work_id"],wr["branch_id"],scene)
    kinds = {r["event_type"] for r in trace}
    assert {"context.compiled","artifact.agent_prompt","artifact.agent_response","artifact.agent_candidate",
            "world.accepted","artifact.change_point","planning.window_proposed"} <= kinds
    assert sum(r["event_type"]=="world.accepted" for r in trace)==1
    assert len({r["task_id"] for r in trace if r["task_id"]}) >= 7
