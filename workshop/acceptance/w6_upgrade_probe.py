"""Seed with the installed release; resume the same operation after upgrading and restarting."""

import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.reconcile import reconcile
from test_delegation import make, ready, setup_slot


def receipt(request, reference, settled):
    fields = ("protocol_version", "work_id", "branch_id", "nf_instance_id", "operation_id")
    return {
        **{k: request[k] for k in fields},
        "currency": "USD",
        "task_id": reference["task"],
        "receipt_ref": reference["receipt"],
        "generation": 0,
        "world_ref": {
            "work_id": request["work_id"],
            "branch_id": request["branch_id"],
            "world_revision_id": reference["world"],
        },
        "task_state": "ACCEPTED" if settled else "RUNNING",
        "accounting_state": "SETTLED" if settled else "UNKNOWN",
        "actual_amount": "0.20" if settled else "0",
        "actual_tokens": 9 if settled else 0,
        "attempt_ids": [reference["attempt"]],
        "attempts": [
            {
                "attempt_id": reference["attempt"],
                "state": "SUCCEEDED" if settled else "UNKNOWN",
                "actual_amount": "0.20" if settled else None,
                "actual_tokens": 9 if settled else None,
            }
        ],
    }


def seed(path):
    dsn, org, inst, token = setup_slot()
    with connect(dsn) as conn:
        manager = authenticate(conn, enroll(conn, inst, org, "manager", 1, 1))
        if conn.execute("SELECT to_regclass('aw_preset_members') AS t").fetchone()["t"]:
            from agent_workshop import presets

            plan = presets.preview(conn, manager, "Standard")
            changed = presets.apply_preset(
                conn, manager, "Standard", "original-switch", plan["revision"], plan["preview_sha"]
            )
            assert changed == presets.apply_preset(
                conn, manager, "Standard", "original-switch", plan["revision"], plan["preview_sha"]
            )
    op = make(dsn, token)
    refs = {key: str(uuid4()) for key in ("task", "receipt", "attempt", "world")}

    async def pending(tool, request):
        assert tool == "nf_delegation_submit" and request["operation_id"] == op
        return receipt(request, refs, False)

    assert asyncio.run(reconcile(dsn, inst, org, op, pending)) == "BOUND"
    foreign = str(uuid4())
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (foreign, str(uuid4())))
        fenced = conn.execute(
            "SELECT to_regclass('aw_recovery_fences') IS NOT NULL AS t"
        ).fetchone()["t"]
        if fenced:
            from agent_workshop.recovery import freeze

            freeze(conn, inst)
        member = conn.execute("""SELECT EXISTS(SELECT FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='aw_bindings' AND column_name='org_member') AS has""").fetchone()[
            "has"
        ]
    path.write_text(
        json.dumps(
            {
                "org": org,
                "installation": inst,
                "operation": op,
                "references": refs,
                "owned_source": member,
                "foreign_installation": foreign,
                "frozen_before_upgrade": fenced,
            }
        )
    )


def restart(dsn, initial):
    from agent_workshop.manager_worker import run_once
    from agent_workshop.recovery import freeze, frozen, permit

    org, inst, op = (initial[k] for k in ("org", "installation", "operation"))
    refs = initial["references"]
    calls = []

    async def resumed(tool, request):
        assert (tool, request["operation_id"]) == ("nf_delegation_get", op)
        calls.append(tool)
        return receipt(request, refs, True)

    async def no_peer(tool, request):
        raise AssertionError(f"Unexpected peer call {tool}")

    with connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT status FROM aw_budget_holds WHERE operation_id=%s", (op,)
            ).fetchone()["status"]
            == "HELD"
        )
        member = conn.execute(
            "SELECT owner_installation_id FROM aw_organizations WHERE org_id=%s", (org,)
        ).fetchone()
        assert member["owner_installation_id"] == (inst if initial["owned_source"] else None)
        assert frozen(conn, inst) == initial["frozen_before_upgrade"]
        with pytest.raises(Denied, match="ORGANIZATION_UNAVAILABLE"):
            enroll(conn, initial["foreign_installation"], org, "volume", 1, 1)
        freeze(conn, inst)
    with pytest.raises(Denied, match="VIEW_DENIED"):
        asyncio.run(reconcile(dsn, initial["foreign_installation"], org, op, no_peer))
    assert asyncio.run(run_once(dsn, org, inst, no_peer, no_peer)) == "IDLE"
    ready(dsn, org, op)
    assert asyncio.run(reconcile(dsn, inst, org, op, resumed)) == "SETTLED"
    with connect(dsn) as conn:
        hold = conn.execute(
            "SELECT status,settled_amount FROM aw_budget_holds WHERE operation_id=%s", (op,)
        ).fetchone()
        assert hold == {"status": "SETTLED", "settled_amount": Decimal("0.20")}
        ledger = conn.execute(
            "SELECT ledger FROM aw_recovery_proofs WHERE installation_id=%s AND operation_id=%s",
            (inst, op),
        ).fetchone()["ledger"]
        assert (
            ledger["receipt_ref"] == refs["receipt"]
            and ledger["attempts"][0]["attempt_id"] == refs["attempt"]
        )
        # A wrong partner history cannot authorize the operator to release the fence.
        with pytest.raises(Denied, match="REMOTE_HISTORY_MISMATCH"):
            permit(conn, inst, {})
        permit(conn, inst, {op: ledger})
    assert asyncio.run(reconcile(dsn, inst, org, op, no_peer)) == "IDLE"
    assert calls == ["nf_delegation_get"]
    return {
        "operation": op,
        "receipt": ledger,
        "hold": "SETTLED",
        "actual_amount": "0.20",
        "lookups_after_restart": calls,
        "foreign_installation_denied": True,
    }


if __name__ == "__main__":
    action, private = sys.argv[1:]
    path = Path(private)
    if action == "seed":
        seed(path)
    elif action == "restart":
        print(json.dumps(restart(os.environ["AW_DATABASE_URL"], json.loads(path.read_text()))))
    elif action == "schema":
        from w5_upgrade_probe import schema

        print(json.dumps(schema(os.environ["AW_DATABASE_URL"])))
    else:
        raise SystemExit("unknown upgrade probe")
