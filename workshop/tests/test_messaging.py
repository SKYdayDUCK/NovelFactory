import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from agent_workshop import messaging as msg
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.organization import TOOLS, bootstrap
from jsonschema import Draft202012Validator


@pytest.fixture
def team():
    url = os.environ["AW_DATABASE_URL"]
    org = uuid4().hex
    install = uuid4().hex
    with connect(url) as conn:
        conn.execute("INSERT INTO aw_installations VALUES(%s,%s,true)", (install, "nf-test"))
        bootstrap(conn, org, "Research-Audit", installation_id=install)
        names = conn.execute(
            "SELECT employee_id FROM aw_employees WHERE org_id=%s", (org,)
        ).fetchall()
        ids = {
            r["employee_id"]: authenticate(conn, enroll(conn, install, org, r["employee_id"], 1, 1))
            for r in names
        }

    assert all(member.org_member and member.installation_id == install for member in ids.values())

    def identity(name):
        return ids[name]

    return url, identity


def send(conn, identity, source, target, kind, op, **kwargs):
    return msg.send(conn, identity(source), op, target, kind, "organization", 1, 60, **kwargs)


def grant(conn, identity):
    return msg.issue_route(
        conn, identity("manager"), "g", "script", "actor-1", "ASSIGN", "organization", 60, 1, 1
    )["grant_id"]


def test_directed_routes_causation_body_and_durable_cursor(team):
    url, ident = team
    with connect(url) as conn:
        assert not Draft202012Validator(msg.TOOLS["aw_message_send"][1]).is_valid(
            {
                "operation_id": "1",
                "recipient": "actor-1",
                "scope_ref": "organization",
                "kind": "ASSIGN",
                "graph_revision": 1,
                "ttl_seconds": 60,
                "sender": "volume",
            }
        )
        with pytest.raises(Denied, match="ROUTE_DENIED"):
            send(conn, ident, "script", "actor-1", "ASSIGN", "x")
        body = msg.register_body(
            conn, ident("script"), "material", "chief", "organization", "bounded evidence"
        )["body_ref"]
        m = send(conn, ident, "script", "chief", "PROPOSE_SCRIPT", "m", body_ref=body)
        assert send(conn, ident, "script", "chief", "PROPOSE_SCRIPT", "m", body_ref=body) == m
        with pytest.raises(Denied, match="IDEMPOTENCY_MISMATCH"):
            send(conn, ident, "script", "chief", "PROPOSE_SCRIPT", "m")
        assert msg.body_get(conn, ident("chief"), m["message_id"]) == {
            "content": "bounded evidence"
        }
        with pytest.raises(Denied, match="DELIVERY_UNAVAILABLE"):
            msg.body_get(conn, ident("actor-1"), m["message_id"])
        with pytest.raises(Denied, match="BODY_DENIED"):
            send(conn, ident, "chief", "volume", "ASSIGN", "forward", body_ref=body)
        n = send(conn, ident, "chief", "volume", "ASSIGN", "forward", causation_id=m["message_id"])
        l = send(conn, ident, "volume", "actor-1", "ASSIGN", "actor", causation_id=n["message_id"])
        mid = l["message_id"]
        assert msg.mailbox(conn, ident("actor-1"))["items"][0]["message_id"] == mid
    with connect(url) as conn:
        assert msg.mailbox(conn, ident("actor-1"), after=1)["items"] == []
        assert msg.ack(conn, ident("actor-1"), mid) == msg.ack(conn, ident("actor-1"), mid)
        events = conn.execute(
            "SELECT event_id FROM aw_outbox WHERE org_id=%s", (ident("chief").org_id,)
        ).fetchall()
        assert len(events) == 3
        for event in reversed(events):
            assert msg.accept_notification(conn, "test", event["event_id"])
            assert not msg.accept_notification(conn, "test", event["event_id"])
        assert not msg.accept_notification(conn, "test", uuid4())


def test_grant_revocation_fences_inflight_body_and_ack(team):
    url, ident = team
    with connect(url) as conn:
        g = grant(conn, ident)
        body = msg.register_body(
            conn, ident("script"), "body", "actor-1", "organization", "private material"
        )["body_ref"]
        m = send(conn, ident, "script", "actor-1", "ASSIGN", "a", body_ref=body, route_grant_id=g)
        assert (
            msg.body_get(conn, ident("actor-1"), m["message_id"])["content"] == "private material"
        )
        with pytest.raises(Denied, match="ROUTE_DENIED"):
            send(conn, ident, "script", "actor-1", "ASSIGN", "b", route_grant_id=g)
        assert (
            send(conn, ident, "script", "actor-1", "ASSIGN", "a", body_ref=body, route_grant_id=g)
            == m
        )
        msg.revoke_route(conn, ident("manager"), "revoke", g)
        assert msg.mailbox(conn, ident("actor-1"))["items"] == [
            {"sequence": 1, "status": "UNAVAILABLE"}
        ]
        for tool in (msg.body_get, msg.ack):
            with pytest.raises(Denied, match="ROUTE_DENIED"):
                tool(conn, ident("actor-1"), m["message_id"])
        with pytest.raises(Denied, match="ROUTE_DENIED"):
            msg.issue_route(
                conn,
                ident("script"),
                "transmit",
                "script",
                "actor-2",
                "ASSIGN",
                "organization",
                10,
                1,
                1,
            )


