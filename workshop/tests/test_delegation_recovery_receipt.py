import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest

from agent_workshop.auth import Denied, connect
from agent_workshop.reconcile import reconcile
from agent_workshop.recovery import LEDGER_FIELDS, freeze, frozen, permit
from test_delegation import make, ready, setup_slot


@pytest.mark.parametrize(
    "change", ["identical", "legacy_world_ref", "world_revision", "extra_attempt", "older_settlement"],
)
def test_recovery_preserves_final_receipt_and_organization_settlement(change):
    dsn, org, install, token = setup_slot()
    operation = make(dsn, token)
    task, receipt, original_revision, attempt = (str(uuid4()) for _ in range(4))
    original_attempt = {
        "attempt_id": attempt,
        "state": "SUCCEEDED",
        "actual_amount": "0.20",
        "actual_tokens": 12,
    }
    reply = {}

    async def submit(tool, request):
        assert tool == "nf_delegation_submit"
        reply.update(
            request,
            task_id=task, receipt_ref=receipt, task_state="ACCEPTED",
            world_ref={
                "work_id": request["work_id"],
                "branch_id": request["branch_id"],
                "world_revision_id": original_revision,
            },
            accounting_state="SETTLED", actual_amount="0.20", actual_tokens=12,
            attempts=[original_attempt], attempt_ids=[attempt],
        )
        return reply

    assert asyncio.run(reconcile(dsn, install, org, operation, submit)) == "SETTLED"
    original_settlement = Decimal("0.20")
    with connect(dsn) as conn:
        if change == "older_settlement":
            # A pre-fix lookup could import new observations without updating a closed hold.
            original_settlement = Decimal("0.10")
            conn.execute(
                "UPDATE aw_budget_holds SET settled_amount=%s WHERE operation_id=%s",
                (original_settlement, operation),
            )
        elif change == "legacy_world_ref":
            # A receipt written by w003 predates the nullable WorldRef added in w003a.
            conn.execute(
                "UPDATE aw_remote_receipts SET world_ref=NULL WHERE operation_id=%s",
                (operation,),
            )
        freeze(conn, install)
    if change == "world_revision":
        reply["world_ref"] = {**reply["world_ref"], "world_revision_id": str(uuid4())}
    elif change == "extra_attempt":
        extra = {**original_attempt, "attempt_id": str(uuid4())}
        reply.update(
            attempts=[original_attempt, extra], attempt_ids=[attempt, extra["attempt_id"]],
            actual_amount="0.40", actual_tokens=24,
        )

    async def lookup(tool, request):
        assert tool == "nf_delegation_get" and request["operation_id"] == operation
        return reply

    ready(dsn, org, operation)
    outcome = asyncio.run(reconcile(dsn, install, org, operation, lookup))
    ledger = {operation: {key: reply[key] for key in LEDGER_FIELDS}}
    with connect(dsn) as conn:
        try:
            permit(conn, install, ledger)
        except Denied as exc:
            assert str(exc) == "REMOTE_HISTORY_MISMATCH"
        saved = conn.execute(
            "SELECT h.status, h.settled_amount, r.world_ref FROM aw_remote_receipts r "
            "JOIN aw_budget_holds h USING(org_id, operation_id) WHERE operation_id=%s",
            (operation,),
        ).fetchone()
        observations = conn.execute(
            "SELECT attempt_id::text AS id FROM aw_cost_observations WHERE operation_id=%s",
            (operation,),
        ).fetchall()
        divergent = change in {"world_revision", "extra_attempt", "older_settlement"}
        assert (outcome, frozen(conn, install)) == (
            "WAITING_OPERATOR" if divergent else "SETTLED", divergent,
        ), {"local_hold": saved, "nf_total": reply["actual_amount"], "observations": observations}
        assert saved["world_ref"]["world_revision_id"] == original_revision
        assert (saved["status"], saved["settled_amount"]) == ("SETTLED", original_settlement)
        assert [row["id"] for row in observations] == [attempt]
