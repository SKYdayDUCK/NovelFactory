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
        bootstrap(conn, org)
        conn.execute("INSERT INTO aw_installations VALUES (%s,'nf-test',true)", (install,))
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
