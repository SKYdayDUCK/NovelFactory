"""Process one durable author wakeup, then exit. Takes no model credentials."""

import argparse
import asyncio
import json
import os
from .private_files import read_private_bytes

from mcp import StdioServerParameters
from psycopg.types.json import Jsonb

from .auth import connect
from .recovery import frozen
from .reviews_skills import expire_due_reviews
from .reconcile import ELIGIBLE, reconcile, remote

MAX_ATTEMPTS = 5
LEASE_SECONDS = 180


def author_is_live(c, request_id, binding, installation, org):
    return c.execute(
        """SELECT 1 FROM aw_resume_requests r
            JOIN aw_bindings b ON b.token_digest=r.binding_digest
                AND (b.org_id,b.installation_id,b.employee_id,b.employment_revision)
                 = (r.org_id,r.installation_id,r.employee_id,r.employment_revision)
            JOIN aw_organizations o ON o.org_id=r.org_id AND o.owner_installation_id=r.installation_id
            JOIN aw_installations i ON i.installation_id=r.installation_id AND i.enabled
            JOIN aw_employees e ON (e.org_id,e.employee_id,e.employment_revision)
                = (r.org_id,r.employee_id,r.employment_revision)
            WHERE r.request_id=%s AND r.binding_digest=%s AND r.installation_id=%s AND r.org_id=%s
                AND b.org_member AND NOT b.revoked AND b.expires_at>clock_timestamp()
                AND e.status='ACTIVE' AND e.role_id IN ('author','workshop_manager') FOR SHARE""",
        (request_id, binding, installation, org),
    ).fetchone()


def take(dsn, org, installation):
    with connect(dsn) as c:
        if frozen(c, installation):
            return None
        owns_org = c.execute(
            """SELECT 1 FROM aw_organizations o JOIN aw_installations i
               ON i.installation_id=o.owner_installation_id AND i.enabled
               WHERE o.org_id=%s AND i.installation_id=%s FOR SHARE""",
            (org, installation),
        ).fetchone()
        if owns_org:
            expire_due_reviews(c, org)
            # Bound the wait even when an unavailable peer can never produce a
            # settled receipt. Preserve the intent for the submitting manager.
            c.execute("""
                WITH expired AS (SELECT r.request_id FROM aw_resume_requests r
                  JOIN aw_delegations d ON (d.org_id,d.operation_id)=(r.org_id,r.after_delegation_id)
                  WHERE r.org_id=%s AND r.installation_id=%s AND r.state='WAITING_DELEGATION'
                    AND (d.deadline<=clock_timestamp() OR d.state IN ('RELEASED','WAITING_OPERATOR'))
                  ORDER BY d.deadline,r.request_id LIMIT 32 FOR UPDATE OF r SKIP LOCKED)
                UPDATE aw_resume_requests r SET state='WAITING_OPERATOR',
                    result='{"error":"DELEGATION_INCOMPLETE"}'::jsonb
                FROM expired WHERE r.request_id=expired.request_id""", (org, installation))
        row = c.execute(
            """SELECT * FROM aw_resume_requests WHERE org_id=%s AND installation_id=%s
            AND state IN ('PENDING','RUNNING') AND eligible_at<=clock_timestamp()
            ORDER BY eligible_at,request_id LIMIT 1 FOR UPDATE SKIP LOCKED""",
            (org, installation),
        ).fetchone()
        if not row:
            return None
        live = author_is_live(c, row["request_id"], row["binding_digest"], installation, org)
        if not live:
            c.execute(
                "UPDATE aw_resume_requests SET state='REVOKED' WHERE request_id=%s",
                (row["request_id"],),
            )
            return None
        if row["state"] == "RUNNING" and row["attempts"] >= MAX_ATTEMPTS:
            c.execute(
                """UPDATE aw_resume_requests SET state='WAITING_OPERATOR',result=%s
                WHERE request_id=%s""",
                (Jsonb({"error": "WORKER_LEASE_EXPIRED"}), row["request_id"]),
            )
            return None
        n = row["attempts"] + 1
        c.execute(
            """UPDATE aw_resume_requests SET attempts=%s,state='RUNNING',
            eligible_at=clock_timestamp()+%s * interval '1 second' WHERE request_id=%s""",
            (n, LEASE_SECONDS, row["request_id"]),
        )
        return row["request_id"], n, row["binding_digest"]


