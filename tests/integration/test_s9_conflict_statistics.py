"""Immutable case decisions are counted once at a replayable PostgreSQL cut."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from novel_factory.application.audit_log import LogService
from novel_factory.application.statistics import StatisticsService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task, load_fixture, point

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_replay_counts_only_completed_outcomes_and_denies_actor(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    admin, world = fixture["principal"], fixture["world_ref"]
    task = create_bound_task(kernel, admin, world, point(2))
    case, receipt = str(uuid4()), str(uuid4())
    scope = (admin, world["work_id"], world["branch_id"])
    task_scope = dict(task_id=task["task_id"], generation=task["generation"])
    with kernel.db.uow() as repo:
        for _ in range(2):
            LogService.conflict(repo.conn, "detected", case_id=case, receipt_id=case,
                                outcome="SEMANTIC_UNCERTAIN", details={}, **task_scope)
            LogService.conflict(
                repo.conn, "resolved", case_id=case, receipt_id=receipt, duration_ms=13,
                outcome="REVIEW_REQUIRED", details={"resolution_strategy": "ESCALATE_REVIEW",
                    "cost_status": "NO_MODEL_CALL", "additional_model_attempts": 0},
                **task_scope,
            )
        messages = repo.conn.scalars(text("""SELECT outbox_id FROM nf_outbox
            WHERE topic='audit.recorded' AND aggregate_id IN (:case,:receipt)"""),
            {"case": case, "receipt": receipt}).all()
        assert len(messages) == 2
        for msg in reversed(messages):
            StatisticsService.handle(repo.conn, SimpleNamespace(event_id=msg))
    service = StatisticsService(kernel)
    observed = service.read(*scope)
    metrics = observed["metrics"]
    assert metrics["conflict.candidates"] == metrics["conflict.review_escalations"] == 1
    assert "conflict.confirmed_by_guard" not in metrics and "conflict.auto_resolved" not in metrics
    assert observed["measures"]["conflict_quality"]["false_negatives"] is None
    assert observed["measures"]["durations"]["semantic.conflict.resolved"] == {
        "samples": 1, "mean_ms": 13, "p95_ms": 13, "p99_ms": 13,
    }
    rebuilt = service.rebuild(*scope, source_cut=observed["source_cut"])
    assert {k: v for k, v in rebuilt["metrics"].items() if k.startswith("conflict.")} == {
        k: v for k, v in metrics.items() if k.startswith("conflict.")}
    assert service.verify_statistics(*scope, source_cut=observed["source_cut"])["consistent"]
    assert "conflict.candidates" not in service.rebuild(*scope, version=4)["metrics"]
    actor = kernel.issue_grant(admin, world["work_id"], role_id="character_actor",
                               actor_id=fixture["ids"]["protagonist"], planes=["BELIEF"],
                               branch_ids=[world["branch_id"]], capabilities=["READ"])["principal"]
    for read in (lambda: service.read(actor, *scope[1:]),
                 lambda: LogService.trace(kernel, actor, *scope[1:], task["task_id"])):
        with pytest.raises(DomainError):
            read()


def test_only_claim_from_exact_recompute_receipt_is_attributed(kernel_env):
    from novel_factory.application.statistics import _is_conflict_reexecution
    from test_s9_resolution import case_for, changed_reader
    k = kernel_env["kernel"]
    f, t, _, _ = changed_reader(k)
    svc, args, case = case_for(k, f, t)
    assert svc.resolve(*args, case)["outcome"] == "RECOMPUTE_QUEUED"
    new = k.claim_task(args[0], "newly-routed-employee", task_id=t["task_id"])
    with k.db.uow(write=False) as repo:
        assert _is_conflict_reexecution(repo.conn, new)
        assert not _is_conflict_reexecution(repo.conn, t)


def test_character_stdio_hides_an_actual_conflict_and_unknown_case_equally(kernel_env, tmp_path):
    import asyncio
    from novel_factory.application.agent_gateway import AgentGateway
    from novel_factory.application.runtime_tasks import RuntimeTaskService
    from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
    from test_p5_mcp import config, error, exercise
    from test_s9_resolution import case_for, changed_reader

    k = kernel_env["kernel"]
    story, source, change, _ = changed_reader(k)
    svc, scope, case = case_for(k, story, source)
    svc.resolve(*scope, case)
    rows = LogService.recent_conflicts(k, *scope)
    assert any(str(row["case_id"]) == case and row["receipt_id"] for row in rows)
    adapter = OpenAICompatibleAdapter(endpoint="https://fixture.invalid/v1/chat/completions",
                                      model="fixture", api_key=None)
    character = RuntimeTaskService(k, AgentGateway(k, {"test": adapter})).prepare(
        scope[0], change["world_ref"], role_id="character_actor", task_kind="character_action",
        actor_id=story["ids"]["rival"], story_time=point(40), subject_ids=[story["ids"]["sword"]],
        predicates=["owner"], assignment="Describe your own intention", adapter_id="test")
    binding = {**{key: character.task[key] for key in ("task_id", "generation")},
               "worker_id": character.worker_id}

    with pytest.raises(DomainError):
        LogService.recent_conflicts(k, character.principal, *scope[1:])

    async def check(session):
        for ref in (case, str(uuid4())):
            await error(session, "nf_conflict_trace", {"case_id": ref}, "VIEW_DENIED")
        received = (await session.call_tool("nf_context_get", {})).structuredContent
        assert received["actor_id"] == story["ids"]["rival"]
        assert case not in str(received) and source["task_id"] not in str(received)

    asyncio.run(exercise(config(tmp_path / "actor.json", kernel_env, character.private_token, binding), check))


def test_independent_label_set_replays_on_cut_and_excludes_self_labels(kernel_env):
    from test_s9_resolution import case_for
    from novel_factory.fixtures import run_command

    k = kernel_env["kernel"]
    f = load_fixture(k)
    wr, admin, ids = f["world_ref"], f["principal"], f["ids"]
    readers = [create_bound_task(k, admin, wr, point(1), [ids[entity]], [predicate])
               for entity, predicate in (("sword", "lifecycle"), ("guard1", "on_duty"))]
    changed = run_command(k, admin, wr, "DESTROY_ITEM", {"item_id": ids["sword"], "at": point(40)})["world_ref"]
    svc, scope, case = case_for(k, f, readers[0])
    assert svc.resolve(*scope, case)["outcome"] == "RECOMPUTE_QUEUED"
    k.claim_task(admin, "qualified-runtime", task_id=readers[0]["task_id"])
    reviewer = k.issue_grant(admin, wr["work_id"], role_id="control_operator",
        capabilities=["APPROVE", "ARTIFACT_WRITE"], planes=["WORLD"], branch_ids=[wr["branch_id"]])["principal"]
    with k.db.uow() as repo:
        rows = repo.conn.execute(text("SELECT * FROM nf_outbox WHERE topic IN ('world.accepted','task.changed')"))
        for row in rows.mappings():
            LogService.record_domain(repo.conn, row)
    labels = [{"task_id": t["task_id"], "generation": t["generation"],
               "world_revision_id": changed["world_revision_id"], "affected": i == 0,
               "reason": "Pre-labelled controlled task in the paired fixture"} for i, t in enumerate(readers)]
    def issue(signer, rows):
        return k.put_artifact(signer, wr["work_id"], {"kind": "SEMANTIC_LABEL_SET",
            "reviewer_grant_id": signer.grant_id, "work_id": wr["work_id"], "branch_id": wr["branch_id"],
            "labels": rows}, "EVIDENCE")
    stats = StatisticsService(k)
    ref = issue(reviewer, labels)
    quality = stats.labelled_conflicts(*scope, ref)
    assert quality["true_positives"] == quality["true_negatives"] == quality["observed_reruns"] == 1
    assert quality["false_positives"] == quality["false_negatives"] == quality["unnecessary_rework_fraction"] == 0
    assert stats.labelled_conflicts(*scope, ref, source_cut=quality["source_cut"]) == quality
    reversed_labels = issue(reviewer, [{**x, "affected": not x["affected"]} for x in labels])
    reversed_result = stats.labelled_conflicts(*scope, reversed_labels, source_cut=quality["source_cut"])
    assert reversed_result["false_positives"] == reversed_result["false_negatives"] == reversed_result["unnecessary_rework_fraction"] == 1
    with pytest.raises(DomainError, match="independent"):
        stats.labelled_conflicts(*scope, issue(admin, labels))
