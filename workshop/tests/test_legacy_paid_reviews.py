"""Upgrade populated W4a from its original application before signing an existing review."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from uuid import uuid4

from alembic import command
import pytest

from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.review_admin import review_slot, subject
from agent_workshop import reviews_skills as reviews

LEGACY = "6f57575419559dc5438b38bc2ab68adaf94bc30e"


def seed_legacy():
    """Executed in a subprocess with the archived W4a application, before membership existed."""
    org, dsn = "legacy-film", os.environ["AW_DATABASE_URL"]
    with connect(dsn) as conn:
        instances = {inst: str(uuid4()) for inst in ("west", "east")}
        for inst, nf_id in instances.items():
            conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (inst, nf_id))
        bootstrap(conn, org, "Standard")
        subject(conn, org, "writer", "writer-id")
        subject(conn, org, "reviewer", "reviewer-id")
        conn.execute("INSERT INTO aw_budget_accounts VALUES (%s,2,'USD')", (org,))
        requests = {}
        for ref, inst in (("west-paid", "west"), ("east-paid", "east"), ("manual", None)):
            reviews.register_artifact(conn, org, ref, "v1", sha256(ref.encode()).hexdigest(), "organization",
                                      ref + "-source", ["writer"], [ref + "-evidence"], {})
            lead = authenticate(conn, enroll(conn, inst or "west", org, "review-lead", 1, 1))
            paid = {}
            if inst:
                allow_slot(conn, org, inst, ref, "reviewer", {
                    "nf_instance_id": instances[inst], "work_id": str(uuid4()), "branch_id": str(uuid4()),
                    "actor_binding_ref": None, "role_id": "reviewer", "task_template_id": "prose-review",
                    "scope_ref": "text", "generation": 0, "grant_epoch": 1, "max_amount": "0.10",
                    "currency": "USD", "token_limit": 100,
                })
                review_slot(conn, org, ref, ref, "v1", "PROSE")
                paid = {"slot_id": ref, "token_limit": 100, "max_amount": "0.10"}
            request = reviews.review_request(conn, lead, ref, ref, "v1", "PROSE", "MATERIAL_CHANGE", 3600,
                                             reviewer_id="reviewer", **paid)
            requests[ref] = request["review_id"]
            if inst:
                # A completed remote receipt in the earlier schema, with its original operation,
                # slot, installation and budget hold created by the genuine legacy service.
                op = request["operation_id"]
                conn.execute("""INSERT INTO aw_remote_receipts(org_id,operation_id,task_id,receipt_ref,generation,
                               task_state,accounting_state) VALUES (%s,%s,%s,%s,1,'ACCEPTED','SETTLED')""",
                             (org, op, uuid4(), uuid4()))
                conn.execute("UPDATE aw_budget_holds SET status='SETTLED',settled_amount=0.02 WHERE operation_id=%s", (op,))
                conn.execute("UPDATE aw_delegations SET state='SETTLED' WHERE operation_id=%s", (op,))
        manager = authenticate(conn, enroll(conn, "west", org, "manager", 1, 1))
        reviews.register_skill(conn, org, "voice", "v1", "creative", "PROSE", b"frozen-voice")
        requests["skill"] = reviews.share(conn, manager, "voice", "voice", "v1", "PUBLIC_SHARED", 3600)["binding_id"]
        for ref, origin in (("west-paid", "west"), ("east-paid", "east"), ("manual", "west")):
            member = authenticate(conn, enroll(conn, origin, org, "reviewer", 1, 1))
            assert reviews.freeze(conn, member, requests["skill"], "PROSE", requests[ref])["skill_id"] == "voice"
        print(json.dumps(requests))


if __name__ == "__main__":
    seed_legacy()
else:
    from test_installation_ownership import isolated_database


def test_legacy_paid_review_requires_own_nf_installation(isolated_database, tmp_path, record_property):
    from agent_workshop.organization import claim_organization
    from agent_workshop.server import dispatch

    dsn, config = isolated_database
    command.upgrade(config, "w004a")
    archive = tmp_path / "old.tar"
    subprocess.run(["git", "archive", "--format=tar", "--output", archive, LEGACY, "workshop"],
                   cwd=Path(__file__).resolve().parents[2], check=True)
    with tarfile.open(archive) as contents:
        contents.extractall(tmp_path, filter="data")
    old = tmp_path / "workshop"
    seeded = subprocess.check_output([sys.executable, str(Path(__file__).resolve())], cwd=old, text=True,
                                    env={**os.environ, "PYTHONPATH": str(old / "src"), "AW_DATABASE_URL": dsn})
    refs = json.loads(seeded)
    history_sql = """SELECT d.operation_id::text,d.installation_id,d.state,r.receipt_ref::text,r.task_state,
                     r.accounting_state,h.status,h.settled_amount::text FROM aw_delegations d
                     JOIN aw_remote_receipts r USING(org_id,operation_id) JOIN aw_budget_holds h USING(org_id,operation_id)
                     ORDER BY d.installation_id"""
    with connect(dsn) as conn:
        before = conn.execute(history_sql).fetchall()
        assert len(before) == 2
    command.upgrade(config, "head")
    with connect(dsn) as conn:
        assert conn.execute(history_sql).fetchall() == before
        assert conn.execute("SELECT count(*) AS n FROM aw_skill_snapshots").fetchone()["n"] == 3
        assert conn.execute("SELECT owner_installation_id FROM aw_organizations").fetchone()["owner_installation_id"] is None
        claim_organization(conn, "legacy-film", "west")
        reviewer = {"database_url": dsn, "token": enroll(conn, "west", "legacy-film", "reviewer", 1, 1)}
        lead = {"database_url": dsn, "token": enroll(conn, "west", "legacy-film", "review-lead", 1, 1)}
        with pytest.raises(Denied, match="ORGANIZATION_UNAVAILABLE"):
            enroll(conn, "east", "legacy-film", "reviewer", 1, 1)
    def read(ref):
        return dispatch(reviewer, "aw_review_get", {"review_id": refs[ref]})
    def freeze(ref):
        return dispatch(reviewer, "aw_skill_freeze", {"binding_id": refs["skill"], "task_ref": refs[ref], "mode": "PROSE"})
    def frozen_history(ref):
        return dispatch(reviewer, "aw_skill_history", {"task_ref": refs[ref], "skill_id": "voice"})
    def decide(ref):
        return dispatch(reviewer, "aw_review_decide", {"review_id": refs[ref], "operation_id": "sign-" + ref,
                        "outcome": "APPROVED", "reason": "VOICE", "evidence_ref": ref + "-evidence"})
    for ref in ("manual", "west-paid"):
        assert read(ref)["artifact_ref"] == ref
        assert freeze(ref)["skill_id"] == "voice"
        assert frozen_history(ref)["historical"] is True
        assert decide(ref)["state"] == "APPROVED"
        assert decide(ref)["state"] == "APPROVED"
    with pytest.raises(Denied, match="REVIEW_DENIED"):
        read("east-paid")
    with pytest.raises(Denied, match="TASK_SCOPE_DENIED"):
        freeze("east-paid")
    with pytest.raises(Denied, match="TASK_SCOPE_DENIED"):
        frozen_history("east-paid")
    with pytest.raises(Denied, match="REVIEW_DENIED"):
        decide("east-paid")
    summary = dispatch(lead, "aw_review_get", {"review_id": refs["east-paid"]})
    assert summary["state"] == "PENDING" and "evidence_refs" not in summary
    args = {"review_id": refs["east-paid"], "operation_id": "escalate-origin"}
    assert dispatch(lead, "aw_review_reconcile", args)["state"] == "ESCALATED"
    assert dispatch(lead, "aw_review_reconcile", args)["state"] == "ESCALATED"
    with connect(dsn) as conn:
        history = conn.execute("""SELECT seq,reason FROM aw_review_evidence WHERE review_id=%s ORDER BY seq""",
                               (refs["east-paid"],)).fetchall()
        assert [row["reason"] for row in history] == ["UNCERTAIN", "ORIGIN_MISMATCH"]
        after = conn.execute(history_sql).fetchall()
        assert after == before
        revision = conn.execute("SELECT version_num FROM aw_alembic_version").fetchone()["version_num"]
        dispositions = conn.execute("""SELECT artifact_ref,state,decision_reason FROM aw_reviews ORDER BY artifact_ref""").fetchall()
    if out := os.environ.get("AW_W4_LEGACY_REVIEW_EVIDENCE"):
        Path(out).write_text(json.dumps({
            "legacy_application_sha": LEGACY, "upgraded_aw_revision": revision, "claimed_installation": "west",
            "source_scope": "Historical application requests backed by controlled settled NF receipt fixtures.",
            "old_receipts": before, "preserved_receipts": after, "dispositions": dispositions,
            "foreign_assignment_history": history, "paid_provider_requests": 0,
        }, indent=2) + "\n")
    record_property("legacy_w4a_sha", LEGACY)
    record_property("old_paid_receipts_and_holds_preserved", len(before))
    record_property("review_dispositions", "manual=APPROVED,west-paid=APPROVED,east-paid=ESCALATED")
