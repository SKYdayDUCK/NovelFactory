"""Durable intents. Only the reconciliation worker can communicate with NF."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb

from .auth import Denied
from .organization import IDENTIFIER, NF_ROLES, employee, fingerprint, require_employee, schema

MAX_MONEY = {"type": "string", "pattern": r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?$"}
UUID_FIELD = {"type": "string", "format": "uuid"}


def route(conn, org, sender, recipient, scope):
    return bool(
        conn.execute(
            """SELECT 1 FROM aw_edges e JOIN aw_organizations o
        ON o.org_id=e.org_id AND o.active_revision=e.graph_revision
        WHERE e.org_id=%s AND src=%s AND dst=%s AND e.scope_ref=%s AND kind IN ('ASSIGN','ASSIGN_REVIEW')""",
            (org, sender, recipient, scope),
        ).fetchone()
    )


def delegate(conn, session, operation_id, slot_id, max_amount, token_limit, *, _review_request=False):
    org, sender = session.org_id, session.employee_id
    require_employee(conn, session)
    amount = Decimal(max_amount)
    signature = fingerprint([slot_id, str(amount.normalize()), token_limit])
    key = (session.installation_id, org, sender, session.employment_revision, operation_id)
    account = conn.execute(
        "SELECT * FROM aw_budget_accounts WHERE org_id=%s FOR UPDATE", (org,)
    ).fetchone()
    if not account:
        raise Denied("BUDGET_UNCONFIGURED")
    old = conn.execute(
        """SELECT operation_id,state,request_sha FROM aw_delegations
        WHERE (installation_id,org_id,sender_id,sender_revision,client_key)=(%s,%s,%s,%s,%s)""",
        key,
    ).fetchone()
    if old:
        if old["request_sha"] != signature:
            raise Denied("IDEMPOTENCY_MISMATCH")
        return {"operation_id": str(old["operation_id"]), "state": old["state"]}
    slot = conn.execute(
        """SELECT * FROM aw_delegation_slots WHERE org_id=%s AND slot_id=%s
        AND installation_id=%s AND enabled FOR SHARE""",
        (org, slot_id, session.installation_id),
    ).fetchone()
    if not slot:
        raise Denied("VIEW_DENIED")
    if slot.get("review_required", False) and not _review_request:
        raise Denied("REVIEW_REQUEST_REQUIRED")
    target = employee(conn, org, slot["employee_id"])
    policy = slot["config"]
    if (
        target["status"] != "ACTIVE"
        or target["employment_revision"] != slot["employment_revision"]
        or policy["role_id"] not in NF_ROLES[target["role_id"]]
        or not route(conn, org, sender, target["employee_id"], target["scope_ref"])
    ):
        raise Denied("VIEW_DENIED")
    if (
        amount <= 0
        or amount != Decimal(policy["max_amount"])
        or token_limit != policy["token_limit"]
        or account["currency"] != policy["currency"]
    ):
        raise Denied("BUDGET_EXCEEDED")
    committed = conn.execute(
        """SELECT COALESCE(SUM(CASE status WHEN 'HELD' THEN amount
             WHEN 'SETTLED' THEN settled_amount ELSE 0 END),0) AS used FROM aw_budget_holds WHERE org_id=%s""",
        (org,),
    ).fetchone()["used"]
    if committed + amount > account["limit_amount"]:
        raise Denied("BUDGET_EXCEEDED")
    if conn.execute(
        "SELECT 1 FROM aw_delegations WHERE org_id=%s AND slot_id=%s", (org, slot_id)
    ).fetchone():
        raise Denied("SLOT_ALREADY_ASSIGNED")
    # Binding is recorded by digest only; never store a transferable credential in an intent.
    remote_id = str(uuid4())
    payload = {
        **policy,
        "protocol_version": "aw-nf.v1",
        "operation_id": remote_id,
        "budget_reservation_ref": str(uuid4()),
        "max_amount": format(amount, "f"),
        "token_limit": token_limit,
    }
    conn.execute(
        """INSERT INTO aw_delegations
        (org_id,operation_id,client_key,sender_id,sender_revision,binding_digest,slot_id,installation_id,
         payload,request_sha,state,owner_id,owner_revision,deadline)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'REQUESTED',%s,%s,%s)""",
        (
            org,
            remote_id,
            operation_id,
            sender,
            session.employment_revision,
            session.binding_digest,
            slot_id,
            session.installation_id,
            Jsonb(payload),
            signature,
            target["employee_id"],
            target["employment_revision"],
            datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    conn.execute(
        "INSERT INTO aw_budget_holds VALUES (%s,%s,%s,'HELD',NULL)", (org, remote_id, amount)
    )
    return {"operation_id": remote_id, "state": "REQUESTED"}


def handoff(conn, session, operation_id, handoff_id, recipient):
    viewer = require_employee(conn, session)
    org = session.org_id
    item = conn.execute(
        "SELECT * FROM aw_delegations WHERE org_id=%s AND operation_id=%s FOR UPDATE",
        (org, operation_id),
    ).fetchone()
    dest = employee(conn, org, recipient)
    if (
        not item
        or not dest
        or dest["status"] != "ACTIVE"
        or viewer["role_id"] != "workshop_manager"
    ):
        raise Denied("VIEW_DENIED")
    if (
        item["installation_id"] != session.installation_id
        or dest["scope_ref"] != viewer["scope_ref"]
        or item["payload"]["role_id"] not in NF_ROLES[dest["role_id"]]
    ):
        raise Denied("VIEW_DENIED")
    sig = fingerprint([operation_id, recipient, dest["employment_revision"]])
    prev = conn.execute(
        "SELECT request_sha FROM aw_handoffs WHERE org_id=%s AND sender=%s AND handoff_id=%s",
        (org, session.employee_id, handoff_id),
    ).fetchone()
    if prev and prev["request_sha"] != sig:
        raise Denied("IDEMPOTENCY_MISMATCH")
    if not prev:
        conn.execute(
            "INSERT INTO aw_handoffs(org_id,handoff_id,operation_id,sender,request_sha,recipient) VALUES (%s,%s,%s,%s,%s,%s)",
            (org, handoff_id, operation_id, session.employee_id, sig, recipient),
        )
        conn.execute(
            "UPDATE aw_delegations SET owner_id=%s,owner_revision=%s WHERE org_id=%s AND operation_id=%s",
            (recipient, dest["employment_revision"], org, operation_id),
        )
    return {"operation_id": operation_id, "handoff_id": handoff_id, "recipient": recipient}


TOOLS = {
    "aw_delegate": (
        "Reserve organization budget and request a provisioned NF task slot.",
        schema(
            {
                "operation_id": IDENTIFIER,
                "slot_id": IDENTIFIER,
                "max_amount": MAX_MONEY,
                "token_limit": {"type": "integer", "minimum": 1, "maximum": 10000000},
            }
        ),
        delegate,
    ),
    "aw_handoff": (
        "Transfer organization follow-up responsibility. The NF attempt keeps its identity.",
        schema({"operation_id": UUID_FIELD, "handoff_id": IDENTIFIER, "recipient": IDENTIFIER}),
        handoff,
    ),
}
