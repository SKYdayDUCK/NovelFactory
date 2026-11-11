import asyncio

from agent_workshop.admin import enroll
from agent_workshop.auth import authenticate, connect
from agent_workshop.manager import request_resume
from agent_workshop.manager_worker import run_once, take
from test_delegation import make, setup_slot
from test_resume_upgrade_authority import queued_wake


def expire_lease(dsn, request_id):
    with connect(dsn) as conn:
        conn.execute("UPDATE aw_resume_requests SET eligible_at=clock_timestamp() WHERE request_id=%s",
                     (request_id,))


def test_reclaimed_wake_does_not_repeat_project_resume_after_delegation_lookup():
    dsn, org, installation, volume = setup_slot()
    make(dsn, volume)
    with connect(dsn) as conn:
        author = authenticate(conn, enroll(conn, installation, org, "author", 1, 1))
        req = request_resume(conn, author, "one-resume")
    calls = []

    async def project(name, args):
        assert (name, args) == ("nf_resume_project", {})
        calls.append(name)
        return dict(workflow_state="IDLE", ready_count=0, awakened=0,
                    recovered=0, pending_review_count=0)

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def delegation(name, args):
            assert name == "nf_delegation_submit"
            started.set()
            await release.wait()
            raise ConnectionError()

        first = asyncio.create_task(run_once(dsn, org, installation, project, delegation))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            expire_lease(dsn, req["request_id"])
            assert await run_once(dsn, org, installation, project, delegation) == "DONE"
        finally:
            release.set()
        assert await first == "SUPERSEDED"

    asyncio.run(exercise())
    with connect(dsn) as conn:
        saved = request_resume(conn, author, "one-resume")
        assert (saved["request_id"], saved["state"]) == (req["request_id"], "DONE")
    assert calls == ["nf_resume_project"]


def test_repeated_worker_crashes_exhaust_the_existing_five_attempt_allowance():
    dsn, org, installation, token, request = queued_wake()
    for number in range(1, 6):
        ident, attempt, _ = take(dsn, org, installation)
        assert str(ident) == request and attempt == number
        # The process exits after its committed claim; the next worker observes expiry.
        expire_lease(dsn, request)
    assert take(dsn, org, installation) is None
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT state,attempts FROM aw_resume_requests WHERE request_id=%s", (request,)
        ).fetchone()
        assert row == {"state": "WAITING_OPERATOR", "attempts": 5}
        old = request_resume(conn, authenticate(conn, token), "old-request")
        assert old["request_id"] == request and old["state"] == "WAITING_OPERATOR"
