"""Reorder the actual S1 materials delivered to independent, signed scene drafts."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from editorial_helpers import binding_for
from test_adaptive_promotion import enroll_case, release_inputs
from test_continuity import entry, qualify, request, reviewer, volume
from test_harness_context import EnvelopeFixture
from test_harness_review_workflow import LINE, _args, _repeat, _reviewer
from test_release import accept_draft, setup_batch, write_scene
from test_review_independence import enroll_native_case

from novel_factory.application.adaptive_evaluation import AdaptiveEvaluationService
from novel_factory.application.adaptive_instructions import AdaptiveInstructionService
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.continuity import ContinuityService
from novel_factory.application.creative_review import CreativeReviewService
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.release import ReleaseService
from novel_factory.application.review_independence import ReviewIndependenceService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import point
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _deliver(k, world, index, previous, manuscript, budget):
    class Writer(EnvelopeFixture):
        def generate(self, request):
            original = super().generate(request)
            value = json.loads(original.text)
            value["payload"]["text"] = manuscript
            return replace(original, text=json.dumps(value, ensure_ascii=False))

    controller, wr = world["principal"], world["world_ref"]
    reader = NarrativeService(k)
    adapter = Writer()
    gateway = AgentGateway(k, {"fixture": adapter}, material_reader=reader.read_agent_material)
    scene = world["scene_refs"][index]
    job = RuntimeTaskService(k, gateway).prepare(
        controller,
        wr,
        role_id="writer",
        task_kind="scene_draft",
        story_time=point(1, 1),
        subject_ids=[world["ids"]["sword"]],
        predicates=["possessed_by"],
        assignment="续写前一场中留下的动作。",
        adapter_id="fixture",
        input_artifacts=[world["plan_ref"], world["batch_ref"], scene],
        material_refs=[scene, previous["draft_ref"], world["continuity_ref"]],
    )
    owner, tid, gen = _args(job)
    PlanningService(k).bind_task_for_controller(
        controller, tid, gen, world["plan_ref"], world["scenes"][index]["plan_node_ids"], "p:" + tid
    )
    ReleaseService(k).bind_writer_task(owner, tid, gen, scene, [], "w:" + tid)
    reader.bind_reader_task(controller, tid, gen, [previous["draft_ref"]], "r:" + tid)
    req = {
        "schema_version": "2.1",
        "task_id": tid,
        "generation": gen,
        "worker_id": job.worker_id,
        "input_ref": job.agent_input["input_ref"],
        "budget_id": budget,
        "call_key": "write:" + tid,
        "reserve_amount": "0.2",
        "token_reservation": 100000,
    }
    receipt = gateway.execute(owner, req)
    assert receipt["validation_status"] == "VALIDATED_CANDIDATE"
    assert len(adapter.calls) == 1
    ref = reader.store_draft(
        owner,
        tid,
        gen,
        scene,
        manuscript,
        "draft:" + tid,
        handoff_refs=[],
        agent_output_ref=receipt["output_ref"],
    )
    accepted = accept_draft(
        k, world, {"writer": owner, "task": job.task, "text": manuscript, "draft_ref": ref}
    )
    return job, accepted, adapter.calls[0]


def test_signed_writer_recipe_and_freeform_readiness(kernel_env, tmp_path, monkeypatch):
    k = kernel_env["kernel"]
    flags = CreativeHarnessConfig(enabled=True, adaptive_instructions=True)
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(flags.as_dict()))
    seed, released, _ = volume(k)
    wr, admin = seed["world_ref"], seed["principal"]
    continuity = ContinuityService(k)
    publish, _, _ = qualify(
        continuity,
        admin,
        reviewer(k, seed),
        wr["work_id"],
        request(seed, [released], [entry(seed, released, 1)]),
    )
    continuity_ref = continuity.publish(admin, wr["work_id"], publish)["snapshot_ref"]
    w = {**setup_batch(k, count=5, fixture=seed), "continuity_ref": continuity_ref}
    admin, wr = w["principal"], w["world_ref"]
    budget = k.tasks.create_budget(wr["work_id"], "5")
    svc = CreativeReviewService(k)
    training = accept_draft(k, w, write_scene(k, w, 0, LINE * 3))
    job, args, read = _reviewer(k, w, training, tmp_path, monkeypatch)
    historical = svc.review(*_args(job), **{**args, "observations": [_repeat(read)]})
    anchor = "江边响起最后一声汽笛，她把伞藏在身后。"
    prior = accept_draft(k, w, write_scene(k, w, 1, anchor))
    source, current, before = _deliver(k, w, 2, prior, anchor + "\n秦生低下头，拎起放错的包。", budget)
    raw = json.loads(before.messages[1]["content"])["creative_materials"]
    assert sorted(raw).index("prose") > 0 and raw["prose"] == [anchor]
    b = binding_for(w, [prior, current])
    job, args, read = _reviewer(k, w, current, tmp_path, monkeypatch, mode="continuity", binding=b)
    grant = {
        "manuscript_ref": prior["draft_ref"],
        "reviewer_task_id": job.task["task_id"],
        "reviewer_generation": job.task["generation"],
    }
    enroll_native_case(k, tmp_path, monkeypatch, **grant, controller=admin)
    proof = ReviewIndependenceService(k).issue(admin, **grant)["independence_ref"]
    previous = svc.read(*_args(job), draft_ref=prior["draft_ref"], idempotency_key="adjacent")
    note = {
        "pattern": "REPEATED_SEAM",
        "start": 0,
        "end": len(anchor),
        "severity": "MINOR",
        "disposition": "PREFERENCE",
        "reason": "前一场结尾已完整叙述相同的汽笛与雨伞，再次放在开头并未推进场景。",
        "counterinterpretation": "此处也可能有意保留两个视角共享的回声。",
        "requested_change": "对照前一场末句，决定用新的动作进入本场。",
        "narrative_function": "从两个视角衔接码头的动作。",
        "related_spans": [{"text_ref": previous["text_ref"], "start": 0, "end": len(anchor)}],
    }
    positive = svc.review(
        *_args(job),
        **{
            **args,
            "layer": "CHAPTER_SEAM",
            "observations": [note],
            "scope_draft_refs": b["ordered_draft_refs"],
            "related_independence_refs": [proof],
            "coverage_refs": [*args["coverage_refs"], previous["coverage_ref"]],
        },
    )
    _, clean, _ = _deliver(k, w, 3, prior, "河滩上留下两行湿脚印，秦生循着足迹走过了石桥。", budget)
    job, args, read = _reviewer(k, w, clean, tmp_path, monkeypatch)
    clean_note = {
        **note,
        "pattern": "MECHANICAL_CADENCE",
        "disposition": "PRESERVE",
        "start": 0,
        "end": len(clean["text"]),
        "requested_change": None,
        "related_spans": [],
    }
    negative = svc.review(*_args(job), **{**args, "observations": [clean_note]})
    quota = {
        "currency": "USD",
        "budget_id": budget,
        "max_amount": "0.2",
        "max_tokens": 180000,
        "max_attempts": 12,
        "deadline": (datetime.now(UTC) + timedelta(minutes=8)).isoformat(),
    }
    kwargs = {
        "task_id": source.task["task_id"],
        "generation": source.task["generation"],
        "failure_report_refs": [historical["report_ref"]],
        "success_report_refs": [],
        "evaluation_report_refs": [positive["report_ref"], negative["report_ref"]],
        "quota": quota,
        "order_seed": 41,
        "repetitions": 1,
    }
    candidates = AdaptiveInstructionService(k)
    engine = AdaptiveEvaluationService(k)

    def evaluate(kind, **extra):
        ref = candidates.propose(admin, wr, **kwargs, change_kind=kind, idempotency_key=kind, **extra)[
            "candidate_ref"
        ]
        result = engine.run(admin, wr, candidate_ref=ref, idempotency_key=kind)
        refs = {key: result[key] for key in ("candidate_ref", "engineering_report_ref", "run_manifest_ref")}
        with k.db.uow(write=False) as repo:
            report = engine.inspect(repo, wr, **refs)
        return result, report

    ready_run, ready = evaluate("ROLE_INSTRUCTIONS", instructions="在人物停顿时让读者先看见一个明确动作。")
    assert ready["status"] == "COMPLETE" and not ready["eligible"]
    assert "READINESS_ONLY_NO_INDEPENDENT_OUTPUT" in ready["quality"]["reasons"]
    assert all(
        r["output"]["payload"]["creative_outcome"] == "UNSCORABLE"
        and r["output"]["payload"]["scope_preserved"]
        for r in ready["results"]
    )
    with k.db.uow(write=False) as repo:
        candidate = k.artifact(repo, ready_run["candidate_ref"], wr["work_id"])["payload"]
        block = k.artifact(repo, candidate["instruction_ref"], wr["work_id"])["payload"]
    for case in {r["case_id"] for r in ready["results"]}:
        original, updated = sorted(
            (r for r in ready["results"] if r["case_id"] == case), key=lambda r: r["arm_id"]
        )
        left, right = (r["output"]["payload"] for r in (original, updated))
        assert left["user_sha256"] == right["user_sha256"]
        assert right["system_bytes"] - left["system_bytes"] == len(
            ("\nApproved role method:\n" + block["body"]).encode()
        )
    result, report = evaluate("CONTEXT_RECIPE", material_order=["prose", "scene"])
    assert report["eligible"], [(r["status"], r["score"]) for r in report["results"]]
    scored = [r for r in report["results"] if r["case_id"] == positive["report_ref"]["artifact_revision_id"]]
    control, treatment = sorted(scored, key=lambda r: r["arm_id"])
    assert treatment["score"]["evidence_position"] == 0 < control["score"]["evidence_position"]
    assert treatment["output"]["payload"]["materials"][0] == {"section": "prose", "items": [anchor]}
    assert not report["quality"]["regressed"] and report["quality"]["held_out_negatives"]
    s = release_inputs({"kernel": k, "admin": admin, "wr": wr, "world": w}, result)
    enroll_case(s, tmp_path)
    promotion = s["service"]
    until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    opinion = promotion.review(
        s["reviewer"].principal,
        wr,
        **s["refs"],
        **s["job"],
        publisher_grant_id=s["publisher"].grant_id,
        verdict="APPROVE",
        reason="优先提供已引用的相邻前文，未改变其它材料。",
        valid_until=until,
        idempotency_key="review-order",
    )
    promotion.decide(
        s["publisher"],
        wr,
        **s["refs"],
        review_ref=opinion["review_ref"],
        action="APPROVE",
        expected_epoch=0,
        reason="采用已验证的场景阅读顺序。",
        valid_until=until,
        idempotency_key="release-order",
    )
    _, _, after = _deliver(k, w, 4, prior, "她关上伞，让双手腾出地方。", budget)
    offered = json.loads(after.messages[1]["content"])["creative_materials"]
    assert offered[0] == {"section": "prose", "items": [anchor]} and "continuity" in [
        v["section"] for v in offered
    ]
    assert after.messages[0]["content"].count("Reading order and use of the supplied materials") == 1
