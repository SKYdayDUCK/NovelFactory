"""Addressed, durable organization mail. Acknowledging never executes a novel task."""

from contextlib import contextmanager
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .auth import Denied
from .organization import (
    DOWN,
    IDENTIFIER,
    POSITIVE,
    UP,
    employee,
    fingerprint,
    lock_org,
    require_employee,
    schema,
)

KINDS = sorted(
    {
        "REPORT",
        "ASK",
        "EXCEPTION",
        "PROPOSE_SCRIPT",
        "REQUEST_REVIEW",
        "INCIDENT",
        *(k for keys in DOWN.values() for k in keys),
        *(k for keys in UP.values() for k in keys),
    }
)
INCIDENT_CODES = ("SERVICE_OFFLINE", "STALE_LEASE", "RECEIPT_MISSING", "QUEUE_PRESSURE")
MAX_HOPS = 6
ID = {"type": "string", "format": "uuid"}
TTL = {"type": "integer", "minimum": 1, "maximum": 86400}
KIND = {"type": "string", "enum": KINDS}
COMMON = {"operation_id": IDENTIFIER, "recipient": IDENTIFIER, "scope_ref": IDENTIFIER}


@contextmanager
def operation(conn, session, action, key, fields):
    pk = (session.org_id, session.employee_id, session.employment_revision, key)
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (fingerprint(pk),))
    digest = fingerprint([action, fields])
    row = conn.execute(
        """SELECT request_sha,receipt FROM aw_message_operations WHERE
        (org_id,employee_id,employment_revision,operation_id)=(%s,%s,%s,%s)""",
        pk,
    ).fetchone()
    if row and row["request_sha"] != digest:
        raise Denied("IDEMPOTENCY_MISMATCH")
    receipt = dict(row["receipt"]) if row else {}
    yield bool(row), receipt
    if not row:
        conn.execute(
            "INSERT INTO aw_message_operations VALUES (%s,%s,%s,%s,%s,%s)",
            (*pk, digest, Jsonb(receipt)),
        )


def _peer(conn, org, name, scope):
    row = employee(conn, org, name)
    if not row or row["status"] != "ACTIVE" or row["scope_ref"] != scope:
        raise Denied("ROUTE_DENIED")
    return row


def _uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        raise Denied("INVALID_REFERENCE") from None


def issue_route(
    conn,
    session,
    operation_id,
    sender,
    recipient,
    kind,
    scope_ref,
    ttl_seconds,
    max_uses,
    expected_revision,
):
    org = lock_org(conn, session.org_id, write=True)
    issuer = require_employee(conn, session)
    if (issuer["role_id"] != "workshop_manager" or issuer["scope_ref"] != scope_ref
            or kind not in KINDS or kind == "INCIDENT"):
        raise Denied("ROUTE_DENIED")
    fields = [sender, recipient, kind, scope_ref, ttl_seconds, max_uses, expected_revision]
    with operation(conn, session, "ISSUE_ROUTE", operation_id, fields) as (old, receipt):
        if old:
            return receipt
        if (
            expected_revision != org["active_revision"]
            or not 0 < ttl_seconds <= 86400
            or not 0 < max_uses <= 100
            or sender == recipient
        ):
            raise Denied("INVALID_ROUTE_GRANT")
        src = _peer(conn, session.org_id, sender, scope_ref)
        dst = _peer(conn, session.org_id, recipient, scope_ref)
        key = uuid4()
        conn.execute(
            """INSERT INTO aw_route_grants(org_id,grant_id,issuer,src,dst,src_revision,dst_revision,
            issuer_revision,allowed_kind,scope_ref,expires_at,max_uses) VALUES (%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,clock_timestamp()+%s * interval '1 second',%s)""",
            (
                session.org_id,
                key,
                session.employee_id,
                sender,
                recipient,
                src["employment_revision"],
                dst["employment_revision"],
                session.employment_revision,
                kind,
                scope_ref,
                ttl_seconds,
                max_uses,
            ),
        )
        receipt.update(grant_id=str(key), revocation_epoch=1)
        return receipt


