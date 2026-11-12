import pytest
from adaptive_helpers import setup_adaptive_case

from novel_factory.application.adaptive_instructions import (
    AdaptiveInstructionService,
    issuer_for_candidate,
    load_instruction,
)
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_signed_failures_calibrated_success_controls_and_held_out_texts(
    kernel_env, tmp_path, monkeypatch
):
    k = kernel_env["kernel"]
    s = setup_adaptive_case(k, tmp_path, monkeypatch)
    p, wr, args = s["admin"], s["wr"], s["args"]
    svc = AdaptiveInstructionService(k)
    with k.db.uow(write=False) as repo:
        head = k.head(repo, wr["work_id"], wr["branch_id"])
        rev_before = k.revision(repo, head)["interpretation_ref"]
    original = svc.propose(p, wr, **args)
    assert svc.propose(p, wr, **args) == original
    candidate = original["candidate_ref"]
    live = svc.refresh_sources(p, wr, candidate)
    with k.db.uow(write=False) as repo:
        loaded = svc.load_evaluation(repo, p, wr, candidate, live=live)
        validate_contract("ExperimentFreeze", loaded["freeze"], "harness")
        record = validate_contract("InstructionCandidate", loaded["candidate"], "harness")
        assert (
            record["rollback_ref"] == original["baseline_ref"]
            and record["freeze_ref"] == original["design_ref"]
        )
        assert (
            loaded["freeze"]["model_config_sha256"] and loaded["design"]["release"]["module_sha256"]
        )
        assert loaded["freeze"]["source_commit"] == loaded["design"]["release"]["source_commit"]
        assert loaded["baseline"]["blocks"] == [] and len(loaded["treatment"]["blocks"]) == 1
        assert len(loaded["allocation"]) == 2 and all(
            case["held_out"] for case in loaded["samples"]
        )
        assert all(case["independent_label"] for case in loaded["samples"])
        assert [len(s["expected_findings"]) for s in loaded["samples"]] == [2, 0]
        assert issuer_for_candidate(k, repo, wr, candidate) == p
        text = load_instruction(k, repo, wr, record["instruction_ref"])
        assert (
            text["review_checks"] == ["MECHANICAL_CADENCE"] and "NF_REVIEW_CHECKS:" in text["body"]
        )
        assert loaded["design"]["producers"][0] == record["candidate"]["created_by"]
        attributed = {
            a["role_id"] for cause in loaded["design"]["causes"] for a in cause["attributions"]
        }
        assert {"writer", "literary_editor", "reviewer"} <= attributed
        assert not repo.many("call_attempts", {"task_id": args["task_id"]})
        assert (
            k.head(repo, wr["work_id"], wr["branch_id"]) == head
            and k.revision(repo, head)["interpretation_ref"] == rev_before
        )
    with pytest.raises(DomainError):
        svc.propose(
            p, wr, **{**args, "failure_report_refs": [s["success_ref"]], "success_report_refs": []}
        )
    with pytest.raises(DomainError):
        svc.propose(s["source"].principal, wr, **args)
    sibling = k.fork(p, wr, "CANDIDATE", "unrelated-branch")
    with pytest.raises(DomainError):
        svc.propose(p, sibling, **args)
    with pytest.raises(DomainError):
        svc.propose(
            p,
            wr,
            **{**args, "change_kind": "CONTEXT_RECIPE", "material_order": ["unauthorized_secrets"]},
        )
    with pytest.raises(DomainError):
        svc.propose(p, wr, **{**args, "instructions": "Different content with old idempotency key"})
    key = tmp_path / "independence" / "private-key"
    protected = key.read_bytes()
    key.write_bytes(b"x" * len(protected))
    with pytest.raises(DomainError):
        svc.refresh_sources(p, wr, candidate)


