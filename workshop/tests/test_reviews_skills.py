import os
from hashlib import sha256
from uuid import uuid4

import pytest
from agent_workshop import reviews_skills as rs
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.review_admin import review_slot, subject


@pytest.fixture
def team():
    org, install = uuid4().hex, uuid4().hex
    dsn = os.environ["AW_DATABASE_URL"]
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (install, str(uuid4())))
        bootstrap(conn, org, "Research-Audit", installation_id=install)
        for person, group in (
            ("writer", "producer"),
            ("stats", "producer"),
            ("reviewer", "independent"),
        ):
            subject(conn, org, person, group)
        rs.register_artifact(
            conn,
            org,
            "text",
            "v1",
            sha256(b"fixture").hexdigest(),
            "organization",
            "receipt-A",
            ["writer"],
            ["source-A"],
            {
                "WORLD": {"opening": 1, "consumed": 1, "remaining": 1},
                "STABILITY": {"requests": 1, "completed": 1, "duplicate_effects": 0},
            },
            routine_eligible=True,
        )

        members = conn.execute("SELECT employee_id FROM aw_employees WHERE org_id=%s", (org,)).fetchall()
        sessions = {m["employee_id"]: authenticate(conn, enroll(conn, install, org, m["employee_id"], 1, 1)) for m in members}

    def identity(name):
        return sessions[name]

    return dsn, identity


def request(conn, identity, mode="PROSE", op="r", **kw):
    return rs.review_request(
        conn, identity("review-lead"), op, "text", "v1", mode, "SAMPLED", 60, **kw
    )


@pytest.mark.parametrize("mode", rs.MODES)
def test_modes_and_deterministic_first(team, mode):
    dsn, idn = team
    with connect(dsn) as c:
        r = request(c, idn, mode)
        if mode in ("WORLD", "STABILITY"):
            assert r["state"] == ("RULE_BLOCKED" if mode == "WORLD" else "RULE_PASSED")
        else:
            assert r["state"] == "PENDING"
            assert (
                c.execute(
                    "SELECT reviewer_id FROM aw_reviews WHERE review_id=%s", (r["review_id"],)
                ).fetchone()["reviewer_id"]
                == "reviewer"
            )
        assert (
            c.execute(
                "SELECT count(*) AS n FROM aw_budget_holds WHERE org_id=%s",
                (idn("manager").org_id,),
            ).fetchone()["n"]
            == 0
        )


def test_independence_freeze_evidence_and_bounded_rounds(team):
    dsn, idn = team
    org = idn("manager").org_id
    with connect(dsn) as c:
        with pytest.raises(Denied, match="FROZEN_SUBJECT"):
            subject(c, org, "stats", "new-independent")
        with pytest.raises(Denied, match="INDEPENDENT_REVIEW_REQUIRED"):
            request(c, idn, reviewer_id="stats")
        r = request(c, idn)
        assert request(c, idn) == r
        with pytest.raises(Denied, match="REVIEW_ALREADY"):
            request(c, idn, op="second")
        with pytest.raises(Denied, match="EVIDENCE_SCOPE_DENIED"):
            rs.decide(c, idn("reviewer"), "x", r["review_id"], "APPROVED", "VOICE", "alien-secret")
        for n in range(2):
            if n:
                r = request(c, idn, op="round2")
            d = rs.decide(
                c,
                idn("reviewer"),
                f"d{n}",
                r["review_id"],
                "CHANGES_REQUESTED",
                "VOICE",
                "source-A",
            )
            assert d["state"] == ("ESCALATED" if n else "CHANGES_REQUESTED")
        with pytest.raises(Denied, match="REVIEW_ALREADY"):
            request(c, idn, op="round3")


