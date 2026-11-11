"""Advance one delegation without holding either service's database lock over MCP."""

import argparse
import asyncio
import json
import os
from decimal import Decimal
from uuid import UUID

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .auth import Denied, connect
from .delegation import route
from .organization import fingerprint, lock_org

GET_FIELDS = (
    "protocol_version",
    "nf_instance_id",
    "work_id",
    "branch_id",
    "operation_id",
    "grant_epoch",
)
TERMINAL = {"ACCEPTED", "FAILED", "CANCELLED"}
ELIGIBLE = ("REQUESTED", "SENDING", "RECONCILING", "BOUND")
SELECT_ITEM = """SELECT * FROM aw_delegations WHERE installation_id=%s AND org_id=%s
                 AND operation_id=%s FOR UPDATE"""


def claim(conn, installation, org, operation, lookup_only=False):
    lock_org(conn, org)
    row = conn.execute(SELECT_ITEM, (installation, org, operation)).fetchone()
    if not row:
        raise Denied("VIEW_DENIED")
    if (
        row["state"] not in ELIGIBLE
        or row["next_check"] > conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
    ):
        return None
    mode = "get"
    if row["state"] == "REQUESTED" and not lookup_only:
        identities = conn.execute(
            """SELECT s.enabled, i.enabled AS installed, b.revoked,
                b.expires_at > clock_timestamp() AS live, b.grant_epoch,
                sender.status AS sender_status, sender.employment_revision AS sender_now,
                t.status AS employee_status,t.employment_revision AS target_now,t.scope_ref,
                s.employee_id,s.employment_revision
            FROM aw_delegation_slots s JOIN aw_installations i USING(installation_id)
            JOIN aw_bindings b ON b.token_digest=%s AND b.installation_id=i.installation_id
            JOIN aw_employees sender ON sender.org_id=s.org_id AND sender.employee_id=%s
            JOIN aw_employees t ON t.org_id=s.org_id AND t.employee_id=s.employee_id
            WHERE s.org_id=%s AND s.slot_id=%s FOR SHARE""",
            (row["binding_digest"], row["sender_id"], org, row["slot_id"]),
        ).fetchone()
        ok = (
            identities
            and identities["enabled"]
            and identities["installed"]
            and identities["live"]
            and not identities["revoked"]
            and identities["sender_status"] == identities["employee_status"] == "ACTIVE"
            and identities["sender_now"] == row["sender_revision"]
            and identities["target_now"] == identities["employment_revision"]
            and route(
                conn, org, row["sender_id"], identities["employee_id"], identities["scope_ref"]
            )
            and row["deadline"] > conn.execute("SELECT clock_timestamp() as t").fetchone()["t"]
        )
        if not ok:
            conn.execute(
                "UPDATE aw_budget_holds SET status='RELEASED',settled_amount=0 WHERE org_id=%s AND operation_id=%s",
                (org, operation),
            )
            conn.execute(
                "UPDATE aw_delegations SET state='RELEASED',last_error='FENCE_BEFORE_SEND' WHERE org_id=%s AND operation_id=%s",
                (org, operation),
            )
            return None
        mode = "submit"
    # On any subsequent claim use get, including a crash between this COMMIT and first send.
    fence = conn.execute(
        """UPDATE aw_delegations SET state='SENDING',fence=fence+1,
        next_check=clock_timestamp()+interval '30 seconds' WHERE org_id=%s AND operation_id=%s
        RETURNING fence""",
        (org, operation),
    ).fetchone()["fence"]
    payload = row["payload"]
    return mode, fence, (payload if mode == "submit" else {k: payload[k] for k in GET_FIELDS})


def validate_receipt(data, payload, previous):
    expected = (
        "protocol_version",
        "nf_instance_id",
        "work_id",
        "branch_id",
        "operation_id",
        "currency",
    )
    if not isinstance(data, dict) or any(data.get(k) != payload[k] for k in expected):
        raise Denied("REMOTE_SCOPE_MISMATCH")
    for k in ("task_id", "receipt_ref"):
        UUID(data[k])
    if (
        data["generation"] < payload["generation"]
        or previous
        and (
            str(previous["task_id"]) != data["task_id"]
            or str(previous["receipt_ref"]) != data["receipt_ref"]
            or previous["generation"] > data["generation"]
        )
    ):
        raise Denied("REMOTE_HISTORY_MISMATCH")
    if (
        data["world_ref"]["work_id"] != payload["work_id"]
        or data["world_ref"]["branch_id"] != payload["branch_id"]
    ):
        raise Denied("REMOTE_SCOPE_MISMATCH")
    if data["accounting_state"] not in {"NO_ATTEMPT", "RESERVED", "UNKNOWN", "SETTLED"}:
        raise Denied("ACCOUNTING_UNAVAILABLE")
    ids = {a["attempt_id"] for a in data["attempts"]}
    if len(ids) != len(data["attempts"]) or ids != set(data["attempt_ids"]):
        raise Denied("ACCOUNTING_UNAVAILABLE")
    amounts = []
    for attempt in data["attempts"]:
        UUID(attempt["attempt_id"])
        if attempt["actual_amount"] is not None:
            value = Decimal(str(attempt["actual_amount"]))
            if not value.is_finite() or value < 0:
                raise Denied("ACCOUNTING_UNAVAILABLE")
            amounts.append(value)
    if Decimal(data["actual_amount"]) != sum(amounts) or (
        data["accounting_state"] == "NO_ATTEMPT" and ids
    ):
        raise Denied("ACCOUNTING_UNAVAILABLE")


