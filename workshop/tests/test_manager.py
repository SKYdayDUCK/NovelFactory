import asyncio
import os
from uuid import uuid4

import pytest

from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.manager import history, request_resume, state
from agent_workshop.manager_worker import run_once
from agent_workshop.organization import bootstrap


def test_manager_restart_cut_and_idle():
    dsn = os.environ["AW_DATABASE_URL"]
    org, install = uuid4().hex, uuid4().hex
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,'nf-test',true)", (install,))
        bootstrap(conn, org, installation_id=install)
        manager = authenticate(conn, enroll(conn, install, org, "manager", 1, 1))
        actor = authenticate(conn, enroll(conn, install, org, "actor-1", 1, 1))
        first = history(conn, manager, limit=1)
        req = request_resume(conn, manager, "resume-once")
        assert req == request_resume(conn, manager, "resume-once")
        assert "budget" not in state(conn, actor)
        with pytest.raises(Denied):
            history(conn, actor, cursor=first["next_cursor"])
        with pytest.raises(Denied):
            request_resume(conn, actor, "other")
    calls = []

    async def nf(name, args):
        calls.append(name)
        return dict(
            workflow_state="IDLE", ready_count=0, awakened=0, recovered=0, pending_review_count=0
        )

    assert asyncio.run(run_once(dsn, org, install, nf, nf)) == "DONE"
    assert asyncio.run(run_once(dsn, org, install, nf, nf)) == "IDLE"
    assert calls == ["nf_resume_project"]
    with connect(dsn) as c:
        done = request_resume(c, manager, "resume-once")
        assert (
            done["request_id"] == req["request_id"] and done["result"]["workflow_state"] == "IDLE"
        )
        cursor = first["next_cursor"]
        while cursor:
            page = history(c, manager, cursor=cursor)
            assert page["source_cut"] == first["source_cut"]
            assert all(e["source"] != "aw_resume_requests" for e in page["events"])
            cursor = page["next_cursor"]
        own = history(c, actor)
        assert all(e["data"].get("employee_id") == "actor-1" for e in own["events"])


def test_installation_isolates_progress_history_and_idempotency():
    from agent_workshop.delegation import delegate
    from agent_workshop.manager import progress
    from agent_workshop.provision import allow_slot
    from test_delegation import make, setup_slot

    dsn, org, original, volume = setup_slot()
    op = make(dsn, volume)
    with connect(dsn) as c:
        nf_instance = c.execute(
            "SELECT nf_instance_id FROM aw_installations WHERE installation_id=%s", (original,)
        ).fetchone()["nf_instance_id"]
        other, other_org = str(uuid4()), str(uuid4())
        c.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (other, nf_instance))
        # An installation can negotiate before the directory exists. Its old token
        # cannot become a member when a different installation provisions the org.
        handshake = enroll(c, original, other_org, "manager", 1, 1)
        bootstrap(c, other_org, installation_id=other)
        c.execute("INSERT INTO aw_budget_accounts VALUES (%s,1,'USD')", (other_org,))
        with pytest.raises(Denied, match="VIEW_DENIED"):
            request_resume(c, authenticate(c, handshake), "same-author-request")
        for installation, directory in ((other, org), (original, other_org)):
            with pytest.raises(Denied, match="ORGANIZATION_UNAVAILABLE"):
                enroll(c, installation, directory, "manager", 1, 1)
        local = authenticate(c, enroll(c, original, org, "manager", 1, 1))
        foreign = authenticate(c, enroll(c, other, other_org, "manager", 1, 1))
        local_resume = request_resume(c, local, "same-author-request")
        other_resume = request_resume(c, foreign, "same-author-request")
        assert local_resume["request_id"] != other_resume["request_id"]
        assert progress(c, local, op)["hold"] == "HELD"
        actor = authenticate(c, enroll(c, original, org, "actor-1", 1, 1))
        assert "hold" not in progress(c, actor, op)
        for who in (foreign, authenticate(c, enroll(c, original, org, "script", 1, 1))):
            with pytest.raises(Denied, match="VIEW_DENIED"):
                progress(c, who, op)
        foreign_events = history(c, foreign, limit=200)["events"]
        assert not {op, local_resume["request_id"]}.intersection(
            e["ref_id"] for e in foreign_events
        )
        assert other_resume["request_id"] in {e["ref_id"] for e in foreign_events}
        assert state(c, foreign)["delegations"] == {}
        other_volume = authenticate(c, enroll(c, other, other_org, "volume", 1, 1))
        with pytest.raises(Denied, match="VIEW_DENIED"):
            delegate(c, other_volume, "request-A", "scene-A", "1", 200)
        policy = c.execute(
            "SELECT config FROM aw_delegation_slots WHERE org_id=%s AND slot_id='scene-A'", (org,)
        ).fetchone()["config"]
        allow_slot(c, other_org, other, "other-slot", "actor-1", policy)
        second = delegate(c, other_volume, "request-A", "other-slot", "1", 200)
        assert second["operation_id"] != op
        assert delegate(c, other_volume, "request-A", "other-slot", "1", 200) == second