@pytest.mark.parametrize(
    "policy,target,allowed,denied",
    [
        ("PUBLIC_SHARED", None, "actor-1", None),
        ("ROLE_SCOPED", "reviewer", "reviewer", "actor-1"),
        ("TEAM_SCOPED", "actors", "actor-1", "reviewer"),
        ("PRIVATE", "actor-1", "actor-1", "actor-2"),
        ("CONTROL_ONLY", None, "manager", "actor-1"),
    ],
)
def test_skill_policies_no_capabilities(team, policy, target, allowed, denied):
    dsn, idn = team
    with connect(dsn) as c:
        sha = rs.register_skill(
            c,
            idn("manager").org_id,
            "skill",
            "v1",
            "control" if policy == "CONTROL_ONLY" else "creative",
            "PROSE",
            b"formula",
        )
        b = rs.share(c, idn("manager"), "b", "skill", "v1", policy, 60, target)
        spec = rs.resolve(c, idn(allowed), b["binding_id"], "PROSE")
        assert set(spec) == {"skill_id", "source_revision", "content_sha", "mode", "epoch"}
        assert spec["content_sha"] == sha
        if denied:
            with pytest.raises(Denied, match="SKILL_DENIED"):
                rs.resolve(c, idn(denied), b["binding_id"], "PROSE")


def test_skill_old_hash_remains_history_after_revocation(team):
    dsn, idn = team
    with connect(dsn) as c:
        r = request(c, idn)
        for v in ("v1", "v2"):
            digest = rs.register_skill(
                c, idn("manager").org_id, "writing", v, "creative", "PROSE", v.encode()
            )
            binding = rs.share(c, idn("manager"), v, "writing", v, "ROLE_SCOPED", 60, "reviewer")
            if v == "v1":
                old = rs.freeze(c, idn("reviewer"), binding["binding_id"], "PROSE", r["review_id"])
                old_id = binding["binding_id"]
            else:
                with pytest.raises(Denied, match="ALREADY_FROZEN"):
                    rs.freeze(c, idn("reviewer"), binding["binding_id"], "PROSE", r["review_id"])
        assert old["content_sha"] != digest
        rs.revoke(c, idn("manager"), "disable", old_id)
        with pytest.raises(Denied, match="SKILL_DENIED"):
            rs.resolve(c, idn("reviewer"), old_id, "PROSE")
        assert (
            rs.skill_history(c, idn("reviewer"), r["review_id"], "writing")["content_sha"]
            == old["content_sha"]
        )


def test_paid_review_cannot_spend_without_hold_or_ignore_unknown(team):
    dsn, idn = team
    session = idn("review-lead")
    org = session.org_id
    with connect(dsn) as c:
        token = enroll(c, session.installation_id, org, "review-lead", 1, 2)
        lead = authenticate(c, token)
        nf = c.execute(
            "SELECT nf_instance_id FROM aw_installations WHERE installation_id=%s",
            (session.installation_id,),
        ).fetchone()["nf_instance_id"]
        allow_slot(
            c,
            org,
            session.installation_id,
            "slot",
            "reviewer",
            {
                "nf_instance_id": nf,
                "work_id": str(uuid4()),
                "branch_id": str(uuid4()),
                "actor_binding_ref": None,
                "role_id": "reviewer",
                "task_template_id": "prose-review",
                "scope_ref": "text",
                "generation": 0,
                "grant_epoch": 1,
                "max_amount": "1",
                "currency": "USD",
                "token_limit": 100,
            },
        )
        review_slot(c, org, "slot", "text", "v1", "PROSE")
        c.execute("INSERT INTO aw_budget_accounts VALUES (%s,0,'USD')", (org,))
        args = {
            "operation_id": "paid",
            "artifact_ref": "text",
            "artifact_revision": "v1",
            "mode": "PROSE",
            "risk_reason": "MATERIAL_CHANGE",
            "ttl_seconds": 120,
            "reviewer_id": "reviewer",
            "slot_id": "slot",
            "max_amount": "1",
            "token_limit": 100,
        }
        with pytest.raises(Denied, match="BUDGET_EXCEEDED"):
            rs.review_request(c, lead, **args)
        c.execute("UPDATE aw_budget_accounts SET limit_amount=1 WHERE org_id=%s", (org,))
        with pytest.raises(Denied, match="REVIEW_REQUEST_REQUIRED"):
            rs.delegate(c, lead, "direct", "slot", "1", 100)
        r = rs.review_request(c, lead, **args)
        assert rs.review_request(c, lead, **args) == r
        op = r["operation_id"]
        c.execute(
            "INSERT INTO aw_remote_receipts VALUES (%s,%s,%s,%s,0,'RUNNING','UNKNOWN')",
            (org, op, uuid4(), uuid4()),
        )
        with pytest.raises(Denied, match="WAITING_NF"):
            rs.decide(c, idn("reviewer"), "d", r["review_id"], "APPROVED", "VOICE", "source-A")
        assert (
            c.execute("SELECT status FROM aw_budget_holds WHERE operation_id=%s", (op,)).fetchone()[
                "status"
            ]
            == "HELD"
        )
        c.execute(
            "UPDATE aw_remote_receipts SET task_state='ACCEPTED',accounting_state='SETTLED' WHERE operation_id=%s",
            (op,),
        )
        c.execute(
            "UPDATE aw_budget_holds SET status='SETTLED',settled_amount=0.1 WHERE operation_id=%s",
            (op,),
        )
        assert (
            rs.decide(c, idn("reviewer"), "d", r["review_id"], "APPROVED", "VOICE", "source-A")[
                "state"
            ]
            == "APPROVED"
        )