def finish(conn, installation, org, operation, fence, data, failure):
    row = conn.execute(SELECT_ITEM, (installation, org, operation)).fetchone()
    if row["fence"] != fence:
        return "SUPERSEDED"
    prev = conn.execute(
        "SELECT * FROM aw_remote_receipts WHERE org_id=%s AND operation_id=%s", (org, operation)
    ).fetchone()
    state = "RECONCILING"
    if failure == "DELEGATION_NOT_FOUND" and prev:
        state, failure = "WAITING_OPERATOR", "REMOTE_HISTORY_MISMATCH"
    elif not failure:
        try:
            validate_receipt(data, row["payload"], prev)
            previously_seen = conn.execute(
                """SELECT DISTINCT attempt_id::text AS id FROM aw_cost_observations
                WHERE org_id=%s AND operation_id=%s""",
                (org, operation),
            ).fetchall()
            if any(old["id"] not in data["attempt_ids"] for old in previously_seen):
                raise Denied("REMOTE_HISTORY_MISMATCH")
            settled = Decimal(data["actual_amount"])
            if settled > Decimal(row["payload"]["max_amount"]):
                raise Denied("BUDGET_DISCREPANCY")
        except (ValueError, ArithmeticError, KeyError, TypeError):
            failure, state = "RECEIPT_DISCREPANCY", "WAITING_OPERATOR"
        else:
            conn.execute(
                """INSERT INTO aw_remote_receipts VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (org_id,operation_id) DO UPDATE SET generation=excluded.generation,
                    task_state=excluded.task_state,accounting_state=excluded.accounting_state""",
                (
                    org,
                    operation,
                    data["task_id"],
                    data["receipt_ref"],
                    data["generation"],
                    data["task_state"],
                    data["accounting_state"],
                ),
            )
            for a in data["attempts"]:
                conn.execute(
                    """INSERT INTO aw_cost_observations
                    (org_id,operation_id,attempt_id,observation_sha,state,actual_amount,actual_tokens)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (
                        org,
                        operation,
                        a["attempt_id"],
                        fingerprint(a),
                        a["state"],
                        a["actual_amount"],
                        a["actual_tokens"],
                    ),
                )
            state = "BOUND"
            if data["task_state"] in TERMINAL and data["accounting_state"] in (
                "NO_ATTEMPT",
                "SETTLED",
            ):
                state = "SETTLED"
                conn.execute(
                    """UPDATE aw_budget_holds SET status='SETTLED',settled_amount=%s
                    WHERE org_id=%s AND operation_id=%s AND status='HELD'""",
                    (settled, org, operation),
                )
    conn.execute(
        """UPDATE aw_delegations SET state=CASE WHEN %s='RECONCILING' AND deadline<=clock_timestamp()
        THEN 'WAITING_OPERATOR' ELSE %s END,last_error=%s,next_check=clock_timestamp()+interval '2 seconds'
        WHERE org_id=%s AND operation_id=%s""",
        (state, state, failure, org, operation),
    )
    return conn.execute(
        "SELECT state FROM aw_delegations WHERE org_id=%s AND operation_id=%s", (org, operation)
    ).fetchone()["state"]


async def reconcile(dsn, installation, org, operation, invoke, lookup_only=False):
    with connect(dsn) as conn:
        claimed = claim(conn, installation, org, operation, lookup_only)
    if not claimed:
        return "IDLE"
    mode, fence, request = claimed
    try:
        async with asyncio.timeout(20):
            data = await invoke("nf_delegation_" + mode, request)
        failure = None
    except Denied as ex:
        failure = (
            str(ex) if str(ex) in {"DELEGATION_NOT_FOUND", "VIEW_DENIED"} else "REMOTE_REJECTED"
        )
        data = None
    except Exception:
        data, failure = None, "REMOTE_UNAVAILABLE"
    with connect(dsn) as conn:
        return finish(conn, installation, org, operation, fence, data, failure)


def remote(params):
    async def invoke(tool, request):
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            await client.initialize()
            if tool not in {t.name for t in (await client.list_tools()).tools}:
                raise Denied("VIEW_DENIED")
            response = await client.call_tool(tool, request)
            if response.isError:
                raise Denied(
                    (response.structuredContent or {})
                    .get("error", {})
                    .get("code", "REMOTE_REJECTED")
                )
            return response.structuredContent

    return invoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument(
        "--lookup-only", action="store_true", help="Recovery mode: only read old operation IDs"
    )
    parser.add_argument("--operation")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "transport", nargs=argparse.REMAINDER, help="-- executable [NF MCP arguments]"
    )
    args = parser.parse_args()
    argv = args.transport[1:] if args.transport[:1] == ["--"] else args.transport
    if not argv or not 1 <= args.limit <= 200:
        parser.error("provide a trusted NF stdio launcher and a bounded batch")
    dsn = os.environ["AW_DATABASE_URL"]
    if args.operation:
        operations = [args.operation]
    else:
        with connect(dsn) as conn:
            operations = [
                str(row["operation_id"])
                for row in conn.execute(
                    """SELECT operation_id FROM aw_delegations
                WHERE installation_id=%s AND org_id=%s AND state=ANY(%s) AND next_check<=clock_timestamp()
                ORDER BY next_check, created_at LIMIT %s""",
                    (args.installation, args.org, list(ELIGIBLE), args.limit),
                )
            ]
    invoke = remote(StdioServerParameters(command=argv[0], args=argv[1:]))
    for op in operations:
        print(
            json.dumps(
                {
                    "operation_id": op,
                    "state": asyncio.run(
                        reconcile(dsn, args.installation, args.org, op, invoke, args.lookup_only)
                    ),
                }
            )
        )


if __name__ == "__main__":
    main()