def revoke_route(conn, session, operation_id, grant_id):
    lock_org(conn, session.org_id, write=True)
    if require_employee(conn, session)["role_id"] != "workshop_manager":
        raise Denied("ROUTE_DENIED")
    with operation(conn, session, "REVOKE_ROUTE", operation_id, grant_id) as (old, receipt):
        if not old:
            row = conn.execute(
                """UPDATE aw_route_grants SET revoked=true,
                revocation_epoch=revocation_epoch+CASE WHEN revoked THEN 0 ELSE 1 END
                WHERE org_id=%s AND grant_id=%s RETURNING revocation_epoch""",
                (session.org_id, _uuid(grant_id)),
            ).fetchone()
            if not row:
                raise Denied("ROUTE_DENIED")
            receipt.update(grant_id=grant_id, **row)
        return receipt


def register_body(conn, session, operation_id, recipient, scope_ref, content):
    sender = require_employee(conn, session)
    if sender["scope_ref"] != scope_ref:
        raise Denied("ROUTE_DENIED")
    with operation(
        conn, session, "REGISTER_BODY", operation_id, [recipient, scope_ref, content]
    ) as (old, receipt):
        if old:
            # Only the same principal, tenure and request hash can replay this opaque receipt.
            return receipt
        target = _peer(conn, session.org_id, recipient, scope_ref)
        direct = conn.execute(
            """SELECT 1 FROM aw_current_message_edges
               WHERE org_id=%s AND src=%s AND dst=%s AND scope_ref=%s AND kind<>'INCIDENT' LIMIT 1""",
            (session.org_id, session.employee_id, recipient, scope_ref),
        ).fetchone()
        if not direct and not _grant(
            conn, session.org_id, session.employee_id, recipient, scope_ref,
            session.employment_revision, target["employment_revision"], remaining=True,
        ):
            raise Denied("ROUTE_DENIED")
        if not 0 < len(content.encode()) <= 16384:
            raise Denied("INVALID_BODY")
        key = uuid4()
        conn.execute(
            """INSERT INTO aw_message_bodies VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (session.org_id, key, session.employee_id, recipient, session.employment_revision,
             target["employment_revision"], scope_ref, content),
        )
        receipt.update(body_ref=str(key))
        return receipt


def _grant(conn, org, sender, recipient, scope, src_rev, dst_rev, *, grant_id=None, kind=None,
           remaining=False):
    conditions = []
    params = [org, sender, recipient, scope, src_rev, dst_rev]
    if grant_id is not None:
        conditions.extend(("g.grant_id=%s", "g.allowed_kind=%s"))
        params.extend((_uuid(grant_id), kind))
    if remaining:
        conditions.append("g.consumed_uses < g.max_uses")
    extra = "".join(" AND " + condition for condition in conditions)
    return conn.execute(
        """SELECT g.grant_id,g.revocation_epoch FROM aw_route_grants g
           JOIN aw_employees i ON (i.org_id,i.employee_id)=(g.org_id,g.issuer)
           WHERE g.org_id=%s AND g.src=%s AND g.dst=%s AND g.scope_ref=%s
           AND g.src_revision=%s AND g.dst_revision=%s AND NOT g.revoked AND g.allowed_kind<>'INCIDENT'
           AND g.expires_at>clock_timestamp() AND i.status='ACTIVE' AND i.employment_revision=g.issuer_revision
           AND i.role_id='workshop_manager' AND i.scope_ref=g.scope_ref"""
        + extra + " ORDER BY g.grant_id LIMIT 1 FOR UPDATE OF g", params,
    ).fetchone()


def _route(conn, org, rev, sender, recipient, kind, scope, grant, src_rev, dst_rev, *, remaining=False):
    if grant:
        row = _grant(conn, org, sender, recipient, scope, src_rev, dst_rev,
                     grant_id=grant, kind=kind, remaining=remaining)
        if not row:
            raise Denied("ROUTE_DENIED")
        return row["revocation_epoch"]
    if kind == "INCIDENT":
        if employee(conn, org, recipient)["role_id"] != "maintenance":
            raise Denied("ROUTE_DENIED")
        return None
    if not conn.execute(
        """SELECT 1 FROM aw_current_message_edges WHERE
        (org_id,graph_revision,src,dst,kind,scope_ref)=(%s,%s,%s,%s,%s,%s)""",
        (org, rev, sender, recipient, kind, scope),
    ).fetchone():
        raise Denied("ROUTE_DENIED")
    return None


def _live(conn, org, rev, m):
    origin = conn.execute(
        """SELECT 1 FROM aw_message_origins o JOIN aw_bindings b USING(token_digest)
        JOIN aw_installations i USING(installation_id)
        JOIN aw_organizations g ON g.org_id=o.org_id AND g.owner_installation_id=b.installation_id
        WHERE o.org_id=%s AND o.message_id=%s AND b.org_member
        AND b.org_id=o.org_id AND b.employee_id=%s AND b.employment_revision=%s
        AND b.grant_epoch=o.grant_epoch AND b.expires_at>clock_timestamp() AND NOT b.revoked AND i.enabled
        FOR SHARE OF b,i""",
        (org, m["message_id"], m["sender"], m["sender_revision"]),
    ).fetchone()
    if not origin:
        raise Denied("SENDER_BINDING_UNVERIFIED")
    if not m["live"]:
        raise Denied("DELIVERY_EXPIRED")
    src = _peer(conn, org, m["sender"], m["scope_ref"])
    dst = _peer(conn, org, m["recipient"], m["scope_ref"])
    if (src["employment_revision"], dst["employment_revision"]) != (
        m["sender_revision"],
        m["recipient_revision"],
    ):
        raise Denied("ROUTE_REVOKED")
    epoch = _route(
        conn,
        org,
        rev,
        m["sender"],
        m["recipient"],
        m["kind"],
        m["scope_ref"],
        m["route_grant_id"],
        m["sender_revision"],
        m["recipient_revision"],
    )
    if epoch != m["grant_epoch"]:
        raise Denied("ROUTE_REVOKED")


def _message(conn, session, mid):
    return conn.execute(
        """SELECT *,expires_at>clock_timestamp() AS live FROM aw_messages
        WHERE org_id=%s AND message_id=%s AND recipient=%s AND recipient_revision=%s""",
        (session.org_id, _uuid(mid), session.employee_id, session.employment_revision),
    ).fetchone()


def send(
    conn,
    session,
    operation_id,
    recipient,
    kind,
    scope_ref,
    graph_revision,
    ttl_seconds,
    body_ref=None,
    route_grant_id=None,
    causation_id=None,
    incident_code=None,
):
    org = lock_org(conn, session.org_id)
    src = require_employee(conn, session)
    origin = conn.execute(
        """SELECT token_digest,b.grant_epoch FROM aw_bindings b JOIN aw_installations i USING(installation_id)
        JOIN aw_organizations g ON g.org_id=b.org_id AND g.owner_installation_id=b.installation_id
        WHERE b.org_member AND b.token_digest=%s AND b.grant_epoch=%s AND b.employee_id=%s AND b.org_id=%s
        AND b.installation_id=%s AND b.employment_revision=%s AND b.expires_at>clock_timestamp() AND NOT revoked AND enabled
        FOR SHARE OF b,i""",
        (
            session.binding_digest,
            session.grant_epoch,
            session.employee_id,
            session.org_id,
            session.installation_id,
            session.employment_revision,
        ),
    ).fetchone()
    if not origin:
        raise Denied("SENDER_BINDING_UNVERIFIED")
    fields = [
        recipient,
        kind,
        scope_ref,
        graph_revision,
        ttl_seconds,
        body_ref,
        route_grant_id,
        causation_id,
        incident_code,
    ]
    with operation(conn, session, "SEND", operation_id, fields) as (old, receipt):
        if old:
            return receipt
        if graph_revision != org["active_revision"]:
            raise Denied("STALE_ORG_REVISION")
        if src["scope_ref"] != scope_ref or kind not in KINDS or not 0 < ttl_seconds <= 86400:
            raise Denied("ROUTE_DENIED")
        dst = _peer(conn, session.org_id, recipient, scope_ref)
        # Authorize the address and kind before inspecting private bodies or predecessor messages.
        epoch = _route(conn, session.org_id, graph_revision, session.employee_id, recipient, kind,
                       scope_ref, route_grant_id, session.employment_revision, dst["employment_revision"],
                       remaining=True)
        if kind == "INCIDENT":
            if (
                incident_code not in INCIDENT_CODES
                or body_ref
                or causation_id
                or route_grant_id
                or ttl_seconds > 300
            ):
                raise Denied("INVALID_INCIDENT")
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,1))",
                (fingerprint([session.org_id, session.employee_id, "INCIDENT"]),),
            )
            count = conn.execute(
                """SELECT count(*) AS n FROM aw_messages WHERE org_id=%s AND sender=%s
                AND kind='INCIDENT' AND created_at>clock_timestamp()-interval '1 minute'""",
                (session.org_id, session.employee_id),
            ).fetchone()["n"]
            if count >= 3:
                raise Denied("INCIDENT_RATE_LIMIT")
        elif incident_code:
            raise Denied("INVALID_INCIDENT")
        mid, hops = uuid4(), 0
        root = mid
        if causation_id:
            parent = _message(conn, session, causation_id)
            if not parent or parent["scope_ref"] != scope_ref:
                raise Denied("CAUSATION_DENIED")
            _live(conn, session.org_id, graph_revision, parent)
            root, hops = parent["root_id"], parent["hops"] + 1
            if hops > MAX_HOPS:
                raise Denied("ROUTE_HOP_LIMIT")
        if (
            body_ref
            and not conn.execute(
                """SELECT 1 FROM aw_message_bodies WHERE
            (org_id,body_ref,src,dst,src_revision,dst_revision,scope_ref)=(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    session.org_id,
                    _uuid(body_ref),
                    session.employee_id,
                    recipient,
                    session.employment_revision,
                    dst["employment_revision"],
                    scope_ref,
                ),
            ).fetchone()
        ):
            raise Denied("BODY_DENIED")
        if route_grant_id and not conn.execute(
            """UPDATE aw_route_grants SET consumed_uses=consumed_uses+1
               WHERE org_id=%s AND grant_id=%s AND NOT revoked AND expires_at>clock_timestamp()
               AND consumed_uses < max_uses RETURNING grant_id""",
            (session.org_id, _uuid(route_grant_id)),
        ).fetchone():
            raise Denied("ROUTE_DENIED")
        conn.execute(
            """INSERT INTO aw_messages
            (org_id,message_id,operation_id,sender,sender_revision,recipient,recipient_revision,graph_revision,
             kind,scope_ref,body_ref,route_grant_id,grant_epoch,causation_id,root_id,hops,incident_code,expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    clock_timestamp()+%s * interval '1 second')""",
            (
                session.org_id,
                mid,
                operation_id,
                session.employee_id,
                session.employment_revision,
                recipient,
                dst["employment_revision"],
                graph_revision,
                kind,
                scope_ref,
                body_ref,
                route_grant_id,
                epoch,
                causation_id,
                root,
                hops,
                incident_code,
                ttl_seconds,
            ),
        )
        conn.execute(
            "INSERT INTO aw_message_origins VALUES (%s,%s,%s,%s)",
            (session.org_id, mid, origin["token_digest"], origin["grant_epoch"]),
        )
        seq = conn.execute(
            """INSERT INTO aw_mailboxes VALUES (%s,%s,%s,1)
            ON CONFLICT (org_id,recipient,employment_revision) DO UPDATE SET seq=aw_mailboxes.seq+1 RETURNING seq""",
            (session.org_id, recipient, dst["employment_revision"]),
        ).fetchone()["seq"]
        conn.execute(
            "INSERT INTO aw_deliveries VALUES (%s,%s,%s,%s,%s,NULL)",
            (session.org_id, mid, recipient, dst["employment_revision"], seq),
        )
        conn.execute(
            "INSERT INTO aw_outbox(event_id,org_id,message_id) VALUES (%s,%s,%s)",
            (uuid4(), session.org_id, mid),
        )
        receipt.update(message_id=str(mid), status="QUEUED", sequence=seq)
        return receipt


