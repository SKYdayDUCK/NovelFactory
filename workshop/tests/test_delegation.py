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
        bootstrap(c, org, installation_id=install)
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
            "actual_tokens": 12 if final else 0,
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


def test_crash_before_first_send_and_frozen_unsent_intent():
    from agent_workshop.reconcile import claim
    from agent_workshop.recovery import freeze, permit

    dsn, org, install, token = setup_slot()
    op = make(dsn, token)
    with connect(dsn) as c:
        assert claim(c, install, org, op)[0] == "submit"
    # Process disappeared before issuing MCP. The next process queries the original key.
    attempts = []

    async def missing(tool, args):
        attempts.append((tool, args["operation_id"]))
        raise Denied("DELEGATION_NOT_FOUND")

    ready(dsn, org, op)
    assert asyncio.run(reconcile(dsn, install, org, op, missing)) == "REQUESTED"
    with connect(dsn) as c:
        freeze(c, install)
    ready(dsn, org, op)
    assert asyncio.run(reconcile(dsn, install, org, op, missing)) == "REQUESTED"
    with connect(dsn) as c:
        permit(c, install, {})
    ready(dsn, org, op)
    with connect(dsn) as c:
        mode, _, args, *_ = claim(c, install, org, op)
        assert mode == "submit" and args["operation_id"] == op
    assert attempts == [("nf_delegation_get", op)] * 2


def test_contradictory_peer_ledger_freezes_and_preserves_reservation():
    from agent_workshop.recovery import frozen

    dsn, org, install, token = setup_slot()
    op = make(dsn, token)

    async def inconsistent(tool, payload):
        task, receipt, attempt = [str(uuid4()) for _ in range(3)]
        return {
            **payload,
            "task_id": task,
            "receipt_ref": receipt,
            "task_state": "ACCEPTED",
            "world_ref": {
                "work_id": payload["work_id"],
                "branch_id": payload["branch_id"],
                "world_revision_id": task,
            },
            "actual_amount": "0",
            "actual_tokens": 0,
            "attempt_ids": [attempt],
            "accounting_state": "SETTLED",
            "attempts": [
                {
                    "attempt_id": attempt,
                    "state": "UNKNOWN",
                    "actual_amount": None,
                    "actual_tokens": None,
                }
            ],
        }

    assert asyncio.run(reconcile(dsn, install, org, op, inconsistent)) == "WAITING_OPERATOR"
    with connect(dsn) as c:
        assert frozen(c, install)
        assert (
            c.execute("SELECT status FROM aw_budget_holds WHERE operation_id=%s", (op,)).fetchone()[
                "status"
            ]
            == "HELD"
        )
        assert not c.execute(
            "SELECT * FROM aw_cost_observations WHERE operation_id=%s", (op,)
        ).fetchall()


def test_manager_rechecks_author_after_delegation_and_before_resume():
    from agent_workshop.manager import request_resume
    from agent_workshop.manager_worker import run_once

    dsn, org, install, token = setup_slot()
    make(dsn, token)
    with connect(dsn) as c:
        manager = enroll(c, install, org, "author", 1, 1)
        request_resume(c, authenticate(c, manager), "revoke-mid-run")
    calls = []

    async def revoke_during_lookup(tool, args):
        calls.append(tool)
        with connect(dsn) as c:
            c.execute(
                "UPDATE aw_bindings SET revoked=true WHERE token_digest=%s", (digest(manager),)
            )
        raise ConnectionError()

    async def resume(tool, args):
        pytest.fail("revoked author reached NF resume")

    assert asyncio.run(run_once(dsn, org, install, resume, revoke_during_lookup)) == "REVOKED"
    assert calls == ["nf_delegation_submit"]