def test_review_get_reassignment_and_offline_expiry(team):
    from agent_workshop.organization import TOOLS

    dsn, idn = team
    with connect(dsn) as c:
        lead = idn("review-lead")
        review = request(c, idn)["review_id"]
        assert rs.get_review(c, idn("reviewer"), review)["evidence_refs"] == ["source-A"]
        assert "evidence_refs" not in rs.get_review(c, idn("manager"), review)
        with pytest.raises(Denied, match="VIEW_DENIED"):
            rs.get_review(c, idn("actor-1"), review)
        with pytest.raises(Denied, match="REVIEW_DENIED"):
            rs.get_review(c, idn("stats"), review)
        TOOLS["aw_hire"][2](
            c,
            idn("manager"),
            expected_revision=1,
            operation_id="hire",
            employee_id="relief",
            role_id="reviewer",
            supervisor_id="review-lead",
            team_id="audit",
        )
        relief = authenticate(c, enroll(c, lead.installation_id, lead.org_id, "relief", 1, 1))
        subject(c, lead.org_id, "relief", "second-independent")
        TOOLS["aw_suspend"][2](
            c, idn("manager"), expected_revision=2, operation_id="s", employee_id="reviewer"
        )
        with pytest.raises(Denied, match="INDEPENDENT_REVIEW"):
            rs.reconcile_review(c, lead, "bad", review, "stats")
        assert rs.reconcile_review(c, lead, "ok", review, "relief")["reviewer_id"] == "relief"
        with pytest.raises(Denied, match="STALE_EMPLOYMENT"):
            rs.get_review(c, idn("reviewer"), review)
        assert rs.get_review(c, relief, review)["artifact_ref"] == "text"
        rs.decide(c, relief, "decide", review, "APPROVED", "VOICE", "source-A")
        assert (
            c.execute(
                "SELECT count(*) AS n FROM aw_review_evidence WHERE review_id=%s", (review,)
            ).fetchone()["n"]
            == 3
        )
        waiting = request(c, idn, "DIRECTION", "deadline")["review_id"]
        c.execute(
            "UPDATE aw_reviews SET deadline=now()-interval '1 second' WHERE review_id=%s",
            (waiting,),
        )
        assert rs.expire_due_reviews(c, lead.org_id) == 1
        assert rs.expire_due_reviews(c, lead.org_id) == 0
        assert rs.reconcile_review(c, lead, "sweep", waiting)["state"] == "ESCALATED"


def test_caller_cannot_downgrade_sensitive_source_to_routine_rule_pass(team):
    dsn, idn = team
    org = idn("manager").org_id
    with connect(dsn) as conn:
        rs.register_artifact(
            conn,
            org,
            "published",
            "v1",
            sha256(b"history").hexdigest(),
            "organization",
            "independent-source",
            ["writer"],
            ["evidence"],
            {"WORLD": {"opening": 1, "consumed": 0, "remaining": 1}},
        )
        result = rs.review_request(
            conn, idn("review-lead"), "low-risk", "published", "v1", "WORLD", "SAMPLED", 60
        )
        assert result["state"] == "PENDING" and result["execution"] == "HUMAN"
        assert rs.get_review(conn, idn("reviewer"), result["review_id"])["evidence_refs"] == [
            "evidence"
        ]
