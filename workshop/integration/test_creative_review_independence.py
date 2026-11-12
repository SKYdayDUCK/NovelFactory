"""Production NF and Workshop handoffs, subject groups and independent review on isolated databases."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from agent_workshop.admin import enroll
from agent_workshop.auth import authenticate, connect
from agent_workshop.delegation import delegate
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import reconcile, remote
from agent_workshop.review_admin import review_slot, subject
from agent_workshop.reviews_skills import register_artifact, review_request
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.kernel import Kernel
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.release import ReleaseService
from novel_factory.application.review_independence import ReviewIndependenceService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from test_dual_stdio import bound_process


def test_dual_review_uses_exact_delegations_and_denies_after_workshop_revocation(dual_databases, tmp_path, monkeypatch):
    root = Path(os.environ["AW_NF_SOURCE"]).resolve()
    monkeypatch.syspath_prepend(str(root / "tests" / "integration"))
    from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
    from test_release import setup_batch

    dsn = dual_databases["aw"]
    db = Database(dual_databases["nf"])
    kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
    instance, org = str(uuid4()), str(uuid4())
    try:
        world = setup_batch(kernel, count=1)
        wr, admin = world["world_ref"], world["principal"]
        installed = install(kernel, admin, nf_instance_id=instance, org_id=org, work_id=wr["work_id"],
                            branch_id=wr["branch_id"], expires_at=datetime.now(UTC) + timedelta(hours=1))
        installation = installed["installation_id"]
        nf = bound_process(tmp_path, "novel_factory.interfaces.delegation_stdio",
                           {"installation_id": installation, "token": installed["token"], "database_url": dual_databases["nf"],
                            "object_root": str(kernel.objects.root)}, root / "src")
        gateway = AgentGateway(kernel, {"fixture": OpenAICompatibleAdapter(endpoint="https://example.invalid/v1/chat/completions", model="fixture", api_key=None)})
        supervisor, tasks = RuntimeSupervisor(kernel, gateway), RuntimeTaskService(kernel, gateway)
        budget = kernel.tasks.create_budget(wr["work_id"], "3")
        with connect(dsn) as conn:
            conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, instance))
            bootstrap(conn, org, "Research-Audit", installation_id=installation)
            conn.execute("INSERT INTO aw_budget_accounts VALUES (%s,3,'USD')", (org,))
            for name, group in (("writer", "story-team"), ("reviewer", "independent-team")):
                subject(conn, org, name, group)
            manager_token = enroll(conn, installation, org, "manager", 1, 1)
        private = tmp_path / "workshop.json"
        private.write_text(json.dumps({"database_url": dsn, "token": manager_token}))
        private.chmod(0o600)
        monkeypatch.setenv("NF_REVIEW_WORKSHOP_BINDING", str(private))

        def dispatch(role_id, inputs, manuscript=None, producer_receipt=None, *, slot=None, mode="PROSE"):
            slot = slot or role_id
            kind = "scene_draft" if role_id == "writer" else "literary"
            frozen = tasks.define(admin, wr, role_id=role_id, task_kind=kind, story_time=point(1, 1),
                                  subject_ids=[world["ids"]["sword"]], predicates=["owner"], assignment="审阅灯船抵岸。",
                                  adapter_id="fixture", input_artifacts=inputs, max_amount="1", token_budget=60000)
            provision_slot(supervisor, admin, installation, scope_ref=slot, task_template_id=kind, frozen=frozen,
                           budget_id=budget, reserve_amount="1", token_reservation=60000)
            policy = {"nf_instance_id": instance, "work_id": wr["work_id"], "branch_id": wr["branch_id"],
                      "generation": 0, "grant_epoch": installed["grant_epoch"], "scope_ref": slot, "task_template_id": kind,
                      "actor_binding_ref": None, "role_id": role_id, "max_amount": "1", "currency": "USD", "token_limit": 60000}
            with connect(dsn) as conn:
                allow_slot(conn, org, installation, slot, role_id, policy)
                sender = conn.execute("""SELECT src FROM aw_current_message_edges WHERE org_id=%s AND dst=%s
                                      AND kind IN ('ASSIGN','ASSIGN_REVIEW') AND (%s='writer' OR src='review-lead') LIMIT 1""",
                                      (org, role_id, role_id)).fetchone()["src"]
                session = authenticate(conn, enroll(conn, installation, org, sender, 1, 1))
                if manuscript:
                    register_artifact(conn, org, manuscript["artifact_id"], manuscript["artifact_revision_id"], manuscript["sha256"],
                                      "organization", producer_receipt, ["writer"], [manuscript["sha256"]], {})
                    review_slot(conn, org, slot, manuscript["artifact_id"], manuscript["artifact_revision_id"], mode)
                    req = review_request(conn, session, "ask-" + slot, manuscript["artifact_id"], manuscript["artifact_revision_id"],
                                         mode, "MATERIAL_CHANGE", 600, role_id, slot, "1", 60000)
                else:
                    req = delegate(conn, session, "write", role_id, "1", 60000)
            state = asyncio.run(reconcile(dsn, installation, org, req["operation_id"], remote(nf)))
            assert state == "BOUND"
            tid = frozen.spec["task_id"]
            task = kernel.claim_task(frozen.principal, "worker-" + role_id, task_id=tid, lease_seconds=600)
            with connect(dsn) as conn:
                conn.execute("UPDATE aw_delegations SET next_check=clock_timestamp() WHERE org_id=%s AND operation_id=%s",
                             (org, req["operation_id"]))
            assert asyncio.run(reconcile(dsn, installation, org, req["operation_id"], remote(nf))) == "BOUND"
            with connect(dsn) as conn:
                rr = conn.execute("SELECT * FROM aw_remote_receipts WHERE org_id=%s AND operation_id=%s",
                                  (org, req["operation_id"])).fetchone()
                assert str(rr["task_id"]) == tid and rr["generation"] == task["generation"]
            compiled = WorldQueryService(kernel).context_compile(frozen.principal, {
                "schema_version": "2.1", "task_id": tid, "generation": task["generation"],
                "recipe_id": role_id.upper(), "input_manifest_ref": frozen.manifest_ref,
                "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr}, "process_policy": "ALLOW_PARTIAL",
                                  "index_policy": "AUTHORITATIVE", "max_wait_ms": 0}, "knowledge_cut": frozen.cut,
                "subject_time": {"kind": "POINT", "at": point(1, 1)}, "token_budget": 30000,
                "idempotency_key": "compile-" + role_id})
            assert compiled["status"] == "READY"
            return frozen.principal, task, str(rr["receipt_ref"])

        inputs = [world["plan_ref"], world["batch_ref"], world["scene_refs"][0]]
        author, writing, receipt = dispatch("writer", inputs)
        tid, gen = writing["task_id"], writing["generation"]
        PlanningService(kernel).bind_task_for_controller(admin, tid, gen, world["plan_ref"],
                                                         world["scenes"][0]["plan_node_ids"], "bind-plan")
        ReleaseService(kernel).bind_writer_task(author, tid, gen, world["scene_refs"][0], [], "bind-writer")
        narrative = NarrativeService(kernel)
        draft = narrative.store_draft(author, tid, gen, world["scene_refs"][0], "雨落在船头。她把灯挪向岸边。", "write")
        reviewer, reading, _ = dispatch("reviewer", [draft], draft, receipt)
        narrative.bind_reader_task(admin, reading["task_id"], reading["generation"], [draft], "bind-review")
        service = ReviewIndependenceService(kernel)
        args = {"manuscript_ref": draft, "reviewer_task_id": reading["task_id"], "reviewer_generation": reading["generation"]}
        proof = service.issue(admin, **args)["independence_ref"]
        _, second, _ = dispatch("reviewer", [draft], draft, receipt, slot="review-story", mode="SCRIPT")
        narrative.bind_reader_task(admin, second["task_id"], second["generation"], [draft], "bind-story-review")
        other = service.issue(admin, manuscript_ref=draft, reviewer_task_id=second["task_id"],
                              reviewer_generation=second["generation"])["independence_ref"]
        batch = service.refresh_many([other, proof, other])
        assert list(batch) == [other["artifact_revision_id"], proof["artifact_revision_id"]]
        live = batch[proof["artifact_revision_id"]]
        with kernel.db.uow() as repo:
            out = service.verify(repo, proof_ref=proof, **args, creator_task_id=tid, creator_generation=gen, live_proof=live)
            assert out["producer"]["principal_id"] == author.principal_id
            assert out["reviewer"]["principal_id"] == reviewer.principal_id
        with connect(dsn) as conn:
            conn.execute("UPDATE aw_delegation_slots SET enabled=false WHERE org_id=%s AND slot_id='reviewer'", (org,))
        with pytest.raises(DomainError, match="qualified"):
            service.refresh_many([other, proof])
        assert service.refresh(other)
        with pytest.raises(DomainError, match="qualified"):
            service.issue(admin, **args)
    finally:
        db.close()