def test_writer_and_context_proposals_use_own_dispatched_packet_and_never_replay_it_as_an_actor(
    kernel_env, tmp_path, monkeypatch
):
    import json
    from dataclasses import replace

    from test_harness_context import EnvelopeFixture
    from test_harness_review_workflow import LINE, _args, _repeat, _reviewer
    from test_release import accept_draft, setup_batch, write_scene

    from novel_factory.application.agent_gateway import AgentGateway
    from novel_factory.application.creative_context_store import load_snapshot
    from novel_factory.application.creative_review import CreativeReviewService
    from novel_factory.application.narrative import NarrativeService
    from novel_factory.application.planning import PlanningService
    from novel_factory.application.release import ReleaseService
    from novel_factory.application.runtime_tasks import RuntimeTaskService
    from novel_factory.fixtures import point
    from novel_factory.infrastructure.creative_config import CreativeHarnessConfig

    k = kernel_env["kernel"]
    f = setup_batch(k, count=1)
    wr, owner = f["world_ref"], f["principal"]
    flags = CreativeHarnessConfig(enabled=True, adaptive_instructions=True)
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(flags.as_dict()))

    class Writer(EnvelopeFixture):
        def generate(self, request):
            result = super().generate(request)
            value = json.loads(result.text)
            value["payload"]["text"] = LINE * 3
            return replace(result, text=json.dumps(value))

    adapter = Writer()
    native = NarrativeService(k)
    gateway = AgentGateway(k, {"fixture": adapter}, material_reader=native.read_agent_material)
    runtime = RuntimeTaskService(k, gateway)
    common = {
        "story_time": point(1, 1),
        "subject_ids": [f["ids"]["sword"]],
        "predicates": ["possessed_by"],
        "assignment": "写下渡口等船的一幕。",
        "adapter_id": "fixture",
    }
    writer = runtime.prepare(
        owner,
        wr,
        role_id="writer",
        task_kind="scene_draft",
        input_artifacts=[f["plan_ref"], f["batch_ref"]],
        material_refs=[f["scene_refs"][0]],
        **common,
    )
    tid, gen = writer.task["task_id"], writer.task["generation"]
    PlanningService(k).bind_task_for_controller(
        owner, tid, gen, f["plan_ref"], f["scenes"][0]["plan_node_ids"], "trace-plan"
    )
    ReleaseService(k).bind_writer_task(
        writer.principal, tid, gen, f["scene_refs"][0], [], "trace-writer"
    )
    budget = k.tasks.create_budget(wr["work_id"], "2")
    request = {
        "schema_version": "2.1",
        "task_id": tid,
        "generation": gen,
        "worker_id": writer.worker_id,
        "call_key": "snapshot",
        "input_ref": writer.agent_input["input_ref"],
        "budget_id": budget,
        "reserve_amount": "0.1",
        "token_reservation": 100000,
    }
    result = gateway.execute(writer.principal, request)
    assert result["validation_status"] == "VALIDATED_CANDIDATE"
    draft = native.store_draft(
        writer.principal,
        tid,
        gen,
        f["scene_refs"][0],
        LINE * 3,
        "from-output",
        agent_output_ref=result["output_ref"],
    )
    accepted = accept_draft(
        k,
        f,
        {"writer": writer.principal, "task": writer.task, "draft_ref": draft, "text": LINE * 3},
    )
    reviewer, args, read = _reviewer(k, f, accepted, tmp_path, monkeypatch)
    report = CreativeReviewService(k).review(
        *_args(reviewer), **{**args, "observations": [_repeat(read)]}
    )["report_ref"]
    service = AdaptiveInstructionService(k)
    proposal_args = {
        "failure_report_refs": [report],
        "success_report_refs": [],
        "order_seed": 67,
        "repetitions": 1,
        "quota": {**args["quota"], "budget_id": budget, "max_amount": "0.1"},
    }
    for kind in ("ROLE_INSTRUCTIONS", "SKILL_INSTRUCTIONS", "CONTEXT_RECIPE"):
        extra = {"material_order": ["scene", "facts"]} if kind == "CONTEXT_RECIPE" else {}
        r = service.propose(
            owner,
            wr,
            task_id=tid,
            generation=gen,
            change_kind=kind,
            idempotency_key=kind,
            **extra,
            **proposal_args,
        )
        witness = service.refresh_sources(owner, wr, r["candidate_ref"])
        with k.db.uow(write=False) as repo:
            data = service.load_evaluation(repo, owner, wr, r["candidate_ref"], live=witness)
        (sample,) = data["samples"]
        assert sample["role_model_input"] == json.loads(adapter.calls[0].messages[1]["content"])
        assert sample["creative_materials"]["scene"] and sample["section_source_refs"]["scene"] == [
            f["scene_refs"][0]
        ]
        assert data["design"]["usage"]["model_attempts_added"] == 0
    actor = runtime.prepare(
        owner,
        wr,
        role_id="character_actor",
        task_kind="reaction",
        actor_id=f["ids"]["protagonist"],
        **common,
    )
    proposed = service.propose(
        owner,
        wr,
        task_id=actor.task["task_id"],
        generation=actor.task["generation"],
        change_kind="CONTEXT_RECIPE",
        idempotency_key="actor-order",
        **proposal_args,
    )
    witness = service.refresh_sources(owner, wr, proposed["candidate_ref"])
    with k.db.uow(write=False) as repo:
        data = service.load_evaluation(repo, owner, wr, proposed["candidate_ref"], live=witness)
    assert (
        data["samples"][0]["role_model_input"] is None
        and data["samples"][0]["creative_materials"] is None
    )
    assert LINE not in data["treatment"]["blocks"][0]["body"]
    assert len(adapter.calls) == 1
    # The historical exception cannot authorize another production dispatch.
    with k.db.uow(write=False) as repo, pytest.raises(DomainError) as denied:
        origin = repo.one("tasks", {"task_id": tid})
        load_snapshot(k, repo, origin, request["input_ref"], required=True)
    assert denied.value.code == "TASK_REBIND_REQUIRED"

    # Even an unaccepted successor changes the source manuscript's provenance.
    write_scene(k, f, 0, "空船载着漂流的灯靠了岸。", prior_draft_ref=accepted["draft_ref"])
    witness = service.refresh_sources(owner, wr, r["candidate_ref"])
    with k.db.uow(write=False) as repo, pytest.raises(DomainError):
        service.load_evaluation(repo, owner, wr, r["candidate_ref"], live=witness)