def _delivered(conn, session, mid):
    org = lock_org(conn, session.org_id)
    require_employee(conn, session)
    message = _message(conn, session, mid)
    if not message:
        raise Denied("DELIVERY_UNAVAILABLE")
    _live(conn, session.org_id, org["active_revision"], message)
    return message


def body_get(conn, session, message_id):
    m = _delivered(conn, session, message_id)
    row = conn.execute(
        """SELECT content FROM aw_message_bodies WHERE org_id=%s AND body_ref=%s
        AND dst=%s AND dst_revision=%s AND scope_ref=%s""",
        (
            session.org_id,
            m["body_ref"],
            session.employee_id,
            session.employment_revision,
            m["scope_ref"],
        ),
    ).fetchone()
    if not row:
        raise Denied("BODY_DENIED")
    return row


def ack(conn, session, message_id):
    _delivered(conn, session, message_id)
    row = conn.execute(
        """UPDATE aw_deliveries SET acknowledged_at=coalesce(acknowledged_at,clock_timestamp())
        WHERE org_id=%s AND message_id=%s RETURNING seq,acknowledged_at""",
        (session.org_id, _uuid(message_id)),
    ).fetchone()
    return {
        "status": "ACKNOWLEDGED",
        "message_id": message_id,
        "sequence": row["seq"],
        "acknowledged_at": row["acknowledged_at"].isoformat(),
    }


