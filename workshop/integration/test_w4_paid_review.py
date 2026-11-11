"""Exercise a paid-slot review with two service identities and one loopback provider invoice."""

import asyncio
import json
import os
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, connect
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import GET_FIELDS, remote
from agent_workshop.review_admin import review_slot, subject
from agent_workshop.reviews_skills import register_artifact, register_skill
from test_dual_stdio import bound_process
from test_paired_restore import aw_state, tick

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.editorial import ReviewService
from novel_factory.application.kernel import Kernel
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def test_independent_review_uses_original_nf_result_and_single_invoice(
    dual_databases, tmp_path, monkeypatch, record_property
):
    root = Path(os.environ["AW_NF_SOURCE"])
    monkeypatch.syspath_prepend(str(root / "tests/integration"))
    from editorial_helpers import review_request
    from test_editorial_service import prepared
    from test_p5_supervisor import backend

    aw_dsn = dual_databases["aw"]
    with ExitStack() as stack:
        db = Database(dual_databases["nf"])
        stack.callback(db.close)
        kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
        fixture, _, binding = prepared(kernel)
        owner, wr = fixture["principal"], binding["world_ref"]
        work, branch = wr["work_id"], wr["branch_id"]
        draft = binding["ordered_draft_refs"][0]
        source_text = NarrativeService(kernel).load_draft(owner, work, draft)["text_ref"]
        artifact, revision = draft["artifact_id"], draft["artifact_revision_id"]
        org, instance = str(uuid4()), str(uuid4())
        installation = install(
            kernel,
            owner,
            nf_instance_id=instance,
            org_id=org,
            work_id=work,
            branch_id=branch,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        inst = installation["installation_id"]
        adapter, requests, server = stack.enter_context(backend())
        candidate = {
            "summary": "工程样本已阅读。",
            "issues": [],
            "counterinterpretations": ["保留多义解释。"],
            "evidence_refs": [],
        }
        server.candidate = lambda context: candidate
        gateway = AgentGateway(kernel, {"fixture": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        definition = RuntimeTaskService(kernel, gateway).define(
            owner,
            wr,
            role_id="reviewer",
            task_kind="literary",
            story_time=point(1000),
            subject_ids=[fixture["ids"]["sword"]],
            predicates=["possessed_by"],
            assignment="Review the assigned scene.",
            input_artifacts=binding["ordered_draft_refs"],
            adapter_id="fixture",
            max_amount="0.10",
            token_budget=100000,
        )
        slot = "scene.review"
        provision_slot(
            supervisor,
            owner,
            inst,
            scope_ref=slot,
            task_template_id="PROSE",
            frozen=definition,
            budget_id=kernel.tasks.create_budget(work, "1"),
            reserve_amount="0.10",
            token_reservation=100000,
        )

        def client(label, module, config, source):
            directory = tmp_path / label
            directory.mkdir()
            return remote(bound_process(directory, module, config, source))

        def call(peer, name, **arguments):
            return asyncio.run(peer(name, arguments))

        aw_clients = {}
        with connect(aw_dsn) as conn:
            conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (inst, instance))
            bootstrap(conn, org, "Research-Audit", installation_id=inst)
            for person in ("writer", "reviewer"):
                subject(conn, org, person, person)
            register_artifact(
                conn,
                org,
                artifact,
                revision,
                source_text["sha256"],
                "organization",
                "source-receipt",
                ["writer"],
                ["source-span"],
                {},
            )
            register_skill(
                conn, org, "close-reading", "v1", "creative", "PROSE", b"Read the assigned scene."
            )
            conn.execute("INSERT INTO aw_budget_accounts VALUES (%s,1,'USD')", (org,))
            allow_slot(
                conn,
                org,
                inst,
                slot,
                "reviewer",
                {
                    "nf_instance_id": instance,
                    "work_id": work,
                    "branch_id": branch,
                    "actor_binding_ref": None,
                    "role_id": "reviewer",
                    "task_template_id": "PROSE",
                    "scope_ref": slot,
                    "generation": 0,
                    "grant_epoch": installation["grant_epoch"],
                    "max_amount": "0.10",
                    "currency": "USD",
                    "token_limit": 100000,
                },
            )
            review_slot(conn, org, slot, artifact, revision, "PROSE")
            for name in ("manager", "review-lead", "reviewer", "writer"):
                aw_clients[name] = client(
                    name,
                    "agent_workshop.server",
                    {"database_url": aw_dsn, "token": enroll(conn, inst, org, name, 1, 1)},
                    Path(__file__).resolve().parents[1] / "src",
                )
        nf = client(
            "nf",
            "novel_factory.interfaces.delegation_stdio",
            {
                "token": installation["token"],
                "installation_id": inst,
                "database_url": dual_databases["nf"],
                "object_root": str(tmp_path / "objects"),
            },
            root / "src",
        )
        lead, reviewer = aw_clients["review-lead"], aw_clients["reviewer"]
        args = {
            "operation_id": "paid-review",
            "artifact_ref": artifact,
            "artifact_revision": revision,
            "mode": "PROSE",
            "risk_reason": "MATERIAL_CHANGE",
            "ttl_seconds": 600,
            "reviewer_id": "reviewer",
            "slot_id": slot,
            "max_amount": "0.10",
            "token_limit": 100000,
        }
        pending = call(lead, "aw_review_request", **args)
        assert call(lead, "aw_review_request", **args) == pending
        operation, review = pending["operation_id"], pending["review_id"]
        share = call(
            aw_clients["manager"],
            "aw_skill_share",
            operation_id="share",
            skill_id="close-reading",
            source_revision="v1",
            policy="ROLE_SCOPED",
            ttl_seconds=600,
            target="reviewer",
        )
        frozen = call(
            reviewer,
            "aw_skill_freeze",
            binding_id=share["binding_id"],
            mode="PROSE",
            task_ref=review,
        )
        assert frozen["content_sha"] == share["content_sha"]
        decision = {
            "operation_id": "decision",
            "review_id": review,
            "outcome": "APPROVED",
            "reason": "VOICE",
            "evidence_ref": "source-span",
        }
        with pytest.raises(Denied, match="VIEW_DENIED"):
            call(aw_clients["writer"], "aw_review_decide", **decision)
        with pytest.raises(Denied, match="WAITING_NF_RECONCILIATION"):
            call(reviewer, "aw_review_decide", **decision)
        assert tick(aw_dsn, inst, org, operation, nf) == "BOUND"
        with connect(aw_dsn) as conn:
            payload = conn.execute(
                "SELECT payload FROM aw_delegations WHERE operation_id=%s", (operation,)
            ).fetchone()["payload"]
        lookup = {name: payload[name] for name in GET_FIELDS}
        supervisor.run_once(owner, work, branch)
        assert len(requests) == 1
        task_id = definition.spec["task_id"]
        task = kernel.get_task(definition.principal, task_id)
        received = call(nf, "nf_delegation_get", **lookup)
        assert received["task_state"] == task["state"] == "RESULT_STORED"
        assert received["task_id"] == task_id and received["accounting_state"] == "UNKNOWN"
        assert len(received["attempt_ids"]) == 1
        gen = task["generation"]
        NarrativeService(kernel).bind_reader_task(
            owner, task_id, gen, binding["ordered_draft_refs"], "bind-reads"
        )
        service = ReviewService(kernel)
        reading = service.read_text(
            definition.principal,
            task_id,
            gen,
            draft,
            0,
            len(kernel.objects.read(source_text["sha256"]).decode()),
            idempotency_key="read",
        )
        submission = review_request(
            binding, task, [reading["coverage_ref"]], "SCENE", "LITERARY", [draft]
        )
        with db.uow(write=False) as repo:
            result = repo.one("task_results", {"result_id": task["current_result_id"]})
            audit = gateway._read_audit(repo, received["attempt_ids"][0])["payload"]
            assert result["task_id"] == task_id and result["generation"] == received["generation"]
            assert (
                audit["outcome"] == "VALIDATED_CANDIDATE" and audit["result_status"] == "SUCCEEDED"
            )
            assert result["payload"]["artifacts"][1] == audit["output_ref"]
        submission.update(
            summary=candidate["summary"],
            counterinterpretations=candidate["counterinterpretations"],
            agent_output_ref=result["payload"]["artifacts"][1],
        )
        report = service.submit_review(definition.principal, submission)
        assert report == service.submit_review(definition.principal, submission)
        # The fixture controller accepts the delivered NF report; the reviewer's Workshop decision is separate.
        kernel.tasks.transition_result(task_id, gen, "VALIDATING")
        kernel.tasks.transition_result(task_id, gen, "ACCEPTED")
        assert tick(aw_dsn, inst, org, operation, nf) == "BOUND"
        assert aw_state(aw_dsn, operation)["accounting_state"] == "UNKNOWN"
        with pytest.raises(Denied, match="WAITING_NF_RECONCILIATION"):
            call(reviewer, "aw_review_decide", **decision)
        with connect(aw_dsn) as conn:
            attempt = conn.execute(
                "SELECT attempt_id FROM aw_cost_observations WHERE operation_id=%s", (operation,)
            ).fetchone()["attempt_id"]
        kernel.tasks.settle_call(
            str(attempt),
            state="SUCCEEDED",
            actual_amount="0.02",
            actual_tokens=18,
            provider_receipt="w4-loopback-invoice",
        )
        assert tick(aw_dsn, inst, org, operation, nf) == "SETTLED"
        closed = call(reviewer, "aw_review_decide", **decision)
        assert (
            closed["state"] == "APPROVED"
            and call(reviewer, "aw_review_decide", **decision) == closed
        )
        settled = aw_state(aw_dsn, operation)
        with connect(aw_dsn) as conn:
            invoice = conn.execute(
                """SELECT actual_tokens, actual_amount FROM aw_cost_observations
                   WHERE operation_id=%s AND attempt_id=%s AND state='SUCCEEDED'""",
                (operation, attempt),
            ).fetchone()
        assert (
            invoice["actual_tokens"] == 18 and invoice["actual_amount"] == settled["settled_amount"]
        )
        paid = call(nf, "nf_delegation_get", **lookup)
        assert paid["task_id"] == task_id and paid["receipt_ref"] == settled["receipt_ref"]
        assert paid["attempt_ids"] == [str(attempt)] == received["attempt_ids"]
        assert (
            paid["accounting_state"] == "SETTLED"
            and paid["actual_tokens"] == invoice["actual_tokens"]
        )
        assert settled["observations"] == 2
        assert str(settled["settled_amount"]) == "0.020000" and len(requests) == 1
        record_property(
            "paid_review",
            json.dumps(
                {
                    "review_id": review,
                    "operation_id": operation,
                    "task_id": task_id,
                    "nf_receipt": settled["receipt_ref"],
                    "nf_report": report,
                    "source_sha": source_text["sha256"],
                    "skill_sha": frozen["content_sha"],
                    "attempt_id": str(attempt),
                    "tokens": invoice["actual_tokens"],
                    "settled": str(invoice["actual_amount"]),
                    "reserved": args["max_amount"],
                    "generation": paid["generation"],
                    "result": closed["state"],
                    "loopback_http_requests": len(requests),
                    "commercial_calls": 0,
                }
            ),
        )
