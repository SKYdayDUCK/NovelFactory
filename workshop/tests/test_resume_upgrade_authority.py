"""A wakeup retains its submitting member's exact scope across a schema upgrade."""
import asyncio
import os
from uuid import uuid4

import pytest

from agent_workshop import manager_worker
from agent_workshop.admin import enroll
from agent_workshop.auth import authenticate, connect, digest
from agent_workshop.manager import request_resume
from agent_workshop.organization import bootstrap, claim_organization


def queued_wake():
    org, installation = uuid4().hex, uuid4().hex
    dsn = os.environ["AW_DATABASE_URL"]
    with connect(dsn) as c:
        c.execute("INSERT INTO aw_installations VALUES (%s,'test-peer',true)", (installation,))
        bootstrap(c, org, installation_id=installation)
        token = enroll(c, installation, org, "manager", 1, 1)
        wake = request_resume(c, authenticate(c, token), "old-request")
    return dsn, org, installation, token, wake["request_id"]


async def forbidden(*args):
    pytest.fail("the manager worker initiated an unauthorized external request")


@pytest.mark.parametrize("mismatch", ["legacy-grant", "unclaimed", "wrong-owner", "different-author",
                                      "different-tenure", "different-org"])
def test_old_wake_is_fenced_and_a_new_owner_grant_can_resume(mismatch):
    dsn, org, installation, token, wake = queued_wake()
    with connect(dsn) as c:
        if mismatch == "legacy-grant":
            c.execute("UPDATE aw_bindings SET org_member=false WHERE token_digest=%s", (digest(token),))
        elif mismatch == "unclaimed":
            c.execute("UPDATE aw_organizations SET owner_installation_id=NULL WHERE org_id=%s", (org,))
        elif mismatch == "wrong-owner":
            other = uuid4().hex
            c.execute("INSERT INTO aw_installations VALUES (%s,'test-peer',true)", (other,))
            c.execute("UPDATE aw_organizations SET owner_installation_id=%s WHERE org_id=%s", (other, org))
        elif mismatch == "different-author":
            other = enroll(c, installation, org, "author", 1, 1)
            c.execute("UPDATE aw_resume_requests SET binding_digest=%s WHERE request_id=%s", (digest(other), wake))
        elif mismatch == "different-org":
            other = uuid4().hex
            bootstrap(c, other, installation_id=installation)
            foreign = enroll(c, installation, other, "manager", 1, 1)
            c.execute("UPDATE aw_resume_requests SET binding_digest=%s WHERE request_id=%s", (digest(foreign), wake))
        else:
            c.execute("UPDATE aw_resume_requests SET employment_revision=2 WHERE request_id=%s", (wake,))
    assert asyncio.run(manager_worker.run_once(dsn, org, installation, forbidden, forbidden)) == "IDLE"
    with connect(dsn) as c:
        row = c.execute("SELECT state,attempts FROM aw_resume_requests WHERE request_id=%s", (wake,)).fetchone()
        assert row == {"state": "REVOKED", "attempts": 0}
        if mismatch == "unclaimed":
            claim_organization(c, org, installation)
        if mismatch in {"legacy-grant", "unclaimed"}:
            fresh = enroll(c, installation, org, "manager", 1, 1)
            request_resume(c, authenticate(c, fresh), "new-request")

    async def project(name, args):
        assert (name, args) == ("nf_resume_project", {})
        return dict(workflow_state="IDLE", ready_count=0, awakened=0, recovered=0, pending_review_count=0)

    if mismatch in {"legacy-grant", "unclaimed"}:
        assert asyncio.run(manager_worker.run_once(dsn, org, installation, project, forbidden)) == "DONE"
        assert asyncio.run(manager_worker.run_once(dsn, org, installation, forbidden, forbidden)) == "IDLE"


def test_membership_lost_between_claim_and_resume_is_rechecked(monkeypatch):
    dsn, org, installation, token, wake = queued_wake()
    original = manager_worker.take

    def claim_then_revoke(*args):
        claimed = original(*args)
        assert claimed
        with connect(dsn) as c:
            c.execute("UPDATE aw_bindings SET org_member=false WHERE token_digest=%s", (digest(token),))
        return claimed

    monkeypatch.setattr(manager_worker, "take", claim_then_revoke)
    assert asyncio.run(manager_worker.run_once(dsn, org, installation, forbidden, forbidden)) == "REVOKED"
    with connect(dsn) as c:
        row = c.execute("SELECT state,attempts FROM aw_resume_requests WHERE request_id=%s", (wake,)).fetchone()
        assert row == {"state": "REVOKED", "attempts": 1}


def test_timer_only_sweeps_reviews_of_its_own_organization(monkeypatch):
    dsn, org, owner, _, _ = queued_wake()
    other = uuid4().hex
    with connect(dsn) as c:
        c.execute("INSERT INTO aw_installations VALUES (%s,'test-peer',true)", (other,))
    swept = []
    monkeypatch.setattr(manager_worker, "expire_due_reviews", lambda _, org_id: swept.append(org_id))
    assert manager_worker.take(dsn, org, other) is None
    assert swept == []
    assert manager_worker.take(dsn, org, owner)
    assert swept == [org]
