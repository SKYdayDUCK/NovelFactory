import pytest

from agent_workshop import messaging
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.delegation import handoff
from agent_workshop.manager import history, progress
from agent_workshop.organization import TOOLS
from test_delegation import make, setup_slot
from test_message_membership_origins import addresses


def test_historical_delivery_keeps_verified_origin_after_sender_expiry(addresses):
    dsn, org, other, tokens = addresses
    with connect(dsn) as conn:
        author = authenticate(conn, tokens["volume"])
        recipient = authenticate(conn, tokens["actor-1"])
        message = messaging.send(conn, author, "assignment", "actor-1", "ASSIGN",
                                 "organization", 1, 60)["message_id"]

        def deliveries():
            return {e["ref_id"] for e in history(conn, recipient, limit=200)["events"]
                    if e["source"] == "aw_deliveries"}

        assert deliveries() == {message}
        conn.execute("UPDATE aw_bindings SET expires_at=now() WHERE token_digest=%s",
                     (author.binding_digest,))
        assert deliveries() == {message}
        # A split historical directory no longer proves this operation belonged to the
        # installation whose employee is reading the restored projection.
        conn.execute("UPDATE aw_bindings SET installation_id=%s WHERE token_digest=%s",
                     (other, author.binding_digest))
        assert deliveries() == set()

def test_handoff_retains_own_past_events_and_checks_the_current_tenure():
    dsn, org, install, volume = setup_slot()
    op = make(dsn, volume)
    with connect(dsn) as conn:
        def member(name, revision=1):
            return authenticate(conn, enroll(conn, install, org, name, revision, 1))

        def owners(session):
            return [e["data"]["owner_id"] for e in history(conn, session, limit=200)["events"]
                    if e["source"] == "aw_delegations" and e["ref_id"] == op]

        original, successor, manager = member("actor-1"), member("actor-2"), member("manager")
        assert progress(conn, original, op)["owner_id"] == "actor-1"
        cursor = history(conn, original, limit=1)["next_cursor"]
        assert cursor
        handoff(conn, authenticate(conn, volume), op, "handoff", "actor-2")
        assert owners(original) == ["actor-1"] and owners(successor) == ["actor-2"]
        assert progress(conn, successor, op)["owner_id"] == "actor-2"
        with pytest.raises(Denied, match="VIEW_DENIED"):
            progress(conn, original, op)

        TOOLS["aw_transfer"][2](conn, manager, employee_id="actor-1", expected_revision=1,
                                operation_id="move", role_id="character_actor",
                                supervisor_id="volume", team_id="relief")
        with pytest.raises(Denied, match="STALE_EMPLOYMENT"):
            history(conn, original, cursor=cursor)
        new_tenure = member("actor-1", 2)
        assert owners(new_tenure) == []
        with pytest.raises(Denied, match="INVALID_CURSOR"):
            history(conn, new_tenure, cursor=cursor)
        assert set(owners(manager)) == {"actor-1", "actor-2"}
