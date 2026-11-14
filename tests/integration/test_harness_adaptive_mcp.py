"""A signed independent release from live stdio sessions, through task dispatch and rollback."""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from adaptive_helpers import setup_adaptive_case
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from test_harness_context import EnvelopeFixture
from test_harness_dynamic_mcp import controller_port
from test_p5_mcp import config, error, exercise
from test_review_independence import enroll_native_case

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.creative_review import CreativeReviewService
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.review_independence import ReviewIndependenceService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.infrastructure.private_files import create_private_file
from novel_factory.interfaces.review_independence import enroll

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


class ReviewTransport(EnvelopeFixture):
    def generate(self, request):
        response = super().generate(request)
        envelope = json.loads(response.text)
        envelope["payload"] = {
            "summary": "本地传输测试。",
            "issues": [],
            "counterinterpretations": [],
            "evidence_refs": [],
        }
        return replace(response, text=json.dumps(envelope, ensure_ascii=False))


def official_state(kernel, wr):
    with kernel.db.uow(write=False) as repo:
        scope = {"work_id": wr["work_id"]}
        return (
            kernel.head(repo, wr["work_id"]),
            repo.one("works", scope)["interpretation_epoch"],
            repo.many("events", scope, order=["event_version_id"]),
            [
                (r["kind"], r["artifact_revision_id"], r["sha256"])
                for r in repo.many(
                    "artifacts",
                    {
                        **scope,
                        "kind": ["NARRATIVE_ACCEPTANCE", "RELEASE_RECORD", "RELEASE_MANIFEST"],
                    },
                    order=["artifact_revision_id"],
                )
            ],
        )


def signed_case(tmp_path, case, deadline):
    paths = []
    for participant in [*case["producers"], case["reviewer"], case["publisher"]]:
        pid = participant["principal_id"]
        path = tmp_path / ("idp-promotion-" + pid)
        if path in paths:
            continue
        record = {
            "kind": "VERIFIED_SUBJECT_GROUP",
            "principal_id": pid,
            "subject_group": "test-team-" + pid,
            "source_record_id": "integration-registrar-" + pid,
            "verified_by": "offline integration registrar",
            **{key: case["world_ref"][key] for key in ("work_id", "branch_id")},
        }
        with os.fdopen(create_private_file(path), "w", encoding="utf-8") as target:
            json.dump(record, target)
        paths.append(path)
    return enroll(case, paths, deadline)


async def open_port(stack, tmp_path, kernel_env, token, binding, label):
    port = config(tmp_path / label, kernel_env, token, binding)
    names = ("NF_CREATIVE_HARNESS_CONFIG", "NF_REVIEW_NATIVE_ATTESTATIONS", "NF_REVIEW_NATIVE_KEY")
    port.env = {k: os.environ[k] for k in names}
    port.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    streams = await stack.enter_async_context(stdio_client(port))
    session = await stack.enter_async_context(ClientSession(*streams))
    hello = await session.initialize()
    assert hello.serverInfo.name == "NovelFactory"
    return session


async def invoke(session, name, args):
    result = await session.call_tool("nf_harness_instruction_" + name, args)
    assert not result.isError, (name, result.content)
    return result.structuredContent


