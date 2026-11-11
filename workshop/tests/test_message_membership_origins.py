"""Exercise current W1 membership over persisted W2 mail and constrained route grants."""
import os
from uuid import uuid4

import pytest

from agent_workshop import messaging as msg
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.organization import bootstrap
from agent_workshop.server import dispatch


@pytest.fixture
def addresses():
    url = os.environ["AW_DATABASE_URL"]
    org, owner, other = uuid4().hex, uuid4().hex, uuid4().hex
    with connect(url) as conn:
        for installation in (owner, other):
            conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, "nf-fixture"))
        bootstrap(conn, org, "Standard", installation_id=owner)
        tokens = {
            name: enroll(conn, owner, org, name, 1, 1)
            for name in ("manager", "volume", "script", "actor-1")
        }
    return url, org, other, tokens


@pytest.mark.parametrize("fence", ["capability_only", "foreign_installation", "expired"])
def test_stored_origin_must_still_belong_to_the_active_installation(addresses, fence):
    url, org, other, tokens = addresses
    with connect(url) as conn:
        director = authenticate(conn, tokens["volume"])
        ref = msg.register_body(conn, director, "body", "actor-1", "organization", "private")["body_ref"]
        args = ("send", "actor-1", "ASSIGN", "organization", 1, 120)
        receipt = msg.send(conn, director, *args, body_ref=ref)
        event = conn.execute("SELECT event_id FROM aw_outbox WHERE org_id=%s", (org,)).fetchone()["event_id"]
        assert msg.accept_notification(conn, "before-change", event)
    with connect(url) as conn:
        receiver = authenticate(conn, tokens["actor-1"])
        if fence == "capability_only":
            conn.execute("UPDATE aw_bindings SET org_member=false WHERE token_digest=%s", (director.binding_digest,))
        elif fence == "foreign_installation":
            conn.execute("UPDATE aw_bindings SET installation_id=%s WHERE token_digest=%s",
                         (other, director.binding_digest))
        else:
            conn.execute("UPDATE aw_bindings SET expires_at=clock_timestamp() WHERE token_digest=%s",
                         (director.binding_digest,))
        # A stale in-memory Session cannot make an old credential usable for an exact retry.
        with pytest.raises(Denied, match="SENDER_BINDING_UNVERIFIED"):
            msg.send(conn, director, *args, body_ref=ref)
        assert msg.mailbox(conn, receiver)["items"] == [{"sequence": 1, "status": "UNAVAILABLE"}]
        for handler in (msg.body_get, msg.ack):
            with pytest.raises(Denied, match="SENDER_BINDING_UNVERIFIED"):
                handler(conn, receiver, receipt["message_id"])
        assert not msg.accept_notification(conn, "after-change", event)
        assert conn.execute("SELECT count(*) AS n FROM aw_inbox WHERE event_id=%s", (event,)).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM aw_deliveries WHERE org_id=%s", (org,)).fetchone()["n"] == 1


def test_revoked_recipient_binding_cannot_read_or_ack(addresses):
    url, _, _, tokens = addresses
    with connect(url) as conn:
        director = authenticate(conn, tokens["volume"])
        actor = authenticate(conn, tokens["actor-1"])
        body = msg.register_body(conn, director, "body", "actor-1", "organization", "private")
        sent = msg.send(conn, director, "send", "actor-1", "ASSIGN", "organization", 1, 120,
                        body_ref=body["body_ref"])
        assert msg.body_get(conn, actor, sent["message_id"]) == {"content": "private"}
        conn.execute("UPDATE aw_bindings SET revoked=true,grant_epoch=grant_epoch+1 WHERE token_digest=%s",
                     (actor.binding_digest,))
    config = {"token": tokens["actor-1"], "database_url": url}
    for tool, args in (("aw_mailbox_get", {}), ("aw_message_body_get", {"message_id": sent["message_id"]}),
                       ("aw_delivery_ack", {"message_id": sent["message_id"]})):
        with pytest.raises(Denied, match="VIEW_DENIED"):
            dispatch(config, tool, args)


def test_private_attachment_and_causation_failure_do_not_consume_the_final_use(addresses):
    url, org, _, tokens = addresses
    with connect(url) as conn:
        writer = authenticate(conn, tokens["script"])
        manager = authenticate(conn, tokens["manager"])
        grant = msg.issue_route(conn, manager, "temporary", "script", "actor-1", "ASSIGN",
                                "organization", 120, 1, 1)["grant_id"]
        body = msg.register_body(conn, writer, "private", "actor-1", "organization", "private")
        for name, args, code in (("body", {"body_ref": str(uuid4())}, "BODY_DENIED"),
                                 ("parent", {"causation_id": str(uuid4())}, "CAUSATION_DENIED")):
            with pytest.raises(Denied, match=code):
                msg.send(conn, writer, name, "actor-1", "ASSIGN", "organization", 1, 120,
                         route_grant_id=grant, **args)
        def consumed():
            return conn.execute("SELECT consumed_uses FROM aw_route_grants WHERE org_id=%s AND grant_id=%s",
                                (org, grant)).fetchone()["consumed_uses"]
        assert consumed() == 0
        request = dict(operation_id="sent", recipient="actor-1", kind="ASSIGN", scope_ref="organization",
                       graph_revision=1, ttl_seconds=120, body_ref=body["body_ref"], route_grant_id=grant)
        receipt = msg.send(conn, writer, **request)
        assert msg.send(conn, writer, **request) == receipt and consumed() == 1
        assert msg.register_body(conn, writer, "private", "actor-1", "organization", "private") == body
        with pytest.raises(Denied, match="ROUTE_DENIED"):
            msg.register_body(conn, writer, "another", "actor-1", "organization", "private")
        with pytest.raises(Denied, match="ROUTE_DENIED"):
            msg.send(conn, writer, **(request | {"operation_id": "new", "body_ref": str(uuid4())}))
        assert consumed() == 1
