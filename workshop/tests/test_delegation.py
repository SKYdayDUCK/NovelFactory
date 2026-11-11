import asyncio
import os
from uuid import uuid4

import pytest

from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect, digest
from agent_workshop.delegation import delegate
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import reconcile


def setup_slot():
    org, install = uuid4().hex, uuid4().hex
    dsn = os.environ["AW_DATABASE_URL"]
    ids = [str(uuid4()) for _ in range(4)]
    with connect(dsn) as c:
        c.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (install, ids[0]))
        bootstrap(c, org)
        token = enroll(c, install, org, "volume", 1, 1)
        c.execute("INSERT INTO aw_budget_accounts VALUES (%s,1,'USD')", (org,))
        policy = dict(
            nf_instance_id=ids[0],
            work_id=ids[1],
            branch_id=ids[2],
            actor_binding_ref=ids[3],
            role_id="character_actor",
            task_template_id="character-action",
            scope_ref="scene-A",
            generation=0,
            grant_epoch=1,
            max_amount="1",
            currency="USD",
            token_limit=200,
        )
        for slot in ("scene-A", "scene-B"):
            allow_slot(c, org, install, slot, "actor-1", {**policy, "scope_ref": slot})
    return dsn, org, install, token


def make(dsn, token, key="request-A", slot="scene-A", amount="1"):
    with connect(dsn) as c:
        return delegate(c, authenticate(c, token), key, slot, amount, 200)["operation_id"]


def ready(dsn, org, op):
    with connect(dsn) as c:
        c.execute(
            "UPDATE aw_delegations SET next_check=now() WHERE org_id=%s AND operation_id=%s",
            (org, op),
        )


def test_lost_reply_unknown_then_final_once():
    dsn, org, install, token = setup_slot()
    op = make(dsn, token)
    assert make(dsn, token) == op
    with pytest.raises(Denied, match="IDEMPOTENCY_MISMATCH"):
        make(dsn, token, amount="0.50")
    with pytest.raises(Denied, match="BUDGET_EXCEEDED"):
        make(dsn, token, key="new", slot="scene-B")
    seen = []
    remote_task, receipt, attempt = [str(uuid4()) for _ in range(3)]

    async def invoke(tool, args):
        # A second PG connection can acquire the intent row during the network call.
        with connect(dsn) as conn:
            row = conn.execute(
                "SELECT payload FROM aw_delegations WHERE operation_id=%s FOR UPDATE NOWAIT", (op,)
            ).fetchone()
        seen.append(tool)
        if len(seen) == 1:
            assert tool == "nf_delegation_submit"
            raise ConnectionError()
        assert tool == "nf_delegation_get" and args["operation_id"] == op
        payload = row["payload"]
        final = len(seen) == 3
        detail = dict(
            attempt_id=attempt,
            state="SUCCEEDED" if final else "UNKNOWN",
            actual_amount="0.20" if final else None,
            actual_tokens=12 if final else None,
        )
        return {
            **{
                k: payload[k]
                for k in (
                    "protocol_version",
                    "work_id",
                    "branch_id",
                    "nf_instance_id",
                    "operation_id",
                    "currency",
                )
            },
            "world_ref": {
                "world_revision_id": remote_task,
                "work_id": payload["work_id"],
                "branch_id": payload["branch_id"],
            },
            "task_id": remote_task,
            "receipt_ref": receipt,
            "generation": 0,
            "task_state": "ACCEPTED" if final else "RUNNING",
            "accounting_state": "SETTLED" if final else "UNKNOWN",
            "actual_amount": "0.20" if final else "0",
            "attempt_ids": [attempt],
            "attempts": [detail],
        }

    for outcome in ("RECONCILING", "BOUND", "SETTLED", "IDLE"):
        ready(dsn, org, op)
        assert asyncio.run(reconcile(dsn, install, org, op, invoke)) == outcome
        with connect(dsn) as c:
            hold = c.execute(
                "SELECT * FROM aw_budget_holds WHERE operation_id=%s", (op,)
            ).fetchone()
            assert hold["status"] == ("SETTLED" if outcome in ("SETTLED", "IDLE") else "HELD")
    assert len(seen) == 3


def test_revoke_committed_intent_before_send():
    dsn, org, install, token = setup_slot()
    op = make(dsn, token)
    with connect(dsn) as c:
        c.execute("UPDATE aw_bindings SET revoked=true WHERE token_digest=%s", (digest(token),))

    async def forbidden(*args):
        pytest.fail("network was reached after revocation")

    assert asyncio.run(reconcile(dsn, install, org, op, forbidden)) == "IDLE"
    with connect(dsn) as c:
        assert (
            c.execute("SELECT status FROM aw_budget_holds WHERE operation_id=%s", (op,)).fetchone()[
                "status"
            ]
            == "RELEASED"
        )