def test_independent_release_freezes_prompt_for_new_tasks_and_restores_exact_prior_selection(
    kernel_env, tmp_path, monkeypatch, record_property
):
    kernel = kernel_env["kernel"]
    setup = setup_adaptive_case(kernel, tmp_path, monkeypatch)
    wr, admin = setup["wr"], setup["admin"]
    bound = {"mode": "creative_controller", **{k: wr[k] for k in ("work_id", "branch_id")}}
    evaluator, publisher = [
        kernel.issue_grant(
            admin,
            wr["work_id"],
            role_id="control_operator",
            branch_ids=[wr["branch_id"]],
            capabilities=["READ", "ADMIN", "APPROVE"],
        )
        for _ in range(2)
    ]
    adapter = ReviewTransport()
    gateway = AgentGateway(kernel, {"fixture": adapter})
    tasks = RuntimeTaskService(kernel, gateway)
    before = official_state(kernel, wr)

    def prepare(refs=()):
        return tasks.prepare(
            admin,
            wr,
            role_id="reviewer",
            task_kind="literary",
            story_time=point(3),
            subject_ids=[setup["world"]["ids"]["sword"]],
            predicates=["lifecycle"],
            assignment="比较行文中的重复及其用意。",
            adapter_id="fixture",
            input_artifacts=list(refs),
        )

    def read_input(job):
        with kernel.db.uow(write=False) as repo:
            return kernel.artifact(
                repo, job.agent_input["input_ref"], wr["work_id"], kind="AGENT_INPUT"
            )["payload"]

    def submit_live_review(selection_ref):
        _, _, history_args, manuscript = setup["records"][2]
        editorial = history_args["binding"]
        reader = prepare(editorial["ordered_draft_refs"] + editorial["reconciliation_refs"])
        tid, generation = reader.task["task_id"], reader.task["generation"]
        draft = manuscript["draft_ref"]
        NarrativeService(kernel).bind_reader_task(
            admin, tid, generation, [draft], "read-released-method"
        )
        case = {"manuscript_ref": draft, "reviewer_task_id": tid, "reviewer_generation": generation}
        enroll_native_case(kernel, tmp_path, monkeypatch, **case, controller=admin)
        proof = ReviewIndependenceService(kernel).issue(admin, **case)
        service = CreativeReviewService(kernel)
        reading = service.read(
            reader.principal,
            tid,
            generation,
            draft_ref=draft,
            worker_id=reader.worker_id,
            idempotency_key="read-release",
        )
        n = manuscript["text"].index("。") + 1
        observation = {
            "pattern": "MECHANICAL_CADENCE",
            "start": 2 * n,
            "end": 3 * n,
            "disposition": "PRESERVE",
            "reason": "第一段三次等待，保留送别的节奏。",
            "counterinterpretation": "重复也可能显得多余。",
            "narrative_function": "保留说话人踌躇的时间。",
            "requested_change": None,
            "related_spans": [],
        }
        submitted = service.review(
            reader.principal,
            tid,
            generation,
            **{
                **history_args,
                "coverage_refs": [reading["coverage_ref"]],
                "independence_ref": proof["independence_ref"],
                "observations": [observation],
                "worker_id": reader.worker_id,
                "idempotency_key": "read-with-release",
            },
        )
        assert len(submitted["report"]["findings"]) == 1
        assert any(f["text_span"]["start"] > 3 * n for f in submitted["report"]["findings"])
        with kernel.db.uow(write=False) as repo:
            details = kernel.artifact(repo, submitted["details_ref"], wr["work_id"])["payload"]
            assert details["reader_observations"] == [observation]
            assert (
                details["source_versions"]["reviewer"]["adaptive_instruction_ref"] == selection_ref
            )
        assert read_input(reader)["adaptive_instruction_ref"] == selection_ref

    async def flow():
        async with AsyncExitStack() as stack:
            origin = await open_port(
                stack, tmp_path, kernel_env, setup["world"]["token"], bound, "proposer"
            )
            assessor = await open_port(
                stack, tmp_path, kernel_env, evaluator["token"], bound, "evaluator"
            )
            editor = await open_port(
                stack, tmp_path, kernel_env, publisher["token"], bound, "publisher"
            )
            names = {t.name for t in (await origin.list_tools()).tools}
            assert (
                "nf_harness_instruction_propose" in names
                and "nf_harness_instruction_review" not in names
            )
            assert "nf_harness_instruction_policy" not in names
            proposal = await invoke(origin, "propose", {"world_ref": wr, **setup["args"]})
            candidate = proposal["candidate_ref"]
            sibling = kernel.fork(admin, wr, "CANDIDATE", "other-branch-adaptive")
            await error(
                assessor,
                "nf_harness_instruction_evaluate",
                {"world_ref": sibling, "candidate_ref": candidate, "idempotency_key": "misbound"},
                "VIEW_DENIED",
            )
            evaluated = await invoke(
                assessor,
                "evaluate",
                {"world_ref": wr, "candidate_ref": candidate, "idempotency_key": "engineering-a-b"},
            )
            assert evaluated["eligible"], evaluated
            refs = {
                "candidate_ref": candidate,
                **{key: evaluated[key] for key in ("engineering_report_ref", "run_manifest_ref")},
            }
            reader = prepare(refs.values())
            task = {k: reader.task[k] for k in ("task_id", "generation")}
            reader_port = await open_port(
                stack,
                tmp_path,
                kernel_env,
                reader.private_token,
                {**task, "worker_id": reader.worker_id},
                "independent-reader",
            )
            names = {t.name for t in (await reader_port.list_tools()).tools}
            assert (
                "nf_harness_instruction_review" in names
                and "nf_harness_instruction_decide" not in names
            )
            await error(reader_port, "nf_harness_instruction_decide", {}, "VIEW_DENIED")
            auth = {**refs, "publisher_grant_id": publisher["principal"].grant_id}
            case = await invoke(
                origin,
                "case",
                {
                    **auth,
                    "world_ref": wr,
                    "reviewer_task_id": task["task_id"],
                    "reviewer_generation": task["generation"],
                },
            )
            now = datetime.now(UTC)
            deadline = (now + timedelta(minutes=15)).isoformat()
            case_path = signed_case(tmp_path, case, (now + timedelta(minutes=20)).isoformat())
            evidence = await invoke(reader_port, "evidence", refs)
            assert evidence["eligible"] and len(evidence["cases"]) == 4
            assert all("text" not in entry for entry in evidence["cases"])
            opinion_args = {
                **auth,
                "verdict": "APPROVE",
                "valid_until": deadline,
                "idempotency_key": "review-outcomes",
                "reason": "独立核对四条轨迹。补充检查在保留样本上发现第二处重复，负例原样保留。",
            }
            review = await invoke(reader_port, "review", opinion_args)
            decision_args = {
                **refs,
                "world_ref": wr,
                "review_ref": review["review_ref"],
                "action": "APPROVE",
                "reason": "只在同一角色与技能范围内发布该检查。",
                "expected_epoch": 0,
                "valid_until": deadline,
                "idempotency_key": "authorize-release",
            }
            await error(
                editor,
                "nf_harness_instruction_decide",
                {**decision_args, "route": "POLICY_AUTOMATIC"},
                "PROMOTION_NOT_AUTHORIZED",
            )
            previous = prepare()
            decision = await invoke(editor, "decide", decision_args)
            assert decision["epoch"] == 1
            assert await invoke(editor, "decide", decision_args) == decision
            submit_live_review(decision["selection_ref"])
            selected = prepare()
            assert read_input(selected)["adaptive_instruction_ref"] == decision["selection_ref"]
            assert "adaptive_instruction_ref" not in read_input(previous)
            request = {
                "schema_version": "2.1",
                "task_id": selected.task["task_id"],
                "generation": selected.task["generation"],
                "worker_id": selected.worker_id,
                "input_ref": selected.agent_input["input_ref"],
                "budget_id": kernel.tasks.create_budget(wr["work_id"], "1"),
                "call_key": "adaptive-rollout",
                "reserve_amount": "0.2",
                "token_reservation": 150000,
            }
            result = gateway.execute(selected.principal, request)
            assert result["validation_status"] == "VALIDATED_CANDIDATE" and len(adapter.calls) == 1
            with kernel.db.uow(write=False) as repo:
                instruction = kernel.artifact(repo, proposal["instruction_ref"], wr["work_id"])[
                    "payload"
                ]
                audit = kernel.artifact(repo, result["evidence_ref"], wr["work_id"])["payload"]
                assert audit["adaptive_instruction_ref"] == decision["selection_ref"]
            assert instruction["body"] in adapter.calls[0].messages[0]["content"]
            observed = await invoke(
                editor,
                "monitor",
                {
                    "world_ref": wr,
                    "decision_ref": decision["decision_ref"],
                    "task_id": request["task_id"],
                    "generation": request["generation"],
                    "idempotency_key": "observe-rollout",
                },
            )
            assert [(a["attempt_id"], a["actual_tokens"]) for a in observed["attempts"]] == [
                (result["attempt_id"], 65)
            ]
            rolled = await invoke(
                editor,
                "rollback",
                {
                    "world_ref": wr,
                    "decision_ref": decision["decision_ref"],
                    "expected_epoch": 1,
                    "reason": "结束分批试用，恢复先前版本。",
                    "idempotency_key": "rollback-release",
                },
            )
            assert rolled["epoch"] == 2 and rolled["selection_ref"] is None
            assert "adaptive_instruction_ref" not in read_input(prepare())
            assert read_input(selected)["adaptive_instruction_ref"] == decision["selection_ref"]
            assert (
                gateway.execute(selected.principal, request) == result and len(adapter.calls) == 1
            )
            case_path.unlink()
            await error(
                reader_port,
                "nf_harness_instruction_review",
                opinion_args,
                "INDEPENDENT_REVIEW_REQUIRED",
            )
            record_property(
                "offline_rollout",
                json.dumps(
                    {
                        "candidate_ref": candidate,
                        "quality": evidence["quality"],
                        "arms": evidence["arm_totals"],
                        "attempt": result["attempt_id"],
                        "epochs": [0, 1, 2],
                        "distinct_identities": len(
                            {
                                p["principal_id"]
                                for p in [*case["producers"], case["reviewer"], case["publisher"]]
                            }
                        ),
                        "commercial_calls": 0,
                    },
                    ensure_ascii=False,
                ),
            )

    asyncio.run(flow())
    assert official_state(kernel, wr) == before


@pytest.mark.parametrize("flags", [None, CreativeHarnessConfig(enabled=True, dynamic_agents=True)])
def test_disabled_optimization_exposes_no_operations_and_creates_no_attempts(
    kernel_env, tmp_path, flags
):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    port = controller_port(tmp_path / "disabled", kernel_env, world, flags)
    with kernel.db.uow(write=False) as repo:
        prior_attempts = {r["attempt_id"] for r in repo.many("call_attempts", {})}

    async def disabled(session):
        names = {t.name for t in (await session.list_tools()).tools}
        assert not any(n.startswith("nf_harness_instruction_") for n in names)
        for operation in ("propose", "evaluate", "case", "decide", "policy", "monitor", "rollback"):
            await error(session, "nf_harness_instruction_" + operation, {}, "VIEW_DENIED")

    asyncio.run(exercise(port, disabled))
    with kernel.db.uow(write=False) as repo:
        assert {r["attempt_id"] for r in repo.many("call_attempts", {})} == prior_attempts
        assert not repo.many(
            "artifacts",
            {"work_id": world["world_ref"]["work_id"], "kind": "CREATIVE_INSTRUCTION_CANDIDATE"},
        )
