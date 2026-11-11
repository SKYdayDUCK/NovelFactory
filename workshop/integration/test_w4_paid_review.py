"""Exercise a paid-review slot across live services using the local synthetic HTTP provider."""

import asyncio
import hashlib
import json
import os
from contextlib import closing
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
from agent_workshop.reviews_skills import register_artifact
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from test_dual_stdio import bound_process
from test_paired_restore import aw_state, tick


def test_review_waits_for_accepted_nf_result_and_settled_original_attempt(
    dual_databases, tmp_path, monkeypatch, record_property
):
    root = Path(os.environ["AW_NF_SOURCE"])
    monkeypatch.syspath_prepend(str(root / "tests/integration"))
    from test_p5_supervisor import backend

    aw_dsn, nf_dsn = dual_databases["aw"], dual_databases["nf"]
    org, instance = str(uuid4()), str(uuid4())
    objects = tmp_path / "objects"

    def service(identity, module, config, source):
        folder = tmp_path / identity
        folder.mkdir()
        return remote(bound_process(folder, module, config, source))

    with closing(Database(nf_dsn)) as db, backend() as (adapter, requests, http):
        kernel = Kernel(db, ObjectStore(objects))
        world = load_fixture(kernel)
        wr, controller = world["world_ref"], world["principal"]
        grant = install(
            kernel,
            controller,
            nf_instance_id=instance,
            org_id=org,
            work_id=wr["work_id"],
            branch_id=wr["branch_id"],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        inst = grant["installation_id"]
        http.candidate = lambda ctx: {
            "summary": "Fixture reviewed the assigned material.",
            "issues": [],
            "counterinterpretations": [],
            "evidence_refs": [],
        }
        gateway = AgentGateway(kernel, {"fixture": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        frozen = RuntimeTaskService(kernel, gateway).define(
            controller,
            wr,
            role_id="reviewer",
            task_kind="literary",
            story_time=point(1),
            subject_ids=[world["ids"]["sword"]],
            predicates=["owner"],
            assignment="Review the authorized material.",
            adapter_id="fixture",
            max_amount="0.10",
            token_budget=100000,
        )
        budget = kernel.tasks.create_budget(wr["work_id"], "2")
        policy = dict(
            nf_instance_id=instance,
            work_id=wr["work_id"],
            branch_id=wr["branch_id"],
            scope_ref="text",
            task_template_id="prose-review",
            generation=0,
            grant_epoch=grant["grant_epoch"],
            actor_binding_ref=None,
            role_id="reviewer",
            max_amount="0.10",
            currency="USD",
            token_limit=100000,
        )
        provision_slot(
            supervisor,
            controller,
            inst,
            scope_ref="text",
            task_template_id="prose-review",
            frozen=frozen,
            budget_id=budget,
            reserve_amount="0.10",
            token_reservation=100000,
            context_tokens=20000,
        )
        with connect(aw_dsn) as conn:
            conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (inst, instance))
            bootstrap(conn, org, "Research-Audit", installation_id=inst)
            for person in ("writer", "reviewer"):
                subject(conn, org, person, person)
            register_artifact(
                conn,
                org,
                "text",
                "v1",
                hashlib.sha256(b"source").hexdigest(),
                "organization",
                "source-receipt",
                ["writer"],
                ["source-A"],
                {},
            )
            allow_slot(conn, org, inst, "review.slot", "reviewer", policy)
            review_slot(conn, org, "review.slot", "text", "v1", "PROSE")
            conn.execute("INSERT INTO aw_budget_accounts VALUES(%s,1,'USD')", (org,))
            members = {
                employee: enroll(conn, inst, org, employee, 1, 1)
                for employee in ("review-lead", "reviewer")
            }
        lead, reviewer = [
            service(
                identity,
                "agent_workshop.server",
                {"token": token, "database_url": aw_dsn},
                root / "workshop/src",
            )
            for identity, token in members.items()
        ]
        nf = service(
            "nf",
            "novel_factory.interfaces.delegation_stdio",
            {
                "token": grant["token"],
                "installation_id": inst,
                "database_url": nf_dsn,
                "object_root": str(objects),
            },
            root / "src",
        )
        request = dict(
            operation_id="paid-review",
            artifact_ref="text",
            artifact_revision="v1",
            mode="PROSE",
            risk_reason="MATERIAL_CHANGE",
            ttl_seconds=600,
            reviewer_id="reviewer",
            slot_id="review.slot",
            max_amount="0.10",
            token_limit=100000,
        )
        initial = asyncio.run(lead("aw_review_request", request))
        assert asyncio.run(lead("aw_review_request", request)) == initial
        op = initial["operation_id"]
        decision = dict(
            operation_id="decision",
            review_id=initial["review_id"],
            outcome="APPROVED",
            reason="VOICE",
            evidence_ref="source-A",
        )
        with pytest.raises(Denied, match="WAITING_NF"):
            asyncio.run(reviewer("aw_review_decide", decision))
        assert tick(aw_dsn, inst, org, op, nf) == "BOUND"
        supervisor.run_once(controller, wr["work_id"], wr["branch_id"])
        with connect(aw_dsn) as conn:
            payload = conn.execute(
                "SELECT payload FROM aw_delegations WHERE operation_id=%s", (op,)
            ).fetchone()["payload"]
        lookup = {k: payload[k] for k in GET_FIELDS}
        result = asyncio.run(nf("nf_delegation_get", lookup))
        assert result["task_state"] == "RESULT_STORED" and result["accounting_state"] == "UNKNOWN"
        assert len(result["attempt_ids"]) == len(requests) == 1
        # The NF operator verifies the current task result before accepting it.
        with db.uow() as repo:
            task = repo.one("tasks", {"task_id": result["task_id"]})
            stored = repo.one("task_results", {"result_id": task["current_result_id"]})
            audit = gateway._read_audit(repo, result["attempt_ids"][0])
            assert (
                stored["task_id"] == result["task_id"]
                and stored["generation"] == result["generation"]
            )
            assert audit["payload"]["outcome"] == "VALIDATED_CANDIDATE"
            assert audit["payload"]["result_status"] == "SUCCEEDED"
            assert stored["payload"]["artifacts"][1] == audit["payload"]["output_ref"]
        kernel.tasks.transition_result(result["task_id"], result["generation"], "VALIDATING")
        kernel.tasks.transition_result(result["task_id"], result["generation"], "ACCEPTED")
        assert tick(aw_dsn, inst, org, op, nf) == "BOUND"
        assert aw_state(aw_dsn, op)["hold"] == "HELD"
        with pytest.raises(Denied, match="WAITING_NF"):
            asyncio.run(reviewer("aw_review_decide", decision))
        attempt = result["attempt_ids"][0]
        kernel.tasks.settle_call(
            attempt,
            state="SUCCEEDED",
            actual_amount="0.02",
            actual_tokens=18,
            provider_receipt="synthetic-W4-invoice",
        )
        assert tick(aw_dsn, inst, org, op, nf) == "SETTLED"
        final = asyncio.run(reviewer("aw_review_decide", decision))
        assert (
            final["state"] == "APPROVED"
            and asyncio.run(reviewer("aw_review_decide", decision)) == final
        )
        account = aw_state(aw_dsn, op)
        assert account["observations"] == 2 and str(account["settled_amount"]) == "0.020000"
        with connect(aw_dsn) as conn:
            observed = conn.execute(
                "SELECT attempt_id::text, state FROM aw_cost_observations "
                "WHERE org_id=%s AND operation_id=%s",
                (org, op),
            ).fetchall()
        assert {(row["attempt_id"], row["state"]) for row in observed} == {
            (attempt, "UNKNOWN"),
            (attempt, "SUCCEEDED"),
        }
        paid = asyncio.run(nf("nf_delegation_get", lookup))
        assert paid["task_id"] == result["task_id"] and paid["attempt_ids"] == [attempt]
        assert paid["accounting_state"] == "SETTLED" and paid["actual_tokens"] == 18
        record_property(
            "paid_review",
            json.dumps(
                {
                    "operation_id": op,
                    "review_id": initial["review_id"],
                    "task_id": paid["task_id"],
                    "generation": paid["generation"],
                    "attempt_id": attempt,
                    "reserved": "0.10",
                    "settled": str(account["settled_amount"]),
                    "tokens": 18,
                    "result": final["state"],
                    "loopback_http_requests": len(requests),
                    "commercial_calls": 0,
                }
            ),
        )