def mailbox(conn, session, after=0, limit=50):
    org = lock_org(conn, session.org_id)
    require_employee(conn, session)
    rows = conn.execute(
        """SELECT m.*,d.seq,d.acknowledged_at,m.expires_at>clock_timestamp() AS live
        FROM aw_deliveries d JOIN aw_messages m USING (org_id,message_id)
        WHERE d.org_id=%s AND d.recipient=%s AND d.recipient_revision=%s AND d.seq>%s ORDER BY d.seq LIMIT %s""",
        (
            session.org_id,
            session.employee_id,
            session.employment_revision,
            after,
            min(max(limit, 1), 100),
        ),
    ).fetchall()
    items = []
    for m in rows:
        try:
            _live(conn, session.org_id, org["active_revision"], m)
        except Denied:
            items.append({"sequence": m["seq"], "status": "UNAVAILABLE"})
        else:
            items.append(
                {
                    "sequence": m["seq"],
                    "message_id": str(m["message_id"]),
                    "sender": m["sender"],
                    "kind": m["kind"],
                    "scope_ref": m["scope_ref"],
                    "has_body": bool(m["body_ref"]),
                    "incident_code": m["incident_code"],
                    "status": "ACKNOWLEDGED" if m["acknowledged_at"] else "QUEUED",
                }
            )
    return {"items": items, "next_cursor": rows[-1]["seq"] if rows else after}


