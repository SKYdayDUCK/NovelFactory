"""Authorize current reads before exposing any historical projection."""

import base64
import hmac
import json
from uuid import UUID, uuid4

from .auth import Denied
from .delegation import UUID_FIELD
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
        """SELECT seq,source,ref_id,data FROM aw_change_log h
        WHERE org_id=%s AND seq>%s AND seq<=%s AND (%s OR (employee_id=%s AND employment_revision=%s))
        AND CASE
            WHEN source IN ('harness.skill.frozen','harness.skill.withdrawn') THEN h.data->>'installation_id'=%s
            WHEN source IN ('aw_remote_receipts','aw_budget_holds','aw_delegations') THEN EXISTS (
                SELECT 1 FROM aw_delegations d WHERE d.org_id=h.org_id AND
                    d.operation_id=h.ref_id::uuid AND d.installation_id=%s)
            WHEN source='aw_resume_requests' THEN EXISTS (
                SELECT 1 FROM aw_resume_requests r WHERE r.org_id=h.org_id AND
                    r.request_id=h.ref_id::uuid AND r.installation_id=%s)
            WHEN source='aw_reviews' AND NOT %s THEN EXISTS (
                SELECT 1 FROM aw_reviews r WHERE r.org_id=h.org_id AND r.review_id=h.ref_id::uuid
                AND (r.execution<>'BUDGETED_NF' OR EXISTS (
                    SELECT 1 FROM aw_delegations d JOIN aw_delegation_slots s USING(org_id,slot_id)
                    WHERE d.org_id=r.org_id AND d.operation_id=r.operation_id
                    AND d.installation_id=%s AND s.installation_id=d.installation_id)))
            WHEN source='aw_deliveries' THEN EXISTS (
                SELECT 1 FROM aw_message_origins o JOIN aw_messages m USING(org_id,message_id)
                JOIN aw_bindings b ON b.token_digest=o.token_digest
                WHERE o.org_id=h.org_id AND o.message_id=h.ref_id::uuid AND b.installation_id=%s
                AND b.org_member AND (b.org_id,b.employee_id,b.employment_revision)=
                    (m.org_id,m.sender,m.sender_revision))
            ELSE true END
        ORDER BY seq LIMIT %s""",
        (
            session.org_id,
            start,
            cut,
            whole,
            session.employee_id,
            session.employment_revision,
            session.installation_id,
            session.installation_id,
            session.installation_id,
            whole,
            session.installation_id,
            session.installation_id,
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
        WHERE org_id=%s AND installation_id=%s GROUP BY d.state""",
        (session.org_id, session.installation_id),
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


def progress(conn, session, operation_id):
    viewer = require_employee(conn, session)
    management = viewer["role_id"] in MANAGEMENT
    row = conn.execute(
        """SELECT d.slot_id,d.owner_id,d.state,d.last_error,r.task_id::text,r.generation,r.task_state,
                  r.world_ref,r.accounting_state,h.status AS hold
           FROM aw_delegations d JOIN aw_budget_holds h USING(org_id,operation_id)
           LEFT JOIN aw_remote_receipts r USING(org_id,operation_id)
           WHERE d.org_id=%s AND d.installation_id=%s AND d.operation_id=%s
             AND (%s OR (d.owner_id,d.owner_revision)=(%s,%s)
                     OR (d.sender_id,d.sender_revision)=(%s,%s))""",
        (
            session.org_id,
            session.installation_id,
            operation_id,
            management,
            session.employee_id,
            session.employment_revision,
            session.employee_id,
            session.employment_revision,
        ),
    ).fetchone()
    if not row:
        raise Denied("VIEW_DENIED")
    result = {key: row[key] for key in ("slot_id", "owner_id", "state", "last_error", "task_state")}
    if management:
        result.update(
            {
                key: row[key]
                for key in ("task_id", "generation", "world_ref", "accounting_state", "hold")
            }
        )
    return result


def request_resume(conn, session, operation_id, after_delegation_id=None):
    viewer = require_employee(conn, session)
    if viewer["role_id"] not in MANAGEMENT:
        raise Denied("VIEW_DENIED")
    state = "PENDING"
    if after_delegation_id is not None:
        after_delegation_id = str(UUID(str(after_delegation_id)))
        # Serialize registration and settlement so a committed signal cannot be missed.
        delegation = conn.execute(
            """SELECT state,deadline>clock_timestamp() AS live FROM aw_delegations WHERE org_id=%s AND installation_id=%s
               AND operation_id=%s FOR UPDATE""",
            (session.org_id, session.installation_id, after_delegation_id),
        ).fetchone()
        if not delegation:
            raise Denied("VIEW_DENIED")
        if delegation["state"] in {"WAITING_OPERATOR", "RELEASED"} or not delegation["live"] and delegation["state"] != "SETTLED":
            state = "WAITING_OPERATOR"
        elif delegation["state"] != "SETTLED":
            state = "WAITING_DELEGATION"
    conn.execute(
        """INSERT INTO aw_resume_requests
        (org_id,request_id,installation_id,employee_id,employment_revision,binding_digest,operation_id,state,after_delegation_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (
            session.org_id,
            str(uuid4()),
            session.installation_id,
            session.employee_id,
            session.employment_revision,
            session.binding_digest,
            operation_id,
            state,
            after_delegation_id,
        ),
    )
    row = conn.execute(
        """SELECT request_id,state,result,after_delegation_id::text FROM aw_resume_requests WHERE org_id=%s AND employee_id=%s
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
    if row.pop("after_delegation_id") != after_delegation_id:
        raise Denied("IDEMPOTENCY_MISMATCH")
    return {**row, "request_id": str(row["request_id"])}


TOOLS = {
    "aw_state_get": (
        "Read authorized organization state, review backlog and budget.",
        schema({}),
        state,
    ),
    "aw_progress_get": (
        "Read an authorized scene or role delegation and its latest NF receipt.",
        schema({"operation_id": UUID_FIELD}),
        progress,
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
        schema({"operation_id": IDENTIFIER, "after_delegation_id": UUID_FIELD}, ["operation_id"]),
        request_resume,
    ),
}
