"""Persistent pre-upgrade intents retain their accounting and lose stale send authority."""
import asyncio
from uuid import uuid4

import pytest

from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, connect, digest
from agent_workshop.organization import bootstrap, claim_organization
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import claim, reconcile
from test_delegation import make, ready, setup_slot


@pytest.mark.parametrize("mismatch", [
    "legacy-grant", "unclaimed", "wrong-owner", "different-sender", "different-tenure",
    "different-org", "wrong-slot-installation",
])
def test_old_pending_grant_cannot_send(mismatch):
    dsn, org, installation, token = setup_slot()
    operation = make(dsn, token)
    with connect(dsn) as c:
        if mismatch == "legacy-grant":
            c.execute("UPDATE aw_bindings SET org_member=false WHERE token_digest=%s", (digest(token),))
        elif mismatch == "unclaimed":
            c.execute("UPDATE aw_organizations SET owner_installation_id=NULL WHERE org_id=%s", (org,))
        elif mismatch in {"wrong-owner", "wrong-slot-installation"}:
            other = uuid4().hex
            c.execute("INSERT INTO aw_installations VALUES (%s,'other',true)", (other,))
            if mismatch == "wrong-owner":
                c.execute("UPDATE aw_organizations SET owner_installation_id=%s WHERE org_id=%s", (other, org))
            else:
                c.execute("UPDATE aw_delegation_slots SET installation_id=%s WHERE org_id=%s AND slot_id='scene-A'", (other, org))
        elif mismatch == "different-sender":
            replacement = enroll(c, installation, org, "manager", 1, 1)
            c.execute("UPDATE aw_delegations SET binding_digest=%s WHERE operation_id=%s", (digest(replacement), operation))
        elif mismatch == "different-org":
            other = uuid4().hex
            bootstrap(c, other, installation_id=installation)
            replacement = enroll(c, installation, other, "volume", 1, 1)
            c.execute("UPDATE aw_delegations SET binding_digest=%s WHERE operation_id=%s", (digest(replacement), operation))
        else:
            c.execute("UPDATE aw_bindings SET employment_revision=2 WHERE token_digest=%s", (digest(token),))

    async def forbidden(*args):
        pytest.fail("a stale unsent operation reached its NF peer")

    assert asyncio.run(reconcile(dsn, installation, org, operation, forbidden)) == "IDLE"
    with connect(dsn) as c:
        row = c.execute("""SELECT d.state,h.status,h.amount,h.settled_amount
            FROM aw_delegations d JOIN aw_budget_holds h USING(org_id,operation_id)
            WHERE d.operation_id=%s""", (operation,)).fetchone()
        assert (row["state"], row["status"], row["amount"], row["settled_amount"]) == ("RELEASED", "RELEASED", 1, 0)
        if mismatch == "unclaimed":
            claim_organization(c, org, installation)
        if mismatch in {"legacy-grant", "unclaimed"}:
            replacement = enroll(c, installation, org, "volume", 1, 1)
    if mismatch in {"legacy-grant", "unclaimed"}:
        fresh = make(dsn, replacement, key="fresh-grant", slot="scene-B")
        with connect(dsn) as c:
            assert claim(c, installation, org, fresh)[0] == "submit"


def test_provisioning_cannot_bind_a_slot_to_a_different_installation():
    dsn, org, installation, _ = setup_slot()
    with connect(dsn) as c:
        policy = c.execute("SELECT config FROM aw_delegation_slots WHERE org_id=%s AND slot_id='scene-A'", (org,)).fetchone()["config"]
        other = uuid4().hex
        c.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (other, policy["nf_instance_id"]))
        with pytest.raises(Denied, match="INVALID_SLOT"):
            allow_slot(c, org, other, "foreign-slot", "actor-1", policy)
        assert not c.execute("SELECT 1 FROM aw_delegation_slots WHERE org_id=%s AND slot_id='foreign-slot'", (org,)).fetchone()
        allow_slot(c, org, installation, "owned-slot", "actor-1", policy)


def test_possibly_sent_legacy_intent_looks_up_and_settles_the_original_hold():
    dsn, org, installation, token = setup_slot()
    operation = make(dsn, token)
    with connect(dsn) as c:
        mode, _, sent, *_ = claim(c, installation, org, operation)
        assert mode == "submit"
        c.execute("UPDATE aw_bindings SET org_member=false WHERE token_digest=%s", (digest(token),))
    task, receipt, attempt = (str(uuid4()) for _ in range(3))
    seen = []

    async def existing(tool, payload):
        assert tool == "nf_delegation_get" and payload["operation_id"] == operation
        seen.append(tool)
        settled = len(seen) == 2
        return {**payload, "currency": sent["currency"], "task_id": task, "receipt_ref": receipt,
                "world_ref": {"work_id": sent["work_id"], "branch_id": sent["branch_id"], "world_revision_id": task},
                "generation": 0, "task_state": "ACCEPTED" if settled else "RUNNING",
                "accounting_state": "SETTLED" if settled else "UNKNOWN",
                "actual_amount": "0.2" if settled else "0", "actual_tokens": 12 if settled else 0,
                "attempt_ids": [attempt], "attempts": [{"attempt_id": attempt,
                    "state": "SUCCEEDED" if settled else "UNKNOWN", "actual_amount": "0.2" if settled else None,
                    "actual_tokens": 12 if settled else None}]}

    for state, hold in (("BOUND", "HELD"), ("SETTLED", "SETTLED")):
        ready(dsn, org, operation)
        assert asyncio.run(reconcile(dsn, installation, org, operation, existing)) == state
        with connect(dsn) as c:
            assert c.execute("SELECT status FROM aw_budget_holds WHERE operation_id=%s", (operation,)).fetchone()["status"] == hold
    assert len(seen) == 2
