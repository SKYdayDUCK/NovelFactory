"""Advance one delegation without holding either service's database lock over MCP."""

import argparse
import asyncio
import json
import os
from decimal import Decimal
from uuid import UUID

from psycopg.types.json import Jsonb
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .auth import Denied, connect
from .delegation import route
from .organization import fingerprint, lock_org
from .recovery import active_epoch, freeze, frozen, record_lookup

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
    organization = lock_org(conn, org)
    lookup_only = frozen(conn, installation) or lookup_only
    epoch = active_epoch(conn, installation) if lookup_only else None
    row = conn.execute(SELECT_ITEM, (installation, org, operation)).fetchone()
    if not row:
        raise Denied("VIEW_DENIED")
    if (
        not (row["state"] in ELIGIBLE or epoch and row["state"] in {"SETTLED", "WAITING_OPERATOR"})
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
                AND b.org_id=s.org_id AND b.org_member
            JOIN aw_employees sender ON (sender.org_id,sender.employee_id,sender.employment_revision)
                = (b.org_id,b.employee_id,b.employment_revision)
            JOIN aw_employees t ON t.org_id=s.org_id AND t.employee_id=s.employee_id
            WHERE s.org_id=%s AND s.slot_id=%s AND s.installation_id=%s
                AND b.employee_id=%s AND b.employment_revision=%s FOR SHARE""",
            (row["binding_digest"], org, row["slot_id"], installation, row["sender_id"], row["sender_revision"]),
        ).fetchone()
        ok = (
            identities
            and organization["owner_installation_id"] == installation
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
            if row["fence"] > 0:
                # A missing receipt observed before a delayed submit commits
                # cannot prove the original intent will never incur a charge.
                conn.execute(
                    """UPDATE aw_delegations SET state='WAITING_OPERATOR',last_error='SUBMISSION_UNCONFIRMED'
                       WHERE installation_id=%s AND org_id=%s AND operation_id=%s""",
                    (installation, org, operation),
                )
                return None
            conn.execute(
                """UPDATE aw_budget_holds SET status='RELEASED',settled_amount=0
                   WHERE org_id=%s AND operation_id=%s AND status='HELD'""",
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
    return (
        mode,
        fence,
        (payload if mode == "submit" else {k: payload[k] for k in GET_FIELDS}),
        lookup_only,
        epoch,
    )


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
            or previous["task_state"] in TERMINAL
            and previous["task_state"] != data["task_state"]
        )
    ):
        raise Denied("REMOTE_HISTORY_MISMATCH")
    if (
        data["world_ref"]["work_id"] != payload["work_id"]
        or data["world_ref"]["branch_id"] != payload["branch_id"]
    ):
        raise Denied("REMOTE_SCOPE_MISMATCH")
    UUID(data["world_ref"]["world_revision_id"])
    if previous and previous["world_ref"] and any(
        previous["world_ref"][key] != data["world_ref"][key]
        for key in ("work_id", "branch_id", "world_revision_id")
    ):
        raise Denied("REMOTE_HISTORY_MISMATCH")
    if data["accounting_state"] not in {"NO_ATTEMPT", "RESERVED", "UNKNOWN", "SETTLED"}:
        raise Denied("ACCOUNTING_UNAVAILABLE")
    ids = {a["attempt_id"] for a in data["attempts"]}
    if len(ids) != len(data["attempts"]) or ids != set(data["attempt_ids"]):
        raise Denied("ACCOUNTING_UNAVAILABLE")
    amounts, tokens, states = [], [], set()
    for attempt in data["attempts"]:
        UUID(attempt["attempt_id"])
        status = attempt["state"]
        states.add(status)
        if status in {"SUCCEEDED", "FAILED"}:
            value = Decimal(str(attempt["actual_amount"]))
            count = attempt["actual_tokens"]
            if not value.is_finite() or value < 0 or type(count) is not int or count < 0:
                raise Denied("ACCOUNTING_UNAVAILABLE")
            amounts.append(value)
            tokens.append(count)
        elif status not in {"PREPARED", "SENT", "UNKNOWN"} or any(
            attempt[k] is not None for k in ("actual_amount", "actual_tokens")
        ):
            raise Denied("ACCOUNTING_UNAVAILABLE")
    expected_state = (
        "UNKNOWN"
        if "UNKNOWN" in states
        else "RESERVED"
        if states & {"PREPARED", "SENT"}
        else "SETTLED"
        if states
        else "NO_ATTEMPT"
    )
    if (
        data["accounting_state"] != expected_state
        or Decimal(data["actual_amount"]) != sum(amounts)
        or type(data["actual_tokens"]) is not int
        or data["actual_tokens"] != sum(tokens)
    ):
        raise Denied("ACCOUNTING_UNAVAILABLE")


def finish(conn, installation, org, operation, fence, data, failure, mode, lookup_only, epoch):
    recovery_mode = frozen(conn, installation, exclusive=True) or lookup_only
    row = conn.execute(SELECT_ITEM, (installation, org, operation)).fetchone()
    if row["fence"] != fence:
        return "SUPERSEDED"
    if epoch is not None:
        conn.execute(
            "DELETE FROM aw_recovery_proofs WHERE installation_id=%s AND org_id=%s AND operation_id=%s AND epoch=%s",
            (installation, org, operation, epoch),
        )
    prev = conn.execute(
        "SELECT * FROM aw_remote_receipts WHERE org_id=%s AND operation_id=%s", (org, operation)
    ).fetchone()
    state = "RECONCILING"
    if failure == "DELEGATION_NOT_FOUND" and prev:
        state, failure = "WAITING_OPERATOR", "REMOTE_HISTORY_MISMATCH"
        freeze(conn, installation, failure)
    elif failure == "DELEGATION_NOT_FOUND" and mode == "get":
        # A trusted exact-key lookup distinguishes a lost send from a lost reply.
        # During restore only a subsequent operator permit may enable new sends.
        state = "REQUESTED"
    elif not failure:
        try:
            validate_receipt(data, row["payload"], prev)
            previously_seen = conn.execute(
                """SELECT attempt_id::text AS id,state,actual_amount,actual_tokens FROM aw_cost_observations
                WHERE org_id=%s AND operation_id=%s""",
                (org, operation),
            ).fetchall()
            current = {a["attempt_id"]: a for a in data["attempts"]}
            if (prev and prev["task_state"] in TERMINAL
                    and prev["accounting_state"] in {"NO_ATTEMPT", "SETTLED"}
                    and set(current) != {old["id"] for old in previously_seen}):
                raise Denied("REMOTE_HISTORY_MISMATCH")
            ranks = {"PREPARED": 0, "SENT": 1, "UNKNOWN": 2, "SUCCEEDED": 3, "FAILED": 3}
            for old in previously_seen:
                item = current.get(old["id"])
                if not item or ranks[item["state"]] < ranks[old["state"]]:
                    raise Denied("REMOTE_HISTORY_MISMATCH")
                if old["state"] in {"SUCCEEDED", "FAILED"} and (
                    item["state"] != old["state"]
                    or old["actual_amount"] != Decimal(item["actual_amount"])
                    or old["actual_tokens"] != item["actual_tokens"]
                ):
                    raise Denied("REMOTE_HISTORY_MISMATCH")
            settled = Decimal(data["actual_amount"])
            if (settled > Decimal(row["payload"]["max_amount"])
                    or data["actual_tokens"] > row["payload"]["token_limit"]):
                raise Denied("BUDGET_DISCREPANCY")
        except Denied as exc:
            state, failure = "WAITING_OPERATOR", str(exc)
            freeze(conn, installation, failure)
        except (ValueError, ArithmeticError, KeyError, TypeError):
            failure, state = "RECEIPT_DISCREPANCY", "WAITING_OPERATOR"
            freeze(conn, installation, failure)
        else:
            conn.execute(
                """INSERT INTO aw_remote_receipts (org_id,operation_id,task_id,receipt_ref,generation,task_state,accounting_state,world_ref) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (org_id,operation_id) DO UPDATE SET generation=excluded.generation,
                    task_state=excluded.task_state,accounting_state=excluded.accounting_state,world_ref=excluded.world_ref""",
                (
                    org,
                    operation,
                    data["task_id"],
                    data["receipt_ref"],
                    data["generation"],
                    data["task_state"],
                    data["accounting_state"],
                    Jsonb(
                        {
                            k: data["world_ref"][k]
                            for k in ("work_id", "branch_id", "world_revision_id")
                        }
                    ),
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
    if epoch is not None and epoch == active_epoch(conn, installation) and mode == "get":
        if state in {"BOUND", "SETTLED"}:
            record_lookup(conn, installation, org, operation, epoch, data)
        elif state == "REQUESTED" and failure == "DELEGATION_NOT_FOUND":
            record_lookup(conn, installation, org, operation, epoch, None)
    conn.execute(
        """UPDATE aw_delegations SET state=CASE WHEN NOT %s AND %s IN ('REQUESTED','RECONCILING') AND deadline<=clock_timestamp()
        THEN 'WAITING_OPERATOR' ELSE %s END,last_error=%s,next_check=clock_timestamp()+interval '2 seconds'
        WHERE org_id=%s AND operation_id=%s""",
        (recovery_mode, state, state, failure, org, operation),
    )
    return conn.execute(
        "SELECT state FROM aw_delegations WHERE org_id=%s AND operation_id=%s", (org, operation)
    ).fetchone()["state"]


async def reconcile(dsn, installation, org, operation, invoke, lookup_only=False):
    with connect(dsn) as conn:
        claimed = claim(conn, installation, org, operation, lookup_only)
    if not claimed:
        return "IDLE"
    mode, fence, request, lookup_only, epoch = claimed
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
        return finish(
            conn, installation, org, operation, fence, data, failure, mode, lookup_only, epoch
        )


def remote(params):
    async def invoke(tool, request):
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            await client.initialize()
            advertised = {t.name for t in (await client.list_tools()).tools}
            response = await client.call_tool(tool, request) if tool in advertised else None
        # Raise after both anyio task groups have closed. An ExceptionGroup would
        # otherwise erase the service error used to distinguish missing history.
        if response is None:
            raise Denied("VIEW_DENIED")
        if response.isError:
            raise Denied(
                (response.structuredContent or {}).get("error", {}).get("code", "REMOTE_REJECTED")
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
                    (
                        args.installation,
                        args.org,
                        [*ELIGIBLE, "SETTLED", "WAITING_OPERATOR"]
                        if args.lookup_only
                        else list(ELIGIBLE),
                        args.limit,
                    ),
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