def accept_notification(conn, subscriber, event_id):
    """Trusted relay hint. An unknown notification cannot add a delivery or execute work."""
    event = _uuid(event_id)
    row = conn.execute(
        """SELECT m.*,g.active_revision,m.expires_at>clock_timestamp() AS live
           FROM aw_outbox e JOIN aw_messages m USING (org_id,message_id)
           JOIN aw_organizations g USING (org_id) WHERE e.event_id=%s FOR SHARE OF g""",
        (event,),
    ).fetchone()
    if not row:
        return False
    try:
        _live(conn, row["org_id"], row["active_revision"], row)
    except Denied:
        return False
    return bool(
        conn.execute(
            """INSERT INTO aw_inbox(subscriber,event_id) VALUES (%s,%s)
               ON CONFLICT DO NOTHING RETURNING event_id""",
            (subscriber, event),
        ).fetchone()
    )


TOOLS = {
    "aw_message_send": (
        "Send one addressed message over a permitted direction and message kind.",
        schema(
            COMMON
            | {
                "kind": KIND,
                "graph_revision": POSITIVE,
                "ttl_seconds": TTL,
                "body_ref": ID,
                "route_grant_id": ID,
                "causation_id": ID,
                "incident_code": {"enum": list(INCIDENT_CODES)},
            },
            [*COMMON, "kind", "graph_revision", "ttl_seconds"],
        ),
        send,
    ),
    "aw_delivery_ack": ("Acknowledge an addressed message.", schema({"message_id": ID}), ack),
    "aw_mailbox_get": (
        "Resume the authenticated employee's mailbox after a sequence.",
        schema(
            {
                "after": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            [],
        ),
        mailbox,
    ),
    "aw_message_body_put": (
        "Register a private body addressed to a recipient and current tenure.",
        schema(COMMON | {"content": {"type": "string", "minLength": 1, "maxLength": 16384}}),
        register_body,
    ),
    "aw_message_body_get": (
        "Read an addressed body under current route authorization.",
        schema({"message_id": ID}),
        body_get,
    ),
    "aw_route_grant": (
        "Issue a limited non-transferrable message route.",
        schema(
            COMMON
            | {
                "sender": IDENTIFIER,
                "kind": KIND,
                "ttl_seconds": TTL,
                "max_uses": {"type": "integer", "minimum": 1, "maximum": 100},
                "expected_revision": POSITIVE,
            }
        ),
        issue_route,
    ),
    "aw_route_revoke": (
        "Revoke a temporary route for subsequent delivery and body reads.",
        schema({"operation_id": IDENTIFIER, "grant_id": ID}),
        revoke_route,
    ),
}
