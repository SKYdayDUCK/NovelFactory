"""Opt-in pytest acceptance using both checkouts and isolated local synthetic providers."""

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
from sqlalchemy import text as sql
from hashlib import sha256
import subprocess
import sys
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import reconcile, remote
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import point

NF = Path(os.environ["AW_NF_SOURCE"]).resolve()
AW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NF / "tests/integration"))
from test_p5_autonomous_banquet import run_banquet
from test_p5_supervisor import backend
from test_narrative import assertion, draft_for, extract
from test_s9_dependencies import query as observed_query
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.conflict_resolution import ConflictResolutionService
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task
from agent_workshop.review_admin import subject
from agent_workshop.reviews_skills import register_artifact

from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

fixture_spec = spec_from_file_location("workshop_dual_fixture", AW / "integration/conftest.py")
dual_fixtures = module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(dual_fixtures)
dual_databases = dual_fixtures.dual_databases


@pytest.fixture
def kernel_env(dual_databases, tmp_path):
    db = Database(dual_databases["nf"], pool_size=8)
    store = ObjectStore(tmp_path / "nf-object-store")
    try:
        yield {"kernel": Kernel(db, store), "url": dual_databases["nf"], "store": store}
    finally:
        db.close()


def source_identity(root, paths):
    files = sorted(
        file
        for item in paths
        for file in ([root / item] if (root / item).is_file() else (root / item).rglob("*.py"))
    )
    hashes = {str(file.relative_to(root)): sha256(file.read_bytes()).hexdigest() for file in files}
    return {
        "head": subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip(),
        "working_changes": subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain", "--", *paths], text=True
        ).splitlines(),
        "source_sha256": sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
    }


def private_file(path, contents):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as out:
        json.dump(contents, out)
    return str(path)


class Mail:
    def __init__(self, dsn, org, installation, temp):
        self.dsn, self.org, self.installation, self.temp = dsn, org, installation, temp
        self.roles = {}
        self.history = []
        self.stack = AsyncExitStack()

    async def call(self, user, tool, args):
        if user not in self.roles:
            with connect(self.dsn) as conn:
                mem = conn.execute(
                    """SELECT employment_revision FROM aw_employees
                    WHERE org_id=%s AND employee_id=%s""",
                    (self.org, user),
                ).fetchone()
                token = enroll(
                    conn, self.installation, self.org, user, mem["employment_revision"], 1
                )
            path = private_file(
                self.temp / (user + "-" + uuid4().hex), {"database_url": self.dsn, "token": token}
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "agent_workshop.server", "--binding", path],
                env={"PYTHONPATH": str(AW / "src")},
            )
            streams = await self.stack.enter_async_context(stdio_client(params))
            client = await self.stack.enter_async_context(ClientSession(*streams))
            info = await client.initialize()
            assert info.serverInfo.name == "AgentWorkshop"
            self.roles[user] = client
        result = await self.roles[user].call_tool(tool, args)
        assert not result.isError, (user, tool, result.structuredContent)
        return result.structuredContent

    async def message(self, sender, recipient, kind, revision, *, parent=None, content=None):
        key = str(uuid4())
        args = {
            "operation_id": key,
            "recipient": recipient,
            "kind": kind,
            "scope_ref": "organization",
            "graph_revision": revision,
            "ttl_seconds": 3600,
        }
        if parent:
            args["causation_id"] = parent
        if content:
            ref = await self.call(
                sender,
                "aw_message_body_put",
                {
                    "recipient": recipient,
                    "operation_id": key + "-body",
                    "scope_ref": "organization",
                    "content": content,
                },
            )
            args["body_ref"] = ref["body_ref"]
        item = await self.call(sender, "aw_message_send", args)
        mid = item["message_id"]
        receipt = await self.call(recipient, "aw_delivery_ack", {"message_id": mid})
        if content:
            body = await self.call(recipient, "aw_message_body_get", {"message_id": mid})
            assert body["content"] == content
        self.history.append(
            {
                "sender": sender,
                "recipient": recipient,
                "kind": kind,
                "message_id": mid,
                "receipt": receipt,
                "causation": parent,
            }
        )
        return mid