def test_departure_expiry_and_isolation(team):
    url, ident = team
    with connect(url) as conn:
        m = send(conn, ident, "volume", "actor-1", "ASSIGN", "1")
        conn.execute(
            "UPDATE aw_messages SET expires_at=now()-interval '1 second' WHERE message_id=%s",
            (m["message_id"],),
        )
        with pytest.raises(Denied, match="DELIVERY_EXPIRED"):
            msg.ack(conn, ident("actor-1"), m["message_id"])
        send(conn, ident, "volume", "actor-1", "ASSIGN", "2")
        TOOLS["aw_suspend"][2](
            conn, ident("manager"), operation_id="s", expected_revision=1, employee_id="actor-1"
        )
        with pytest.raises(Denied, match="STALE_EMPLOYMENT"):
            msg.mailbox(conn, ident("actor-1"))


def test_concurrent_last_grant_use(team):
    url, ident = team
    with connect(url) as conn:
        g = grant(conn, ident)
    gate = Barrier(2)

    def worker(n):
        with connect(url) as conn:
            gate.wait(5)
            try:
                return send(conn, ident, "script", "actor-1", "ASSIGN", str(n), route_grant_id=g)[
                    "status"
                ]
            except Denied as exc:
                return str(exc)

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(worker, [1, 2])) == ["QUEUED", "ROUTE_DENIED"]


def test_incident_whitelist_and_rate_limit(team):
    url, ident = team
    with connect(url) as conn:
        with pytest.raises(Denied, match="INVALID_INCIDENT"):
            send(conn, ident, "actor-1", "ops", "INCIDENT", "bad", incident_code="character secret")
        for n in range(3):
            send(conn, ident, "actor-1", "ops", "INCIDENT", str(n), incident_code="SERVICE_OFFLINE")
        with pytest.raises(Denied, match="INCIDENT_RATE_LIMIT"):
            send(conn, ident, "actor-1", "ops", "INCIDENT", "next", incident_code="SERVICE_OFFLINE")


def test_default_edges_enforce_each_direction_and_message_type(team):
    url, ident = team
    # These message kinds come from the W2 communication contract. Management edges also
    # allow employees to request review and escalate an exception to their direct supervisor.
    routes = [
        ("script", "chief", ("PROPOSE_SCRIPT",), "REQUEST_REVISION"),
        ("chief", "script", ("REQUEST_REVISION", "REPORT"), "PROPOSE_SCRIPT"),
        ("chief", "volume", ("ASSIGN", "PAUSE", "REQUEST_REVISION"), "REPORT"),
        ("volume", "chief", ("REPORT", "ASK", "REQUEST_DECISION", "EXCEPTION", "REQUEST_REVIEW"), "ASSIGN"),
        ("volume", "actor-1", ("ASSIGN", "PLAN_NOTICE"), "ASK"),
        ("actor-1", "volume", ("REPORT", "ASK", "REQUEST_RESOURCE", "EXCEPTION", "REQUEST_REVIEW"), "PLAN_NOTICE"),
        ("review-lead", "reviewer", ("ASSIGN_REVIEW",), "REPORT"),
        ("reviewer", "review-lead", ("REPORT", "EXCEPTION", "REQUEST_REVIEW"), "ASK"),
    ]
    with connect(url) as conn:
        # An organization created with the signed W1 preset retains its original frozen edges.
        org = ident("reviewer").org_id
        assert conn.execute(
            """SELECT 1 FROM aw_edges WHERE org_id=%s AND graph_revision=1
               AND src='reviewer' AND dst='review-lead' AND kind='ASK'""",
            (org,),
        ).fetchone()
        for source, target, allowed, denied in routes:
            as_sender = TOOLS["aw_roster_get"][2](conn, ident(source))
            advertised = {x["kind"] for x in as_sender["routes"] if x["src"] == source and x["dst"] == target}
            assert set(allowed) <= advertised and denied not in advertised
            for kind in allowed:
                args = (conn, ident, source, target, kind, source + "-" + target + "-" + kind)
                assert send(*args)["status"] == "QUEUED"
            with pytest.raises(Denied, match="^ROUTE_DENIED$"):
                send(conn, ident, source, target, denied, source + "-" + target + "-denied")
