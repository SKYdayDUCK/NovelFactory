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
