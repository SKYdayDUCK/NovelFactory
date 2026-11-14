"""Actual PG/MCP, separate probe database/store, and send-forbidden native Host fixtures."""
import json
from copy import deepcopy
from dataclasses import replace

import conftest
import pytest
import test_reading_mcp_upgrade as legacy_fixture
from sqlalchemy import text
from test_production_strategy import synthetic_production
from test_production_verification import coverage_fixture

from agent_runtime.native_review import REVIEW_TRANSPORT_REVISION
from agent_runtime.protocol import ModelCallFailure, ModelResponse
from novel_factory.application import native_tooling_recovery as recovery
from novel_factory.application import production_pipeline as boards
from novel_factory.application import rolling_production as rolling
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.reading_mcp_upgrade import upgrade_reading_policy
from novel_factory.application.scene_revision import _archive
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.fixture(scope="module")
def external_probe_env(tmp_path_factory):
    # Reuse the repository's actual isolated-DB fixture, not the target's DB/store.
    setup = conftest.kernel_env.__wrapped__(tmp_path_factory)
    environment = next(setup)
    yield environment
    with pytest.raises(StopIteration):
        next(setup)


def prepared_probe(kernel, monkeypatch):
    gateway, owner, wr, _, _ = synthetic_production(kernel, monkeypatch)
    original = RuntimeModelAdapter.generate

    def marked(adapter, request):
        result = original(adapter, request)
        return replace(result, raw_response={**result.raw_response, "native_review_transport_revision": REVIEW_TRANSPORT_REVISION})
    monkeypatch.setattr(RuntimeModelAdapter, "generate", marked)
    for _ in range(20):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        with kernel.db.uow(write=False) as repo:
            board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
            for node in board["nodes"].values():
                task = node.get("task_id") and repo.one("tasks", {"task_id": node["task_id"]})
                if task and task["role_id"] == "reviewer" and task["state"] == "RESULT_STORED":
                    evidence, _ = CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
                    return gateway, owner, wr, task["task_id"], evidence
    pytest.fail("The isolated engineering probe did not complete its real MCP path")


