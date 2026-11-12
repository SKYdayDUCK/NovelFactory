import json
from datetime import UTC, datetime, timedelta

import pytest
import test_harness_dynamic_registry
import test_harness_planning_simulation
from test_adaptive_promotion import enroll_case, release_case
from test_harness_planning_simulation import decide, propose, simulate

from novel_factory.application.creative_metrics import CreativeMetricsService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture

scene = test_harness_planning_simulation.scene
delegated_site = test_harness_dynamic_registry.delegated_site
setup = test_harness_dynamic_registry.setup

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_planning_ratios_count_only_executed_paths_and_keep_pending(scene):
    s = scene
    service = CreativeMetricsService(s["kernel"])

    def see(**args):
        return service.report(s["owner"], s["wr"], **args)

    candidate = propose(s)
    run = simulate(s, candidate)
    ids = [p["option_id"] for p in run["stops"] if p["steps"] > 0]
    assert len(ids) == 2
    before = see()
    assert before["planning"]["pending_paths"] == len(ids)
    assert before["planning"]["adoption"]["rate"] is None
    for arm in before["review"]["heldout_review_errors"].values():
        assert arm["false_negatives"] is None and arm["false_positives"] is None
    decide(s, candidate, ids[0], "adopt-first")
    observed = see()
    assert observed["planning"]["adoption"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    assert observed["planning"]["abandonment"] == observed["planning"]["adoption"]
    assert observed["planning"]["candidates"] == 1 and observed["planning"]["alternatives"] >= 2
    assert observed["latency"]["terminal_ms"]["p95"] is None
    assert "conditional_result" not in json.dumps(observed) and "believed_by" not in json.dumps(
        observed
    )
    with pytest.raises(DomainError, match="row limit"):
        see(limit=1)
    with pytest.raises(DomainError):
        service.report(s["actors"][0].principal, s["wr"])
    other = load_fixture(s["kernel"])
    with pytest.raises(DomainError):
        service.report(s["owner"], other["world_ref"])
    sibling = s["kernel"].fork(s["owner"], s["wr"], "CANDIDATE", "metrics-empty")
    assert service.report(s["owner"], sibling)["planning"]["candidates"] == 0
    with pytest.raises(DomainError):
        see(since="2025-03-01", until="2025-03-02")
    with pytest.raises(DomainError):
        see(since="2025-01-01T00:00:00Z", until="2025-04-01T00:00:00Z")


def test_real_s5_run_and_signed_native_review_population(kernel_env, tmp_path, monkeypatch):
    k = kernel_env["kernel"]
    s = release_case(k, tmp_path, monkeypatch)
    m = CreativeMetricsService(k)

    def see():
        return m.report(s["admin"], s["wr"])

    earlier = see()
    assert earlier["instructions"]["complete"] == 1 and earlier["instructions"]["eligible"] == 1
    assert earlier["instructions"]["promoted"] == earlier["instructions"]["rolled_back"] == 0
    assert earlier["review"]["coverage"]["rate"] == 1
    errors = earlier["review"]["heldout_review_errors"]
    assert errors["CONTROL"]["false_negatives"] > errors["TREATMENT"]["false_negatives"] == 0
    assert (
        errors["TREATMENT"]["false_positives"] == 0
        and errors["TREATMENT"]["negative_case_false_alarm_rate"]["denominator"] >= 1
    )
    assert earlier["latency"]["by_terminal_state_ms"]["ACCEPTED"]["n"] >= 4
    refs, pub, svc = s["refs"], s["publisher"], s["service"]
    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    enroll_case(s, tmp_path)
    op = svc.review(
        s["reviewer"].principal,
        s["wr"],
        **refs,
        **s["job"],
        publisher_grant_id=pub.grant_id,
        verdict="APPROVE",
        reason="独立留出样本支持。",
        valid_until=deadline,
        idempotency_key="metric-op",
    )
    change = svc.decide(
        pub,
        s["wr"],
        **refs,
        review_ref=op["review_ref"],
        action="APPROVE",
        expected_epoch=0,
        reason="留出核验通过。",
        valid_until=deadline,
        idempotency_key="metric-release",
    )
    svc.rollback(
        pub,
        s["wr"],
        decision_ref=change["decision_ref"],
        expected_epoch=1,
        reason="恢复原指令。",
        idempotency_key="metric-back",
    )
    new = see()
    assert new["instructions"]["promoted"] == new["instructions"]["rolled_back"] == 1
    assert new["review"]["independently_fixed"]["numerator"] == 0


def test_scoped_dynamic_instance_count_distinguishes_versions(setup):
    k, wr, owner, source, svc, args, _ = setup
    a = svc.register_revision(owner, *source, **args(0))
    svc.register_revision(owner, *source, **args(1))
    svc.select(
        owner,
        wr,
        instance_ref=a["instance_ref"],
        revision_ref=a["revision_ref"],
        expected_epoch=0,
        idempotency_key="metric-select",
    )
    report = CreativeMetricsService(k).report(owner, wr)
    assert report["dynamic"]["instances_created"] == 1
    assert sum(report["dynamic"]["actions"].values()) == 1
    assert not report["dynamic"]["dispatched_skill_versions"]


def test_measured_packets_scene_billing_and_provider_tokens_survive_unknown_amount(
    kernel_env, monkeypatch
):
    from decimal import Decimal

    from test_harness_context import EnvelopeFixture, configured
    from test_release import setup_batch

    from novel_factory.application.agent_gateway import AgentGateway
    from novel_factory.application.planning import PlanningService
    from novel_factory.application.release import ReleaseService
    from novel_factory.application.runtime_tasks import RuntimeTaskService
    from novel_factory.domain.canonical import canonical_bytes
    from novel_factory.fixtures import point

    k = kernel_env["kernel"]
    f = setup_batch(k, count=2)
    wr, owner = f["world_ref"], f["principal"]
    configured(monkeypatch)
    budget = k.tasks.create_budget(wr["work_id"], "1")
    expected_bytes = []
    expected_sources = []
    for i, amount in enumerate((Decimal("0.01"), None)):
        adapter = EnvelopeFixture(amount=amount)
        gw = AgentGateway(k, {"fixture": adapter})
        job = RuntimeTaskService(k, gw).prepare(
            owner,
            wr,
            role_id="writer",
            task_kind="scene_draft",
            story_time=point(1, 1),
            subject_ids=[f["ids"]["sword"]],
            predicates=["possessed_by"],
            assignment="记录河岸一幕。",
            adapter_id="fixture",
            input_artifacts=[f["plan_ref"], f["batch_ref"], f["scene_refs"][i]],
        )
        tid, gen = job.task["task_id"], job.task["generation"]
        PlanningService(k).bind_task_for_controller(
            owner, tid, gen, f["plan_ref"], f["scenes"][i]["plan_node_ids"], "bind-plan-" + tid
        )
        ReleaseService(k).bind_writer_task(
            job.principal, tid, gen, f["scene_refs"][i], [], "bind-scene-" + tid
        )
        req = {
            "schema_version": "2.1",
            "task_id": tid,
            "generation": gen,
            "worker_id": job.worker_id,
            "call_key": "measure",
            "input_ref": job.agent_input["input_ref"],
            "budget_id": budget,
            "reserve_amount": "0.1",
            "token_reservation": 100000,
        }
        first = gw.execute(job.principal, req)
        assert gw.execute(job.principal, req) == first and len(adapter.calls) == 1
        expected_bytes.append(
            len(canonical_bytes(json.loads(adapter.calls[0].messages[1]["content"])))
        )
        with k.db.uow(write=False) as repo:
            audit = k.artifact(repo, first["evidence_ref"], wr["work_id"])["payload"]
            packet = k.artifact(repo, audit["creative_context_ref"], wr["work_id"])["payload"][
                "packet"
            ]
            expected_sources.append(len(packet["sources"]))
    m = CreativeMetricsService(k)
    result = m.report(owner, wr)
    assert (
        result["context"]["packet_build_ms"]["n"] == 2 and result["context"]["compile_ms"]["n"] >= 2
    )
    assert result["context"]["model_input_bytes"]["sum"] == sum(expected_bytes)
    assert result["context"]["loaded_sources"]["sum"] == sum(expected_sources)
    assert result["context"]["compiled_bytes"]["sum"] > 0
    assert result["dynamic"]["dispatched_skill_versions"][0]["calls"] == 2
    assert result["latency"]["model_call_ms_by_execution_kind"]["TEST_FIXTURE"]["n"] == 2
    scenes = {row["scene_id"]: row for row in result["scenes"]}
    known, unknown = [scenes[s["scene_contract"]["scene_id"]] for s in f["scenes"]]
    assert known["actual_tokens"] == unknown["actual_tokens"] == 65
    assert known["actual_amount"] == "0.010000" and unknown["actual_amount"] is None
    assert (
        unknown["tokens_from_unsettled_provider_receipts"]
        == unknown["unknown_amount_attempts"]
        == 1
    )
    assert known["execution_origin"] == unknown["execution_origin"] == "TEST_FIXTURE"
    assert result["review"]["heldout_review_errors"]["CONTROL"]["false_negatives"] is None


def test_real_lease_fence_and_reclaimed_generation_counts_once(kernel_env, monkeypatch):
    from test_harness_context import EnvelopeFixture, configured

    from novel_factory.application.agent_gateway import AgentGateway
    from novel_factory.application.queries import WorldQueryService
    from novel_factory.application.runtime_tasks import RuntimeTaskService
    from novel_factory.fixtures import point

    k = kernel_env["kernel"]
    w = load_fixture(k)
    wr, owner = w["world_ref"], w["principal"]
    configured(monkeypatch)
    gw = AgentGateway(k, {"fixture": EnvelopeFixture()})
    job = RuntimeTaskService(k, gw).prepare(
        owner,
        wr,
        role_id="writer",
        task_kind="scene_draft",
        story_time=point(1, 1),
        subject_ids=[w["ids"]["sword"]],
        predicates=["possessed_by"],
        assignment="清点原有的事实。",
        adapter_id="fixture",
    )
    tid = job.task["task_id"]
    with k.db.uow() as repo:
        repo.change("tasks", {"task_id": tid}, {"lease_expiry": repo.now() - timedelta(seconds=2)})
    (blocked,) = k.tasks.recover_expired()
    assert blocked["state"] == "READY"
    service = CreativeMetricsService(k)
    assert service.report(owner, wr)["recovery"]["success_after_failure"] == {
        "numerator": 0,
        "denominator": 1,
        "rate": 0,
    }
    claimed = k.claim_task(job.principal, "replacement", task_id=tid, lease_seconds=60)
    gen = claimed["generation"]
    WorldQueryService(k).context_compile(
        job.principal,
        {
            "schema_version": "2.1",
            "task_id": tid,
            "generation": gen,
            "recipe_id": "WRITER",
            "input_manifest_ref": job.manifest_ref,
            "read_contract": {
                "revision": {"mode": "FIXED", "world_ref": wr},
                "process_policy": "ALLOW_PARTIAL",
                "index_policy": "AUTHORITATIVE",
                "max_wait_ms": 0,
            },
            "knowledge_cut": {"plane": "WORLD"},
            "subject_time": {"kind": "POINT", "at": point(1, 1)},
            "token_budget": 20000,
            "idempotency_key": "metric-recover",
        },
    )
    budget = k.tasks.create_budget(wr["work_id"], "1")
    gw.execute(
        job.principal,
        {
            "schema_version": "2.1",
            "task_id": tid,
            "generation": gen,
            "worker_id": "replacement",
            "call_key": "finish",
            "input_ref": job.agent_input["input_ref"],
            "budget_id": budget,
            "reserve_amount": "0.1",
            "token_reservation": 100000,
        },
    )
    assert service.report(owner, wr)["recovery"]["success_after_failure"] == {
        "numerator": 1,
        "denominator": 1,
        "rate": 1,
    }


def test_rework_is_not_fixed_until_independent_reread_and_native_editor_acceptance(
    kernel_env, tmp_path, monkeypatch
):
    from test_harness_review_workflow import (
        CHANGE,
        LINE,
        _args,
        _formal,
        _repeat,
        _reviewer,
        _writer,
    )
    from test_release import accept_draft, setup_batch, write_scene

    from novel_factory.application.creative_review import CreativeReviewService

    k = kernel_env["kernel"]
    f = setup_batch(k, count=1)
    owner, wr = f["principal"], f["world_ref"]
    svc = CreativeReviewService(k)
    metrics = CreativeMetricsService(k)
    draft = accept_draft(k, f, write_scene(k, f, 0, LINE * 3))
    reviewer, args, read = _reviewer(k, f, draft, tmp_path, monkeypatch)
    report = svc.review(*_args(reviewer), **{**args, "observations": [_repeat(read)]})
    svc.decide(
        owner,
        *_args(reviewer)[1:],
        report_ref=report["report_ref"],
        action="RETURN",
        reason="定向改动并复审。",
        idempotency_key="return",
    )
    writer = _writer(k, f, draft, report["report_ref"])
    child = svc.revise(
        *_args(writer),
        report_ref=report["report_ref"],
        scene_ref=f["scene_refs"][0],
        worker_id=writer.worker_id,
        replacements=[{"finding_id": report["findings"][0]["finding_id"], "replacement": CHANGE}],
        idempotency_key="replace",
    )
    before = metrics.report(owner, wr)["review"]
    assert before["targeted_revisions"] == 1 and before["independently_fixed"] == {
        "numerator": 0,
        "denominator": 1,
        "rate": 0,
    }
    edited = accept_draft(
        k,
        f,
        {
            "writer": writer.principal,
            "task": writer.task,
            "draft_ref": child["draft_ref"],
            "text": LINE * 2 + CHANGE,
        },
    )
    newjob, newargs, newread = _reviewer(k, f, edited, tmp_path, monkeypatch)
    reread = svc.review(
        *_args(newjob),
        **{**newargs, "quota": args["quota"], "prior_report_ref": report["report_ref"]},
    )
    svc.decide(
        owner,
        *_args(newjob)[1:],
        report_ref=reread["report_ref"],
        action="ACCEPT_REVISION",
        reason="已核对正文及上次指出的问题。",
        idempotency_key="approve",
        native_decision=_formal(k, f, svc, newjob, newargs, newread, reread),
    )
    after = metrics.report(owner, wr)["review"]
    assert after["targeted_revisions"] == 1 and after["independently_fixed"] == {
        "numerator": 1,
        "denominator": 1,
        "rate": 1,
    }
    assert after["coverage"] == {"numerator": 2, "denominator": 2, "rate": 1}
