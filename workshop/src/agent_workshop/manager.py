"""Authorize current reads before exposing any historical projection."""

import base64
import hmac
import json
from uuid import uuid4

from .auth import Denied
from .organization import IDENTIFIER, require_employee, roster, schema

MANAGEMENT = {"workshop_manager", "author"}


def _audience(conn, session):
    viewer = require_employee(conn, session)
    watermark = conn.execute(
        "SELECT * FROM aw_source_cuts WHERE org_id=%s", (session.org_id,)
    ).fetchone()
    return viewer["role_id"] in MANAGEMENT, watermark


def history(conn, session, cursor=None, limit=50):
    whole, mark = _audience(conn, session)
    if not mark:
        return {"source_cut": 0, "events": [], "next_cursor": None}
    key = str(mark["cursor_key"]).encode()
    scope = [
        session.org_id,
        session.employee_id,
        session.employment_revision,
        session.grant_epoch,
        session.installation_id,
        whole,
    ]
    start, cut = 0, mark["cut"]
    if cursor is not None:
        try:
            raw, tag = cursor.split(".")
            data = base64.urlsafe_b64decode(raw)
            if not hmac.compare_digest(hmac.digest(key, data, "sha256").hex(), tag):
                raise ValueError()
            audience, start, cut = json.loads(data)
            if audience != scope or not 0 <= start <= cut <= mark["cut"]:
                raise ValueError()
        except (ValueError, TypeError):
            raise Denied("INVALID_CURSOR") from None
    rows = conn.execute(
        """SELECT seq,source,ref_id,data FROM aw_change_log
        WHERE org_id=%s AND seq>%s AND seq<=%s AND (%s OR (employee_id=%s AND employment_revision=%s))
        ORDER BY seq LIMIT %s""",
        (
            session.org_id,
            start,
            cut,
            whole,
            session.employee_id,
            session.employment_revision,
            limit + 1,
        ),
    ).fetchall()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        data = json.dumps([scope, rows[-1]["seq"], cut], separators=(",", ":")).encode()
        next_cursor = (
            base64.urlsafe_b64encode(data).decode() + "." + hmac.digest(key, data, "sha256").hex()
        )
    return {"source_cut": cut, "events": rows, "next_cursor": next_cursor}


def state(conn, session):
    # This shared org lock linearizes the roster; history endpoints freeze their own source cut.
    viewer = require_employee(conn, session)
    directory = roster(conn, session)
    directory.pop("routes")
    if viewer["role_id"] not in MANAGEMENT:
        return directory
    held = conn.execute(
        """SELECT COALESCE(SUM(amount) FILTER (WHERE status='HELD'),0) AS held,
        COALESCE(SUM(settled_amount) FILTER (WHERE status='SETTLED'),0) AS settled
        FROM aw_budget_holds WHERE org_id=%s""",
        (session.org_id,),
    ).fetchone()
    budget = conn.execute(
        "SELECT currency,limit_amount FROM aw_budget_accounts WHERE org_id=%s", (session.org_id,)
    ).fetchone()
    tasks = conn.execute(
        """SELECT d.state,count(*) AS count FROM aw_delegations d
        WHERE org_id=%s GROUP BY d.state""",
        (session.org_id,),
    ).fetchall()
    reviews = conn.execute(
        "SELECT state,count(*) AS count FROM aw_reviews WHERE org_id=%s GROUP BY state",
        (session.org_id,),
    ).fetchall()
    return {
        **directory,
        "delegations": {r["state"]: r["count"] for r in tasks},
        "reviews": {r["state"]: r["count"] for r in reviews},
        "budget": ({k: str(v) for k, v in {**held, **budget}.items()} if budget else None),
    }


def request_resume(conn, session, operation_id):
    viewer = require_employee(conn, session)
    if viewer["role_id"] not in MANAGEMENT:
        raise Denied("VIEW_DENIED")
    conn.execute(
        """INSERT INTO aw_resume_requests
        (org_id,request_id,installation_id,employee_id,employment_revision,binding_digest,operation_id,state)
        VALUES(%s,%s,%s,%s,%s,%s,%s,'PENDING') ON CONFLICT DO NOTHING""",
        (
            session.org_id,
            str(uuid4()),
            session.installation_id,
            session.employee_id,
            session.employment_revision,
            session.binding_digest,
            operation_id,
        ),
    )
    row = conn.execute(
        """SELECT request_id,state,result FROM aw_resume_requests WHERE org_id=%s AND employee_id=%s
        AND employment_revision=%s AND operation_id=%s AND installation_id=%s""",
        (
            session.org_id,
            session.employee_id,
            session.employment_revision,
            operation_id,
            session.installation_id,
        ),
    ).fetchone()
    if not row:
        raise Denied("VIEW_DENIED")
    return {**row, "request_id": str(row["request_id"])}


TOOLS = {
    "aw_state_get": (
        "Read authorized organization state, review backlog and budget.",
        schema({}),
        state,
    ),
    "aw_history_get": (
        "Read a fixed cut of organization history within the live audience grant.",
        schema(
            {
                "cursor": {"type": "string", "maxLength": 2048},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            [],
        ),
        history,
    ),
    "aw_resume_project": (
        "Persist one authorized wake request for the bound NF project and end this turn.",
        schema({"operation_id": IDENTIFIER}),
        request_resume,
    ),
}
