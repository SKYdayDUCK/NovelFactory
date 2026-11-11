"""Upgrade populated original W4 revisions before signing an existing review."""
from decimal import Decimal
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
        for ref, inst in (("west-paid", "west"), ("east-paid", "east"), ("manual", None),
                          ("east-signed", "east")):
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
        east_reviewer = authenticate(conn, enroll(conn, "east", org, "reviewer", 1, 1))
        assert reviews.decide(conn, east_reviewer, "sign-east-signed", requests["east-signed"],
                              "APPROVED", "VOICE", "east-signed-evidence")["state"] == "APPROVED"
        # The first W4 release could sign a review, but its API lacked reconciliation.
        reconcile = getattr(reviews, "reconcile_review", None)
        requests["prior_reconcile_recorded"] = reconcile is not None
        if reconcile is not None:
            east_lead = authenticate(conn, enroll(conn, "east", org, "review-lead", 1, 1))
            assert reconcile(conn, east_lead, "escalate-origin", requests["east-paid"])["state"] == "PENDING"
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
    import psycopg
    from test_installation_ownership import isolated_database
    from test_w4_routing_upgrade import route_view, rows, save_receipt, verify_signed_catalog


def archived_app(source, directory):
    directory.mkdir()
    archive = directory / "source.tar"
    subprocess.run(["git", "archive", "--format=tar", "--output", archive, source, "workshop"],
                   cwd=Path(__file__).resolve().parents[2], check=True)
    with tarfile.open(archive) as contents:
        contents.extractall(directory, filter="data")
    return directory / "workshop"


def old_upgrade(app, dsn):
    env = {**os.environ, "PYTHONPATH": str(app / "src"), "AW_DATABASE_URL": dsn}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=app, env=env, check=True)
    return env


