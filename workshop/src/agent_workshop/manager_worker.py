"""Process one durable author wakeup, then exit. Takes no model credentials."""

import argparse
import asyncio
import json
import os
import stat

from mcp import StdioServerParameters
from psycopg.types.json import Jsonb

from .auth import connect
from .reconcile import ELIGIBLE, reconcile, remote


def take(dsn, org, installation):
    with connect(dsn) as c:
        row = c.execute(
            """SELECT * FROM aw_resume_requests WHERE org_id=%s AND installation_id=%s
            AND state IN ('PENDING','RUNNING') AND eligible_at<=clock_timestamp()
            ORDER BY eligible_at,request_id LIMIT 1 FOR UPDATE SKIP LOCKED""",
            (org, installation),
        ).fetchone()
        if not row:
            return None
        live = c.execute(
            """SELECT 1 FROM aw_bindings b JOIN aw_installations i USING(installation_id)
            JOIN aw_employees e ON (b.org_id,b.employee_id)=(e.org_id,e.employee_id)
            WHERE b.token_digest=%s AND i.enabled AND NOT b.revoked AND b.expires_at>clock_timestamp()
            AND b.installation_id=%s AND b.org_id=%s AND e.status='ACTIVE' AND b.employment_revision=e.employment_revision
            AND e.role_id IN ('author','workshop_manager') FOR SHARE""",
            (row["binding_digest"], installation, org),
        ).fetchone()
        if not live:
            c.execute(
                "UPDATE aw_resume_requests SET state='REVOKED' WHERE request_id=%s",
                (row["request_id"],),
            )
            return None
        n = row["attempts"] + 1
        c.execute(
            """UPDATE aw_resume_requests SET attempts=%s,state='RUNNING',
            eligible_at=clock_timestamp()+interval '3 minutes' WHERE request_id=%s""",
            (n, row["request_id"]),
        )
        return row["request_id"], n


async def run_once(dsn, org, installation, nf_project, nf_delegation):
    request = take(dsn, org, installation)
    if request is None:
        return "IDLE"
    ident, attempt = request
    try:
        # Only broker-configured commands may select a project. An MCP caller never provides a path.
        with connect(dsn) as c:
            operations = c.execute(
                """SELECT operation_id FROM aw_delegations WHERE org_id=%s
                AND installation_id=%s AND state=ANY(%s) AND next_check<=clock_timestamp()
                ORDER BY next_check LIMIT 4""",
                (org, installation, list(ELIGIBLE)),
            ).fetchall()
        for row in operations:
            await reconcile(dsn, installation, org, str(row["operation_id"]), nf_delegation)
        async with asyncio.timeout(20):
            receipt = await nf_project("nf_resume_project", {})
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
        state = "WAITING_OPERATOR" if attempt >= 5 else "PENDING"
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
    with os.fdopen(os.open(args.route, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as f:
        st = os.fstat(f.fileno())
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
            or st.st_size > 16384
        ):
            p.error("invalid route file")
        cfg = json.load(f)

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