def before_peer_call(dsn, org, installation, request):
    ident, attempt, binding = request
    with connect(dsn) as c:
        paused = frozen(c, installation)
        current = c.execute(
            """SELECT 1 FROM aw_resume_requests WHERE request_id=%s AND attempts=%s
            AND state='RUNNING' AND eligible_at>clock_timestamp() FOR UPDATE""",
            (ident, attempt),
        ).fetchone()
        if not current:
            return "SUPERSEDED"
        if paused:
            c.execute(
                """UPDATE aw_resume_requests SET state='PENDING',eligible_at=clock_timestamp()
                WHERE request_id=%s""", (ident,),
            )
            return "RECOVERY_FENCED"
        if not author_is_live(c, ident, binding, installation, org):
            c.execute("UPDATE aw_resume_requests SET state='REVOKED' WHERE request_id=%s", (ident,))
            return "REVOKED"
        # Give the bounded peer call a full lease after any time spent reconciling.
        renewed = c.execute(
            """UPDATE aw_resume_requests SET eligible_at=clock_timestamp()+%s * interval '1 second'
            WHERE request_id=%s AND eligible_at>clock_timestamp() RETURNING request_id""",
            (LEASE_SECONDS, ident),
        ).fetchone()
        return None if renewed else "SUPERSEDED"


async def run_once(dsn, org, installation, nf_project, nf_delegation):
    request = take(dsn, org, installation)
    if request is None:
        return "IDLE"
    ident, attempt, _ = request
    try:
        # Only broker-configured commands may select a project. An MCP caller never provides a path.
        with connect(dsn) as c:
            binding = c.execute(
                """SELECT w.after_delegation_id,d.payload, d.state, r.task_id, r.receipt_ref, r.generation
                   FROM aw_resume_requests w LEFT JOIN aw_delegations d ON d.org_id=w.org_id
                     AND d.installation_id=w.installation_id AND d.operation_id=w.after_delegation_id
                   LEFT JOIN aw_remote_receipts r ON (r.org_id,r.operation_id)=(d.org_id,d.operation_id)
                   WHERE w.request_id=%s""", (ident,),
            ).fetchone()
            args = {}
            if binding["after_delegation_id"]:
                if binding["state"] != "SETTLED" or binding["receipt_ref"] is None:
                    raise ValueError("Completion receipt unavailable")
                args["expected_scope"] = {
                    **{k: binding["payload"][k] for k in ("work_id", "branch_id", "nf_instance_id", "grant_epoch")},
                    "installation_id": str(installation), "org_id": str(org),
                    "operation_id": str(binding["after_delegation_id"]), "task_id": str(binding["task_id"]),
                    "receipt_ref": str(binding["receipt_ref"]), "generation": binding["generation"],
                }
            # Pre-authorized completion wakes must not initiate unrelated
            # delegation submissions. Ordinary manager resumes retain their recovery pass.
            operations = [] if args else c.execute(
                """SELECT operation_id FROM aw_delegations WHERE org_id=%s
                AND installation_id=%s AND state=ANY(%s) AND next_check<=clock_timestamp()
                ORDER BY next_check LIMIT 4""",
                (org, installation, list(ELIGIBLE)),
            ).fetchall()
        for row in operations:
            if blocked := before_peer_call(dsn, org, installation, request):
                return blocked
            await reconcile(dsn, installation, org, str(row["operation_id"]), nf_delegation)
        # Recovery may have frozen the installation or a new worker may own this wake.
        if blocked := before_peer_call(dsn, org, installation, request):
            return blocked
        async with asyncio.timeout(20):
            receipt = await nf_project("nf_resume_project", args)
        result = {
            key: receipt[key]
            for key in (
                "workflow_state",
                "ready_count",
                "awakened",
                "recovered",
                "pending_review_count",
            )
        }
        state = "DONE"
    except Exception:
        state = "WAITING_OPERATOR" if attempt >= MAX_ATTEMPTS else "PENDING"
        result = {"error": "PEER_UNAVAILABLE"}
    with connect(dsn) as c:
        updated = c.execute(
            """UPDATE aw_resume_requests SET state=%s,result=%s,
            eligible_at=clock_timestamp()+interval '3 seconds' WHERE request_id=%s AND attempts=%s AND state='RUNNING'
            RETURNING state""",
            (state, Jsonb(result), ident, attempt),
        ).fetchone()
        return updated["state"] if updated else "SUPERSEDED"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--route",
        required=True,
        help="Private operator manifest binding one organization to two trusted stdio launchers",
    )
    args = p.parse_args()
    try:
        cfg = json.loads(read_private_bytes(args.route, 16384))
    except (ValueError, OSError):
        p.error("invalid route file")

    def launcher(name):
        argv = cfg[name]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(v, str) and v for v in argv)
        ):
            p.error("expected a trusted launcher argument list")
        return remote(StdioServerParameters(command=argv[0], args=argv[1:]))

    print(
        asyncio.run(
            run_once(
                os.environ["AW_DATABASE_URL"],
                cfg["org_id"],
                cfg["installation_id"],
                launcher("project"),
                launcher("delegation"),
            )
        )
    )


if __name__ == "__main__":
    main()