@pytest.mark.parametrize(("old_revision", "deployed"), [
    ("w004", "71b471416c9407e348d4bdce817588def5f11d5b"),
    ("w004a", LEGACY),
    ("w004b", "4f7d8ca35bdf0e04045986950edf5819bd572e80"),
], ids=["w004", "w004a", "w004b"])
def test_legacy_paid_review_requires_own_nf_installation(isolated_database, tmp_path, record_property,
                                                         old_revision, deployed):
    from agent_workshop.organization import claim_organization
    from agent_workshop.server import dispatch

    dsn, config = isolated_database
    source = LEGACY if old_revision == "w004b" else deployed
    old = archived_app(source, tmp_path / "original")
    env = old_upgrade(old, dsn)
    seeded = subprocess.check_output([sys.executable, str(Path(__file__).resolve())], cwd=old, text=True, env=env)
    if old_revision == "w004b":
        old_upgrade(archived_app(deployed, tmp_path / "deployment"), dsn)
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchone() == (old_revision,)
        assert route_view(conn) is None
        fields, history_rows = rows(conn)
    refs = json.loads(seeded)
    assert refs["prior_reconcile_recorded"] == (old_revision != "w004")
    history_sql = """SELECT d.operation_id::text,d.installation_id,d.state,r.receipt_ref::text,r.task_state,
                     r.accounting_state,h.status,h.settled_amount::text FROM aw_delegations d
                     JOIN aw_remote_receipts r USING(org_id,operation_id) JOIN aw_budget_holds h USING(org_id,operation_id)
                     ORDER BY d.installation_id,d.operation_id"""
    with connect(dsn) as conn:
        before = conn.execute(history_sql).fetchall()
        assert sorted(row["installation_id"] for row in before) == ["east", "east", "west"]
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with psycopg.connect(dsn) as conn:
        assert route_view(conn)
        assert rows(conn, fields)[1] == history_rows
        # Preserve the stored early ASK edge but apply the signed live routing policy.
        edge = "WHERE src='reviewer' AND dst='review-lead' AND kind='ASK'"
        assert conn.execute("SELECT EXISTS (SELECT FROM aw_edges " + edge + ")").fetchone() == (True,)
        assert conn.execute("SELECT EXISTS (SELECT FROM aw_current_message_edges " + edge + ")").fetchone() == (False,)
        assert conn.execute("SELECT EXISTS (SELECT FROM aw_current_message_edges WHERE kind='REPORT')").fetchone() == (True,)
    catalog_hash = verify_signed_catalog(dsn)
    with connect(dsn) as conn:
        assert conn.execute(history_sql).fetchall() == before
        assert conn.execute("SELECT count(*) AS n FROM aw_skill_snapshots").fetchone()["n"] == 3
        assert conn.execute("SELECT owner_installation_id FROM aw_organizations").fetchone()["owner_installation_id"] is None
        claim_organization(conn, "legacy-film", "west")
        reviewer = {"database_url": dsn, "token": enroll(conn, "west", "legacy-film", "reviewer", 1, 1)}
        lead = {"database_url": dsn, "token": enroll(conn, "west", "legacy-film", "review-lead", 1, 1)}
        with pytest.raises(Denied, match="ORGANIZATION_UNAVAILABLE"):
            enroll(conn, "east", "legacy-film", "reviewer", 1, 1)
    # W5's employee history must preserve the same installation boundary as the
    # W4 assignment. The organization still owes every recorded settled charge.
    history_rows_for_reviewer = dispatch(reviewer, "aw_history_get", {"limit": 200})["events"]
    assert {row["ref_id"] for row in history_rows_for_reviewer if row["source"] == "aw_reviews"} == {
        refs["manual"], refs["west-paid"],
    }
    with connect(dsn) as conn:
        manager = {"database_url": dsn, "token": enroll(conn, "west", "legacy-film", "manager", 1, 1)}
    account = dispatch(manager, "aw_state_get", {})["budget"]
    assert Decimal(account["settled"]) == sum(Decimal(row["settled_amount"]) for row in before)
    manager_events = dispatch(manager, "aw_history_get", {"limit": 200})["events"]
    for source_name in ("aw_delegations", "aw_remote_receipts", "aw_budget_holds"):
        assert {row["ref_id"] for row in manager_events if row["source"] == source_name} == {
            row["operation_id"] for row in before if row["installation_id"] == "west"
        }

    def read(ref):
        return dispatch(reviewer, "aw_review_get", {"review_id": refs[ref]})
    def freeze(ref):
        return dispatch(reviewer, "aw_skill_freeze", {"binding_id": refs["skill"], "task_ref": refs[ref], "mode": "PROSE"})
    def frozen_history(ref):
        return dispatch(reviewer, "aw_skill_history", {"task_ref": refs[ref], "skill_id": "voice"})
    def decide(ref):
        return dispatch(reviewer, "aw_review_decide", {"review_id": refs[ref], "operation_id": "sign-" + ref,
                        "outcome": "APPROVED", "reason": "VOICE", "evidence_ref": ref + "-evidence"})
    def request_again(ref):
        arguments = {"operation_id": ref, "artifact_ref": ref, "artifact_revision": "v1",
                     "mode": "PROSE", "risk_reason": "MATERIAL_CHANGE", "ttl_seconds": 3600,
                     "reviewer_id": "reviewer"}
        if ref != "manual":
            arguments.update(slot_id=ref, token_limit=100, max_amount="0.10")
        return dispatch(lead, "aw_review_request", arguments)
    for ref in ("manual", "west-paid"):
        assert request_again(ref)["review_id"] == refs[ref]
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
    with pytest.raises(Denied, match="REVIEW_DENIED"):
        request_again("east-paid")
    with pytest.raises(Denied, match="REVIEW_DENIED"):
        decide("east-signed")
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
    assert {r["artifact_ref"]: r["state"] for r in dispositions} == {
        "manual": "APPROVED", "west-paid": "APPROVED", "east-paid": "ESCALATED", "east-signed": "APPROVED",
    }
    save_receipt(old_revision, {"deployed_source_sha": deployed, "seed_source_sha": source,
                               "previous_revision": old_revision, "upgraded_revision": revision,
                               "prior_reconcile_recorded": refs["prior_reconcile_recorded"],
                               "preserved_table_digests": history_rows, "catalog_sha256": catalog_hash,
                               "old_receipts": before, "preserved_receipts": after, "dispositions": dispositions})
    if old_revision == "w004a" and (out := os.environ.get("AW_W4_LEGACY_REVIEW_EVIDENCE")):
        Path(out).write_text(json.dumps({
            "legacy_application_sha": LEGACY, "upgraded_aw_revision": revision, "claimed_installation": "west",
            "source_scope": "Historical application requests backed by controlled settled NF receipt fixtures.",
            "old_receipts": before, "preserved_receipts": after, "dispositions": dispositions,
            "foreign_assignment_history": history, "paid_provider_requests": 0,
        }, indent=2) + "\n")
    if old_revision == "w004a":
        record_property("legacy_w4a_sha", LEGACY)
    record_property("deployed_sha", deployed)
    record_property("old_paid_receipts_and_holds_preserved", len(before))
    record_property("review_dispositions", "manual=APPROVED,west-paid=APPROVED,east-paid=ESCALATED,east-signed=APPROVED")