@pytest.mark.parametrize("preset", ["Minimal", "Standard", "Long-form", "Research-Audit"])
def test_real_delegation_unblocks_banquet_actor(
    kernel_env, dual_databases, tmp_path, monkeypatch, preset
):
    kernel = kernel_env["kernel"]
    org, instance = str(uuid4()), str(uuid4())
    aw_dsn = dual_databases["aw"]
    with connect(aw_dsn) as conn:
        dst = conn.execute("SELECT current_database() AS db,current_user AS owner").fetchone()
        assert dst["db"].startswith("aw_test_") and dst["owner"] == dst["db"]
        assert conn.execute("SELECT to_regclass('nf_tasks') AS t").fetchone()["t"] is None
    with kernel.db.uow(write=False) as repo:
        assert repo.conn.exec_driver_sql("SELECT current_user").scalar_one() != dst["owner"]
        assert (
            repo.conn.exec_driver_sql("SELECT to_regclass('aw_organizations')").scalar_one() is None
        )
    maintainer = "memory" if preset == "Long-form" else "clerk"
    reviewer = "stats" if preset == "Research-Audit" else "reviewer"
    review_mode = "STATISTICS" if preset == "Research-Audit" else "PROSE"
    state = {"maintainer": maintainer, "reviewer": reviewer, "review_mode": review_mode, "preset": preset, "organization": org, "nf_instance_id": instance}
    state["source"] = {
        "NovelFactory": source_identity(
            NF, ["src", "migrations", "tests/integration", "pyproject.toml"]
        ),
        "Workshop": source_identity(AW, ["src", "migrations", "integration", "pyproject.toml"]),
        "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    evidence = tmp_path / "accepted-scene.json"
    monkeypatch.setenv("NF_P5_BANQUET_EVIDENCE", str(evidence))

    story = {}

    def prepare(world, budget, ids):
        story.update(world)
        owner, wr = world["principal"], world["world_ref"]
        ticket = install(
            kernel,
            owner,
            nf_instance_id=instance,
            org_id=org,
            work_id=wr["work_id"],
            branch_id=wr["branch_id"],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        installation = state["installation"] = ticket["installation_id"]
        with connect(aw_dsn) as conn:
            conn.execute(
                "INSERT INTO aw_installations(installation_id,nf_instance_id) VALUES (%s,%s)",
                (installation, instance),
            )
            bootstrap(conn, org, preset)
            conn.execute("INSERT INTO aw_budget_accounts VALUES (%s,%s,%s)", (org, "2", "USD"))
        path = private_file(
            tmp_path / "nf-delegate.json",
            {
                "database_url": kernel_env["url"],
                "object_root": str(kernel_env["store"].root),
                "installation_id": installation,
                "token": ticket["token"],
            },
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "novel_factory.interfaces.delegation_stdio", "--binding", path],
            env={"PYTHONPATH": str(NF / "src")},
        )
        policy = {
            **{k: wr[k] for k in ("work_id", "branch_id")},
            "nf_instance_id": instance,
            "role_id": "evidence_maintainer",
            "task_template_id": "summary.v1",
            "scope_ref": "scene.summary.1",
            "actor_binding_ref": None,
            "grant_epoch": 1,
            "generation": 0,
            "currency": "USD",
            "max_amount": "0.10",
            "token_limit": 100000,
        }
        with backend() as (http, requests, _):
            gateway = AgentGateway(kernel, {"workshop-fixture": http})
            supervisor = RuntimeSupervisor(kernel, gateway)
            frozen = RuntimeTaskService(kernel, gateway).define(
                owner,
                wr,
                role_id="evidence_maintainer",
                task_kind="summary",
                story_time=point(30),
                subject_ids=[ids["warehouse"]],
                predicates=["located_in"],
                assignment="Check inventory for the banquet scene.",
                adapter_id="workshop-fixture",
                max_amount="0.10",
                token_budget=100000,
            )
            provision_slot(
                supervisor,
                owner,
                installation,
                scope_ref=policy["scope_ref"],
                task_template_id=policy["task_template_id"],
                frozen=frozen,
                budget_id=budget,
                reserve_amount="0.10",
                token_reservation=100000,
                accept_summary=True,
            )

            async def begin():
                mail = Mail(aw_dsn, org, installation, tmp_path)
                async with mail.stack:
                    rev = 1
                    m = await mail.message(
                        "author",
                        "manager",
                        "GOAL",
                        rev,
                        content="Verify the warehouse record before the second actor makes a choice.",
                    )
                    hires = [] if preset == "Long-form" else [(maintainer, "evidence_maintainer", "chief")]
                    if preset == "Minimal":
                        hires.extend(
                            [
                                ("review-lead", "review_lead", "manager"),
                                ("reviewer", "reviewer", "review-lead"),
                            ]
                        )
                    for emp, role, manager in hires:
                        result = await mail.call(
                            "manager",
                            "aw_hire",
                            {
                                "employee_id": emp,
                                "role_id": role,
                                "supervisor_id": manager,
                                "operation_id": "hire-" + emp,
                                "team_id": "production",
                                "expected_revision": rev,
                            },
                        )
                        rev = result["revision"]
                    state["org_revision"] = rev
                    m = await mail.message("manager", "script", "ASSIGN", rev, parent=m)
                    m = await mail.message("script", "chief", "PROPOSE_SCRIPT", rev, parent=m)
                    m = await mail.message("chief", "volume", "ASSIGN", rev, parent=m)
                    for actor in ("actor-1", "actor-2", "actor-3"):
                        await mail.message("volume", actor, "ASSIGN", rev, parent=m)
                    with connect(aw_dsn) as conn:
                        allow_slot(conn, org, installation, "inventory", maintainer, policy)
                    intent = await mail.call(
                        "chief",
                        "aw_delegate",
                        {
                            "operation_id": "verify-inventory",
                            "slot_id": "inventory",
                            "max_amount": "0.10",
                            "token_limit": 100000,
                        },
                    )
                    state["operation_id"] = intent["operation_id"]
                    assert intent["state"] == "REQUESTED"
                    state["production_mail"] = mail.history
                return await reconcile(
                    aw_dsn, installation, org, state["operation_id"], remote(params)
                )

            try:
                assert asyncio.run(begin()) == "BOUND"
            except ExceptionGroup as group:
                cause = group
                while isinstance(cause, ExceptionGroup) and len(cause.exceptions) == 1:
                    cause = cause.exceptions[0]
                raise cause from None
            with connect(aw_dsn) as conn:
                receipt = conn.execute(
                    "SELECT * FROM aw_remote_receipts WHERE org_id=%s", (org,)
                ).fetchone()
            delegated = str(receipt["task_id"])
            assert delegated == frozen.spec["task_id"] and not requests
            assert (
                supervisor.run_once(owner, wr["work_id"], wr["branch_id"])["task_id"] == delegated
            )
            supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
            assert kernel.tasks.get(delegated)["state"] == "ACCEPTED" and len(requests) == 1
            state["nf_delegation_receipt"] = str(receipt["receipt_ref"])
        return delegated

    run_banquet(kernel_env, tmp_path, prepare_prerequisite=prepare)
    scene = json.loads(evidence.read_text())
    (source_task,) = scene["external_prerequisites"]
    with kernel.db.uow(write=False) as repo:
        assert repo.one(
            "workflow_edges",
            {"task_id": scene["dependent_actor_task"], "predecessor_id": source_task},
        )
    owner, old_wr, new_wr = story["principal"], scene["base_world_ref"], scene["accepted_world_ref"]
    ids = story["ids"]
    # Two late-arriving consumers independently read the same old cut after the
    # canonical branch advanced. Track the moved actor and an unrelated resource.
    changed = create_bound_task(
        kernel, owner, old_wr, point(31), [ids["protagonist"]], ["located_in"]
    )
    unaffected = create_bound_task(kernel, owner, old_wr, point(31), [ids["sword"]], ["lifecycle"])
    observed_query(kernel, story, old_wr, changed, ids["protagonist"], "located_in", 31)
    observed_query(kernel, story, old_wr, unaffected, ids["sword"], "lifecycle", 31)
    with kernel.db.uow(write=False) as repo:
        with pytest.raises(DomainError) as invalid:
            check_task_dependencies(repo, changed["task_id"], changed["generation"])
        assert invalid.value.code == "DEPENDENCY_CONFLICT"
        check_task_dependencies(repo, unaffected["task_id"], unaffected["generation"])
        case = repo.conn.execute(
            sql("SELECT case_id FROM nf_conflict_cases WHERE task_id=:task LIMIT 1"),
            {"task": changed["task_id"]},
        ).scalar_one()
        plan = kernel.artifact(repo, scene["selected_plan_ref"], new_wr["work_id"])["payload"]
    resolution_service = ConflictResolutionService(kernel)
    resolution = resolution_service.resolve(owner, new_wr["work_id"], new_wr["branch_id"], str(case))
    assert resolution["outcome"] == "REVIEW_REQUIRED"
    independent = kernel.issue_grant(
        owner, new_wr["work_id"], role_id="control_operator", branch_ids=[new_wr["branch_id"]],
        capabilities=["ADMIN", "APPROVE", "ARTIFACT_WRITE"], planes=["WORLD"], max_story_time=point(60),
    )["principal"]
    decision = "DISCARD_CANDIDATE"
    signed = kernel.put_artifact(independent, new_wr["work_id"], {
        "case_id": str(case), "world_ref": new_wr, "decision": decision,
        "reason": "The late reader used the old actor location. Keep the NF-selected future window.",
    }, "EVIDENCE")
    def finish_review():
        return resolution_service.decide_review(
            independent, new_wr, str(case), decision=decision, evidence_ref=signed
        )
    resolved = finish_review()
    assert resolved["outcome"] == "REVIEW_DECIDED" and finish_review() == resolved
    with kernel.db.uow(write=False) as repo:
        original = kernel.artifact(repo, scene["initial_plan_ref"], new_wr["work_id"])["payload"]
        selection = repo.one("selections", {
            "work_id": new_wr["work_id"], "selection_kind": PlanningService.selection_key(plan["volume_id"])
        })["artifact_ref"]
        assert selection == scene["selected_plan_ref"] and selection != scene["initial_plan_ref"]
        changes = [n["node_id"] for o, n in zip(original["nodes"], plan["nodes"], strict=True) if o != n]
        retained = [n["node_id"] for o, n in zip(original["nodes"], plan["nodes"], strict=True) if o == n]
        assert len(changes) == 2 and retained
        assert repo.one("tasks", {"task_id": resolved["review_task_id"]})["state"] == "ACCEPTED"
        check_task_dependencies(repo, unaffected["task_id"], unaffected["generation"])
        assert kernel.head(repo, new_wr["work_id"], new_wr["branch_id"]) == new_wr
    state["selected_plan_repair"] = {
        "initial": scene["initial_plan_ref"], "selected": selection, "world_ref": new_wr,
        "change_point": scene["change_point_ref"], "window": scene["window_ref"],
        "revised_node_ids": changes, "retained_node_ids": retained,
    }
    state["s9"] = {
        "case_id": str(case),
        "affected_task": changed["task_id"],
        "independent_task": unaffected["task_id"],
        "strategy_receipt": resolution,
        "final_decision": resolved,
    }
    now = {
        **story,
        "world_ref": new_wr,
        "plan_ref": scene["selected_plan_ref"],
        "plan": plan,
        "narrative_id": str(uuid4()),
    }
    excerpt = "灯芯噼啪响了一声，门缝里的风掀起了纸角。"
    prose = draft_for(
        kernel, now, excerpt, interval={"start": point(31), "end": point(32), "bounds": "[)"}
    )
    reading = extract(kernel, now, prose, [assertion(excerpt)])
    narrative = NarrativeService(kernel)
    alignment = narrative.reconcile(
        owner, prose["draft_ref"], reading["extraction_ref"], new_wr, "match-final-scene"
    )
    assert alignment["status"] == "CLEAR"
    accepted = narrative.accept_narrative(
        owner, prose["draft_ref"], alignment["reconciliation_ref"], new_wr, "accept-final-scene"
    )
    text_ref = reading["delivery"]["text_ref"]
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, new_wr["work_id"], new_wr["branch_id"]) == new_wr
    state["prose"] = {
        "source": "deterministic engineering fixture",
        "accepted": accepted,
        "draft_ref": prose["draft_ref"],
        "text_ref": text_ref,
        "reconciliation": alignment["reconciliation_ref"],
    }
    with connect(aw_dsn) as conn:
        subject(conn, org, "script", "production")
        subject(conn, org, reviewer, "separate-review")
        register_artifact(
            conn,
            org,
            text_ref["artifact_id"],
            text_ref["artifact_revision_id"],
            text_ref["sha256"],
            "organization",
            accepted["artifact_revision_id"],
            ["script"],
            [alignment["reconciliation_ref"]["artifact_revision_id"]],
            {},
        )

    async def finish():
        installation = state["installation"]
        args = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "novel_factory.interfaces.delegation_stdio",
                "--binding",
                str(tmp_path / "nf-delegate.json"),
            ],
            env={"PYTHONPATH": str(NF / "src")},
        )
        assert (
            await reconcile(aw_dsn, installation, org, state["operation_id"], remote(args))
            == "BOUND"
        )
        with connect(aw_dsn) as conn:
            attempt = conn.execute(
                "SELECT attempt_id FROM aw_cost_observations WHERE org_id=%s AND state='UNKNOWN'",
                (org,),
            ).fetchone()
            assert (
                attempt
                and conn.execute(
                    "SELECT status FROM aw_budget_holds WHERE org_id=%s", (org,)
                ).fetchone()["status"]
                == "HELD"
            )
        kernel.tasks.settle_call(
            str(attempt["attempt_id"]),
            state="SUCCEEDED",
            actual_amount="0.02",
            actual_tokens=18,
            provider_receipt="synthetic-invoice-" + org,
        )
        state["unknown_held_then_settled_attempt"] = str(attempt["attempt_id"])
        mail = Mail(aw_dsn, org, installation, tmp_path)
        async with mail.stack:
            review = await mail.call(
                "review-lead",
                "aw_review_request",
                {
                    "operation_id": "independent-review",
                    "artifact_ref": text_ref["artifact_id"],
                    "artifact_revision": text_ref["artifact_revision_id"],
                    "mode": review_mode,
                    "risk_reason": "MATERIAL_CHANGE",
                    "ttl_seconds": 600,
                    "reviewer_id": reviewer,
                },
            )
            assert review["state"] == "PENDING"
            assigned = await mail.call(
                reviewer, "aw_review_get", {"review_id": review["review_id"]}
            )
            assert assigned["content_sha"] == text_ref["sha256"]
            assert assigned["evidence_refs"] == [
                alignment["reconciliation_ref"]["artifact_revision_id"]
            ]
            state["reviewed_frozen_source"] = {
                "artifact_revision": assigned["artifact_revision"],
                "content_sha": assigned["content_sha"],
            }
            task_view = await mail.call(
                "author", "aw_progress_get", {"operation_id": state["operation_id"]}
            )
            assert task_view["task_state"] == "ACCEPTED" and task_view["hold"] == "HELD"
            state["author_task_progress"] = task_view
            decision = await mail.call(
                reviewer,
                "aw_review_decide",
                {
                    "operation_id": "review-result",
                    "review_id": review["review_id"],
                    "outcome": "APPROVED",
                    "reason": "EVIDENCE",
                    "evidence_ref": alignment["reconciliation_ref"]["artifact_revision_id"],
                },
            )
            assert decision["state"] == "APPROVED"
            state["independent_workshop_review"] = {
                **decision,
                "scope": "Programmed account exercise; no human literary rating.",
            }
            await mail.message(
                "manager",
                "author",
                "REPORT",
                state["org_revision"],
                content=json.dumps(
                    {
                        "accepted_text_ref": text_ref,
                        "approved_plan_revision": scene["selected_plan_ref"][
                            "artifact_revision_id"
                        ],
                        "world_revision": scene["accepted_world_ref"]["world_revision_id"],
                    }
                ),
            )
            state["author_retrieval"] = mail.history
        assert (
            await reconcile(aw_dsn, installation, org, state["operation_id"], remote(args))
            == "SETTLED"
        )

    asyncio.run(finish())
    with connect(aw_dsn) as conn:
        hold = conn.execute(
            "SELECT status,settled_amount FROM aw_budget_holds WHERE org_id=%s", (org,)
        ).fetchone()
        obs = conn.execute(
            "SELECT state,actual_tokens FROM aw_cost_observations WHERE org_id=%s", (org,)
        ).fetchall()
        assert hold["status"] == "SETTLED" and any(
            o["state"] == "SUCCEEDED" and o["actual_tokens"] == 18 for o in obs
        )
    state.update(
        accepted_scene=scene,
        delegated_task_id=source_task,
        cost_observations=obs,
        hold={"status": hold["status"], "settled_fixture_amount": str(hold["settled_amount"])},
        accounting_scope="Deterministic loopback model usage; no external vendor or paid spend.",
    )
    directory = Path(os.environ["AW_ACCEPTANCE_OUTPUT"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ("scene-" + preset.lower() + ".json")).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    )
