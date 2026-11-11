"""Generate old durable requests and exercise them after a real PostgreSQL migration."""
import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from agent_workshop.admin import enroll
from agent_workshop.auth import authenticate, connect
from agent_workshop.reconcile import claim, reconcile
from test_delegation import make, ready, setup_slot

DSN = os.environ["AW_DATABASE_URL"]


def seed(path):
    target = {}
    for kind in ("pending", "sent"):
        _, org, installation, token = setup_slot()
        operation = make(DSN, token)
        target[kind] = dict(org=org, installation=installation, token=token, operation=operation)
    sent = target["sent"]
    remote = dict(task_id=str(uuid4()), receipt_ref=str(uuid4()), attempt_id=str(uuid4()))
    async def lose_reply(tool, payload):
        assert tool == "nf_delegation_submit"
        assert "payload" not in remote
        remote["payload"] = payload
        raise ConnectionError("synthetic successful peer submission with lost reply")
    assert asyncio.run(reconcile(DSN, sent["installation"], sent["org"], sent["operation"], lose_reply)) == "RECONCILING"
    target["remote"] = remote
    pending = target["pending"]
    with connect(DSN) as c:
        assert not c.execute("""SELECT 1 FROM information_schema.columns WHERE table_name='aw_bindings' AND column_name='org_member'""").fetchone()
        target["old_revision"] = c.execute("SELECT version_num FROM aw_alembic_version").fetchone()["version_num"]
        for item in (sent, pending):
            assert authenticate(c, item["token"]).employee_id == "volume"
            assert c.execute("SELECT status FROM aw_budget_holds WHERE operation_id=%s", (item["operation"],)).fetchone()["status"] == "HELD"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(target, stream, default=str)


def check(path):
    from agent_workshop.organization import claim_organization

    state = json.loads(path.read_bytes())
    pending, sent, remote = state["pending"], state["sent"], state["remote"]
    calls = []
    async def forbidden(*args):
        raise AssertionError("unexpected peer write")

    with connect(DSN) as c:
        versions = sorted(r["version_num"] for r in c.execute("SELECT version_num FROM aw_alembic_version"))
        assert versions == [os.environ["AW_EXPECTED_HEAD"]]
        for item in (pending, sent):
            org = c.execute("SELECT owner_installation_id FROM aw_organizations WHERE org_id=%s", (item["org"],)).fetchone()
            assert org["owner_installation_id"] is None
            assert not authenticate(c, item["token"]).org_member
    assert asyncio.run(reconcile(DSN, pending["installation"], pending["org"], pending["operation"], forbidden)) == "IDLE"
    with connect(DSN) as c:
        hold = c.execute("SELECT status,settled_amount FROM aw_budget_holds WHERE operation_id=%s", (pending["operation"],)).fetchone()
        assert hold == {"status":"RELEASED", "settled_amount": Decimal("0")}
        assert c.execute("SELECT state FROM aw_delegations WHERE operation_id=%s", (pending["operation"],)).fetchone()["state"] == "RELEASED"

    async def peer(tool, request):
        assert tool == "nf_delegation_get" and request["operation_id"] == sent["operation"]
        calls.append(tool)
        final = len(calls) == 2
        return {**request, "task_id":remote["task_id"], "receipt_ref":remote["receipt_ref"], "generation":0,
                "currency":remote["payload"]["currency"],
                "world_ref":{"work_id":request["work_id"], "branch_id":request["branch_id"], "world_revision_id":remote["task_id"]},
                "task_state":"ACCEPTED" if final else "RUNNING", "accounting_state":"SETTLED" if final else "UNKNOWN",
                "actual_amount":"0.25" if final else "0", "actual_tokens":11 if final else 0, "attempt_ids":[remote["attempt_id"]],
                "attempts":[{"attempt_id":remote["attempt_id"], "state":"SUCCEEDED" if final else "UNKNOWN",
                             "actual_amount":"0.25" if final else None, "actual_tokens":11 if final else None}]}

    for expected, status in (("BOUND", "HELD"), ("SETTLED", "SETTLED")):
        ready(DSN, sent["org"], sent["operation"])
        assert asyncio.run(reconcile(DSN, sent["installation"], sent["org"], sent["operation"], peer)) == expected
        with connect(DSN) as c:
            hold = c.execute("SELECT status,settled_amount FROM aw_budget_holds WHERE operation_id=%s", (sent["operation"],)).fetchone()
            assert hold["status"] == status
            if status == "SETTLED":
                assert hold["settled_amount"] == Decimal("0.25")
    with connect(DSN) as c:
        claim_organization(c, pending["org"], pending["installation"])
        token = enroll(c, pending["installation"], pending["org"], "volume", 1, 1)
        assert authenticate(c, token).org_member
        assert not authenticate(c, pending["token"]).org_member
    fresh = make(DSN, token, key="fresh-operation", slot="scene-B")
    with connect(DSN) as c:
        assert claim(c, pending["installation"], pending["org"], fresh)[0] == "submit"
    assert calls == ["nf_delegation_get", "nf_delegation_get"]
    report=dict(postgres_versions=versions, source_revision=state["old_revision"], old_unsent_hold="RELEASED",
                old_sent_unknown_hold="HELD", terminal_settlement="0.25", terminal_tokens=11, old_sends_after_migration=0,
                single_original_sent_operation=True, authorized_fresh_sendable_operations=1,
                external_provider_calls=0, observed_peer_calls=calls)
    Path(os.environ["AW_RECEIPT"]).write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,sort_keys=True))


if __name__ == "__main__":
    path = Path(sys.argv[2])
    if sys.argv[1] == "seed":
        seed(path)
    elif sys.argv[1] == "check":
        check(path)
    else:
        raise SystemExit("expected seed or check")
