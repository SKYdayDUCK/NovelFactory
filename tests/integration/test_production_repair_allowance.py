"""PG17: one finite legacy policy migration preserves all production history.

Synthetic setup may reserve an UNKNOWN journal; every real transport is forbidden.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from test_agent_gateway import FixtureAdapter

from agent_runtime import native_codex, providers
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.planning import PlanningService
from novel_factory.application.production_pipeline import _key, _save
from novel_factory.application.production_reassessment import repair_budget
from novel_factory.application.production_repair_allowance import (
    COMMAND,
    authorize_repair_allowance,
)
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.volume_successor import _selected
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter, TrustedHostAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.fixture(autouse=True)
def forbid_real_send(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Repair authorization reached a real model transport")
    monkeypatch.setattr(providers, "post", forbidden)
    monkeypatch.setattr(native_codex, "generate", forbidden)
    monkeypatch.setattr(native_codex, "start_host", forbidden)
    monkeypatch.setattr(OpenAICompatibleAdapter, "generate", forbidden)
    monkeypatch.setattr(TrustedHostAdapter, "generate", forbidden)
    monkeypatch.setattr(FixtureAdapter, "generate", forbidden)


def legacy(kernel, revision=75):
    world = load_fixture(kernel)
    wr, owner = world["world_ref"], world["principal"]
    gateway = AgentGateway(kernel, {"fixture": FixtureAdapter()})
    prepared = RuntimeTaskService(kernel, gateway).prepare(owner, wr, role_id="writer", task_kind="scene_draft",
        story_time=point(1), subject_ids=[world["ids"]["sword"]], predicates=["owner"],
        assignment="Synthetic legacy repair", adapter_id="fixture")
    task = prepared.task
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    call = kernel.tasks.reserve_call(task["task_id"], task["generation"], prepared.worker_id,
        budget_id=budget, amount="0.25", token_amount=100, call_key="legacy-unknown", request_hash="a" * 64)
    kernel.tasks.mark_call_sent(call["attempt_id"], worker_id=prepared.worker_id)
    kernel.tasks.settle_call(call["attempt_id"], state="UNKNOWN")
    node_id = "draft:" + str(uuid4())
    chapter = str(uuid4())
    board = {"world_ref": wr, "volume_id": str(uuid4()), "sentinel": {"must_remain": True},
        "nodes": {node_id: {"stage": "draft", "state": "STOPPED", "reason": "MODEL_TRANSPORT_UNKNOWN",
            "chapter_id": chapter,
            "task_id": task["task_id"], "generation": task["generation"], "revision_count": revision,
            "history": [{"task_id": str(uuid4()), "generation": 1, "state": "DRAFT_STORED"}],
            "repair_plans": [{"method": "Synthetic historical strategy"}],
            "reassessments": [{"node_id": "old:1"}, {"node_id": "old:2"}]}}}
    for prefix, stage in (("knowledge:", "epistemic"), ("facts:", "facts"), ("review:", "review")):
        identity = prefix + (chapter if stage == "review" else node_id.removeprefix("draft:"))
        board["nodes"][identity] = {"stage": stage, "state": "PENDING", "chapter_id": chapter}
        if stage == "epistemic":
            board["nodes"][identity]["schema_repairs"] = [{"signature": str(i)} for i in range(30)]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        ref = _save(kernel, repo, owner, board)
    request = {"world_ref": wr, "expected_board_sha256": ref["sha256"], "node_id": node_id,
        "expected_task_id": task["task_id"], "expected_generation": task["generation"],
        "expected_revision": revision, "source_revision": "b" * 40, "idempotency_key": "legacy-v1"}
    request["technical_bindings"] = authorize_repair_allowance(kernel, owner, **request)["technical_bindings"]
    return owner, board, ref, request


def journal(kernel, request):
    work = request["world_ref"]["work_id"]
    task = request["expected_task_id"]
    with kernel.db.uow(write=False) as repo:
        return {"task": repo.one("tasks", {"task_id": task}),
            "inputs": repo.many("task_inputs", {"task_id": task}),
            "attempts": repo.many("call_attempts", {"task_id": task}),
            "budgets": repo.many("budgets", {"work_id": work})}


def metadata(kernel, request):
    work, branch = request["world_ref"]["work_id"], request["world_ref"]["branch_id"]
    with kernel.db.uow(write=False) as repo:
        return {"selected": _selected(repo, work, _key(branch)),
                **{table: len(repo.many(table, {"work_id": work})) for table in ("artifacts", "receipts")},
                "outbox": len(repo.many("outbox"))}


@pytest.mark.parametrize("revision", [75, 36])
def test_preview_apply_preserves_every_old_attempt_budget_task_and_board(kernel_env, revision):
    kernel = kernel_env["kernel"]
    owner, board, old_ref, request = legacy(kernel, revision)
    old_journal, before = journal(kernel, request), metadata(kernel, request)
    preview = authorize_repair_allowance(kernel, owner, **request)
    assert preview["status"] == "PREVIEW" and preview["board_ref"] == old_ref
    assert preview["remaining_revisions"] == 12 and preview["remaining_reassessments"] == 2
    assert metadata(kernel, request) == before and journal(kernel, request) == old_journal
    result = authorize_repair_allowance(kernel, owner, **request, apply=True)
    assert result["status"] == "APPLIED" and result["provider_calls_sent"] == 0
    with kernel.db.uow(write=False) as repo:
        selected = kernel.artifact(repo, result["board_ref"], request["world_ref"]["work_id"])["payload"]
        old = kernel.artifact(repo, old_ref, request["world_ref"]["work_id"])["payload"]
        authorization = kernel.artifact(repo, result["authorization_ref"], request["world_ref"]["work_id"],
                                        kind="PRODUCTION_REPAIR_AUTHORIZATION")["payload"]
    expected = deepcopy(board)
    expected["nodes"][request["node_id"]]["repair_allowance"] = {"revision_baseline": revision, "reassessment_baseline": 2}
    for binding in request["technical_bindings"]:
        expected["nodes"][binding["node_id"]]["technical_repair_allowance"] = {"repair_baseline": binding["repair_count"]}
    assert selected == expected and old == board
    assert authorization["request"]["source_revision"] == "b" * 40
    assert journal(kernel, request) == old_journal
    assert repair_budget(selected["nodes"][request["node_id"]]) is None
    assert authorize_repair_allowance(kernel, owner, **request, apply=True) == result
    assert metadata(kernel, request)["receipts"] == before["receipts"] + 1


@pytest.mark.parametrize("field,value", [("expected_board_sha256", "f" * 64),
    ("expected_task_id", str(uuid4())), ("expected_generation", 900), ("expected_revision", 74)])
def test_stale_binding_cannot_migrate(kernel_env, field, value):
    kernel = kernel_env["kernel"]
    owner, _, _, request = legacy(kernel)
    before = metadata(kernel, request)
    with pytest.raises(DomainError) as error:
        authorize_repair_allowance(kernel, owner, **{**request, field: value}, apply=True)
    assert error.value.code in {"STALE_PLAN", "STALE_GENERATION"}
    assert metadata(kernel, request) == before


def test_concurrent_apply_has_one_winner_and_same_key_receipt_replays(kernel_env):
    kernel = kernel_env["kernel"]
    owner, _, _, request = legacy(kernel)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: authorize_repair_allowance(kernel, owner, **request, apply=True), range(2)))
    assert results[0] == results[1]
    result = results[0]
    with pytest.raises(DomainError, match="one-time policy migration"):
        authorize_repair_allowance(kernel, owner, **{**request, "expected_board_sha256": result["board_ref"]["sha256"],
                                                     "idempotency_key": "second-grant"}, apply=True)
    with pytest.raises(DomainError) as error:
        authorize_repair_allowance(kernel, owner, **{**request, "source_revision": "c" * 40}, apply=True)
    assert error.value.code == "IDEMPOTENCY_MISMATCH"


def test_reselecting_legacy_board_cannot_repeat_policy_migration(kernel_env):
    kernel = kernel_env["kernel"]
    owner, _, old_ref, request = legacy(kernel)
    authorize_repair_allowance(kernel, owner, **request, apply=True)
    with kernel.db.uow() as repo:
        PlanningService(kernel)._select(repo, request["world_ref"], _key(request["world_ref"]["branch_id"]), old_ref)
    with pytest.raises(DomainError) as error:
        authorize_repair_allowance(kernel, owner, **{**request, "idempotency_key": "rollback-grant"}, apply=True)
    assert error.value.code == "REPAIR_ALLOWANCE_ALREADY_AUTHORIZED"


def test_runtime_writer_cannot_authorize_allowance(kernel_env):
    from novel_factory.application.authorization import Principal

    kernel = kernel_env["kernel"]
    _, _, _, request = legacy(kernel)
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": request["expected_task_id"]})
    principal = Principal(task["grant_id"], task["principal_id"])
    with pytest.raises(DomainError) as error:
        authorize_repair_allowance(kernel, principal, **request, apply=True)
    assert error.value.code == "VIEW_DENIED"


def test_receipt_failure_rolls_back_selection_and_authorization(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    owner, _, _, request = legacy(kernel)
    before, old_journal = metadata(kernel, request), journal(kernel, request)
    original = kernel._receipt
    def fail(repo, principal, wr, kind, *args, **kwargs):
        if kind == COMMAND:
            raise RuntimeError("synthetic receipt failure")
        return original(repo, principal, wr, kind, *args, **kwargs)
    monkeypatch.setattr(kernel, "_receipt", fail)
    with pytest.raises(RuntimeError, match="synthetic receipt failure"):
        authorize_repair_allowance(kernel, owner, **request, apply=True)
    assert metadata(kernel, request) == before and journal(kernel, request) == old_journal


@pytest.mark.parametrize("state", ["ENROLLED", "DRAFT_STORED", "STOPPED"])
def test_missing_node_generation_binds_current_task_only_while_enrolled(kernel_env, state):
    kernel = kernel_env["kernel"]
    owner, board, _, request = legacy(kernel)
    node = board["nodes"][request["node_id"]]
    node.pop("generation")
    node["state"] = state
    with kernel.objects.lock(), kernel.db.uow() as repo:
        ref = _save(kernel, repo, owner, board)
    request["expected_board_sha256"] = ref["sha256"]
    before = journal(kernel, request)
    if state == "ENROLLED":
        result = authorize_repair_allowance(kernel, owner, **request, apply=True)
        with kernel.db.uow(write=False) as repo:
            changed = kernel.artifact(repo, result["board_ref"], request["world_ref"]["work_id"])["payload"]
        assert "generation" not in changed["nodes"][request["node_id"]]
        assert journal(kernel, request) == before
    else:
        with pytest.raises(DomainError) as error:
            authorize_repair_allowance(kernel, owner, **request, apply=True)
        assert error.value.code == "STALE_GENERATION"