def blocked_reading(kernel, monkeypatch, *, kind="epistemic", repeat=False, incomplete=False):
    if kind == "verification":
        gateway, owner, wr, calls, _, _ = coverage_fixture(kernel, monkeypatch)
    else:
        gateway, owner, wr, calls, _ = synthetic_production(kernel, monkeypatch)
    original = RuntimeModelAdapter.generate
    broken = []

    def generate(adapter, request):
        identity = json.loads(request.messages[-1]["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
        with kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": identity["task_id"]})
        if task["task_kind"] != kind or (broken and not repeat):
            return original(adapter, request)
        assert adapter.profile.kind == "codex-cli"
        adapter.journal.authorize_send(request.attempt_id)
        thread, turn = "tooling-thread-" + request.attempt_id, "tooling-turn-" + request.attempt_id
        adapter.journal.record(request.attempt_id, thread, None, "THREAD_STARTED")
        adapter.journal.record(request.attempt_id, thread, turn, "TURN_STARTED")
        if not incomplete:
            adapter.journal.record(request.attempt_id, thread, turn, "COMPLETED")
        payload = ({"text": "Engineering tool channel unavailable.", "event_refs": [], "important_deltas": [], "evidence_refs": []}
                   if kind == "assertion_extract" else {"decision": "Engineering tool channel unavailable.",
                       "reason": "code-mode host is disabled", "deferred_questions": [], "evidence_refs": []}
                   if kind == "verification" else {"summary": "code-mode host is disabled", "issues": [],
                       "counterinterpretations": [], "evidence_refs": []})
        broken.append(request.attempt_id)
        return ModelResponse(text=json.dumps({**identity, "status": "NEEDS_INPUT", "payload": payload,
            "unresolved_questions": ["Engineering: code-mode host is disabled; required tools are unavailable."]}),
            request_sha256=request.request_sha256, provider_receipt=turn, input_tokens=30, output_tokens=45,
            actual_amount=None, currency=None, raw_response={"codex_thread_id": thread, "codex_turn_id": turn,
                "model": adapter.profile.model, "auth_mode": "host-login", "billing": "SUBSCRIPTION_AMOUNT_UNKNOWN",
                "review_mcp_receipt_refs": []})

    monkeypatch.setattr(RuntimeModelAdapter, "generate", generate)
    for _ in range(35):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        with kernel.db.uow(write=False) as repo:
            board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
            for identity, node in board["nodes"].items():
                task = node.get("task_id") and repo.one("tasks", {"task_id": node["task_id"]})
                if task and task["task_kind"] == kind and task["state"] == "BLOCKED":
                    return gateway, owner, wr, identity, task, broken, calls
    pytest.fail("The completed no-tool return was not durably blocked")


@pytest.mark.parametrize("kind", ["epistemic", "literary", "assertion_extract", "verification"])
def test_post_fix_probe_enrolls_one_same_manuscript_reader_and_preserves_unknown(kernel_env, external_probe_env, monkeypatch, kind):
    kernel, probe_kernel = kernel_env["kernel"], external_probe_env["kernel"]
    _, probe_owner, probe_wr, probe_task_id, probe_audit = prepared_probe(probe_kernel, monkeypatch)
    gateway, owner, wr, identity, task, broken, _ = blocked_reading(kernel, monkeypatch, kind=kind)
    monkeypatch.setattr(recovery, "_runtime_binary_sha256", lambda: "e" * 64)
    certificate = {"binary_sha256": "e" * 64, "source_sha256": recovery._runtime_sources()}
    with kernel.db.uow() as repo:
        board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert recovery.recover_toolless_native_reading(kernel.in_transaction(repo), repo, gateway, owner, board, identity)
        assert board["nodes"][identity]["reason"] == recovery.REASON
        assert not recovery.recover_toolless_native_reading(kernel.in_transaction(repo), repo, gateway, owner, board, identity)
        boards._save(kernel, repo, owner, board)
        old_calls = repo.many("call_attempts", {"task_id": task["task_id"]})
        old_results = repo.many("task_results", {"task_id": task["task_id"]})
        budgets = repo.many("budgets", {"work_id": wr["work_id"]})
        count = repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts"))
    authorization = recovery.authorize_host_repair(kernel, gateway, owner, wr, probe_task_id=probe_task_id,
        source_revision="1" * 40, probe_kernel=probe_kernel, probe_principal=probe_owner, probe_certificate=certificate, apply=True)
    assert authorization["provider_calls_sent"] == 0
    with kernel.db.uow() as repo:
        board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        bound = kernel.in_transaction(repo)
        assert recovery.recover_toolless_native_reading(bound, repo, gateway.with_kernel(bound), owner, board, identity)
        new_task_id = board["nodes"][identity]["task_id"]
        boards._save(bound, repo, owner, board)
        assert new_task_id != task["task_id"]
        assert repo.one("tasks", {"task_id": task["task_id"]})["state"] == "CANCELLED"
        assert repo.many("call_attempts", {"task_id": task["task_id"]}) == old_calls
        assert repo.many("task_results", {"task_id": task["task_id"]}) == old_results
        assert repo.many("budgets", {"work_id": wr["work_id"]}) == budgets
        assert not repo.many("call_attempts", {"task_id": new_task_id})
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts")) == count
        assert not repo.one("artifacts", {"artifact_revision_id": probe_audit["response_ref"]["artifact_revision_id"]})
    for _ in range(120):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        state = rolling.rolling_status(kernel, owner, wr["work_id"], wr["branch_id"])
        if state["state"] == "FROZEN":
            break
    assert state["state"] == "FROZEN" and len(broken) == 1, state
    with kernel.db.uow(write=False) as repo:
        board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        cancelled = repo.one("tasks", {"task_id": task["task_id"]})
        call = repo.one("call_attempts", {"attempt_id": broken[0]})
        assert recovery.verify_superseded_tooling_return(kernel, repo, gateway, board, cancelled, call)
        assert repo.many("call_attempts", {"task_id": task["task_id"]}) == old_calls
        assert probe_wr["work_id"] != wr["work_id"] and count >= 1


def authorize_fixture(kernel, probe_kernel, monkeypatch, *, repeat=False):
    _, probe_owner, _, probe_id, _ = prepared_probe(probe_kernel, monkeypatch)
    fixture = blocked_reading(kernel, monkeypatch, repeat=repeat)
    gateway, owner, wr = fixture[:3]
    monkeypatch.setattr(recovery, "_runtime_binary_sha256", lambda: "e" * 64)
    recovery.authorize_host_repair(kernel, gateway, owner, wr, probe_task_id=probe_id, source_revision="1" * 40,
        probe_kernel=probe_kernel, probe_principal=probe_owner,
        probe_certificate={"binary_sha256": "e" * 64, "source_sha256": recovery._runtime_sources()}, apply=True)
    return fixture


def test_fixed_transport_still_without_tools_stops_after_one_fresh_read(kernel_env, external_probe_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, identity, _, broken, _ = authorize_fixture(kernel, external_probe_env["kernel"], monkeypatch, repeat=True)
    for _ in range(25):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        with kernel.db.uow(write=False) as repo:
            board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        if board["nodes"][identity].get("reason") == recovery.EXHAUSTED:
            break
    assert len(broken) == 2 and board["nodes"][identity]["reason"] == recovery.EXHAUSTED
    before = len(broken)
    for _ in range(4):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(broken) == before
    with kernel.db.uow() as repo:
        board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert len([item for item in board["nodes"][identity]["evidence_repairs"] if item["family"] == recovery.COMMAND]) == 1
        assert not recovery.recover_toolless_native_reading(kernel.in_transaction(repo), repo, gateway, owner, board, identity)


def test_recovery_defers_to_an_actual_enrolled_reader_without_using_a_second_slot(kernel_env, external_probe_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, identity, original, broken, _ = authorize_fixture(kernel, external_probe_env["kernel"], monkeypatch)
    with kernel.db.uow() as repo:
        board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        other = next(key for key, node in board["nodes"].items() if node["stage"] == "review")
        bound = kernel.in_transaction(repo)
        boards._enroll(bound, repo, gateway.with_kernel(bound), owner, board, other)
        assert recovery.recover_toolless_native_reading(bound, repo, gateway.with_kernel(bound), owner, board, identity)
        assert board["nodes"][identity]["tooling_wait"] == "READING_SLOT"
        assert not recovery.recover_toolless_native_reading(bound, repo, gateway.with_kernel(bound), owner, board, identity)
        assert repo.one("tasks", {"task_id": original["task_id"]})["state"] == "BLOCKED"
        assert sum(node["state"] == "ENROLLED" for node in board["nodes"].values()) == 1
        boards._save(bound, repo, owner, board)
    for _ in range(80):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        status = rolling.rolling_status(kernel, owner, wr["work_id"], wr["branch_id"])
        if status["state"] == "FROZEN":
            break
    assert status["state"] == "FROZEN" and len(broken) == 1


def test_incomplete_native_return_is_not_retried_or_misreported_as_novel_input(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, identity, original, broken, _ = blocked_reading(kernel, monkeypatch, incomplete=True)
    for _ in range(3):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    with kernel.db.uow() as repo:
        board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert board["nodes"][identity]["reason"] == "CALL_RECONCILIATION_REQUIRED"
        assert repo.one("tasks", {"task_id": original["task_id"]})["state"] == "BLOCKED"
        assert not recovery.recover_toolless_native_reading(kernel.in_transaction(repo), repo, gateway, owner, board, identity)
    assert len(broken) == 1


def test_cancelled_legacy_orphan_requires_upgrade_receipt_and_real_archived_coverage_successor(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]

    def uncovered_production(bound, patch, **_options):
        gateway, owner, wr, calls, diagnoses, _ = coverage_fixture(bound, patch)
        return gateway, owner, wr, calls, diagnoses
    monkeypatch.setattr(legacy_fixture, "synthetic_production", uncovered_production)
    gateway, owner, wr, _, board, _, request = legacy_fixture.legacy_clear(kernel, monkeypatch)
    identity = next(key for key, node in board["nodes"].items() if node["stage"] == "facts")
    with kernel.db.uow() as repo:
        bound = kernel.in_transaction(repo)
        boards._enroll(bound, repo, gateway.with_kernel(bound), owner, board, identity)
        orphan_id = board["nodes"][identity]["task_id"]
        boards._save(bound, repo, owner, board)
    original = RuntimeModelAdapter.generate

    def orphaned(adapter, packet):
        identity = json.loads(packet.messages[-1]["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
        if identity["task_id"] != orphan_id:
            return original(adapter, packet)
        adapter.journal.authorize_send(packet.attempt_id)
        adapter.journal.record(packet.attempt_id, "abandoned-thread", None, "THREAD_STARTED")
        adapter.journal.record(packet.attempt_id, "abandoned-thread", "abandoned-turn", "TURN_STARTED")
        raise ModelCallFailure("CODEX_TRANSPORT_UNKNOWN", sent=True)
    monkeypatch.setattr(RuntimeModelAdapter, "generate", orphaned)
    RuntimeSupervisor(kernel, gateway).admit_model(owner, wr["work_id"], wr["branch_id"])
    monkeypatch.setattr(RuntimeModelAdapter, "generate", original)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        bound = kernel.in_transaction(repo)
        kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
        task = repo.one("tasks", {"task_id": orphan_id})
        call = repo.one("call_attempts", {"task_id": orphan_id})
        assert call["state"] == "UNKNOWN" and task["current_result_id"] is None
        # Exercise the real restore fence (call generation 1, current task 2).
        restored = kernel.tasks._recover_task(repo.conn, task, reason="RESTORE_ADMIN_FENCE")
        assert restored["generation"] == call["generation"] + 1
        bound.tasks.cancel(orphan_id, restored["generation"], principal_id=owner.principal_id,
            idempotency_key="mcp-upgrade-orphan:" + call["attempt_id"], reason=recovery.LEGACY_CANCEL_REASON)
        task = repo.one("tasks", {"task_id": orphan_id})
        assert not recovery.verify_superseded_tooling_return(bound, repo, gateway, board, task, call)
        selected = boards._load(bound, repo, wr["work_id"], wr["branch_id"])
        ref = boards._save(bound, repo, owner, selected)
        request = replace(request, expected_board_sha256=ref["sha256"])
    upgraded = upgrade_reading_policy(kernel, gateway, owner, request, apply=True)
    assert upgraded["cancelled_unknown_attempts"][0]["attempt_id"] == call["attempt_id"]
    with kernel.db.uow(write=False) as repo:
        current = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert not recovery.verify_superseded_tooling_return(kernel, repo, gateway, current, task, call)
    for _ in range(120):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        status = rolling.rolling_status(kernel, owner, wr["work_id"], wr["branch_id"])
        if status["state"] == "FROZEN":
            break
    assert status["state"] == "FROZEN", status
    with kernel.db.uow(write=False) as repo:
        current = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        task = repo.one("tasks", {"task_id": orphan_id})
        assert recovery.verify_superseded_tooling_return(kernel, repo, gateway, current, task, call)
        successor = repo.one("tasks", {"task_id": current["nodes"][identity]["task_id"]})
        old_spec = repo.one("task_inputs", {"task_id": orphan_id, "generation": call["generation"]})["spec"]
        new_spec = repo.one("task_inputs", {"task_id": successor["task_id"], "generation": successor["generation"]})["spec"]
        assert old_spec["world_ref"] != new_spec["world_ref"]
        archived = deepcopy(current)
        _archive(archived["nodes"][identity])
        assert recovery.verify_superseded_tooling_return(kernel, repo, gateway, archived, task, call)
        for record in archived["nodes"][identity]["history"]:
            source_task = record.get("task_id") and repo.one("tasks", {"task_id": record["task_id"]})
            if source_task and source_task["state"] == "ACCEPTED":
                record["generation"] = source_task["generation"] + 1
        with pytest.raises(DomainError) as wrong_generation:
            recovery.verify_superseded_tooling_return(kernel, repo, gateway, archived, task, call)
        assert wrong_generation.value.code == "INVALID_EVIDENCE"
        assert repo.one("call_attempts", {"attempt_id": call["attempt_id"]}) == call
