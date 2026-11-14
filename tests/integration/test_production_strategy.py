"""PG17 durable diagnosis handoffs with fail-closed synthetic model transports."""
import json
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from test_p6_rolling_production import answer, request
from test_release import setup_batch

from agent_runtime import native_codex, providers
from agent_runtime.protocol import ModelResponse
from novel_factory.application import production_pipeline as boards
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.rolling_production import register_rolling, rolling_status
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure import runtime_providers
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def synthetic_production(kernel, monkeypatch, *, lengths=None, target=(6, 10), diagnosis="new", editorial=False):
    """Use actual native ledgers, never the real Codex host or HTTP send path."""
    from novel_factory.application.production_extent import bind_extent
    f = setup_batch(kernel, count=1)
    owner, wr = f["principal"], f["world_ref"]
    params = request(kernel, f, 1)
    params["profile"]["overrides"]["production_mode"] = "quality"
    params["routes"] = {"draft": "creative", "review": "review", "plan": "review"}
    with kernel.objects.lock(), kernel.db.uow() as repo:
        bind_extent(kernel, repo, owner, wr,
                    {"preset": "short", "unit": "han_characters", "min": target[0], "max": target[1]})
    sizes = iter(lengths) if lengths is not None else None
    calls = []
    diagnoses = []

    def invoke(profile, packet, *, authorize, record, receipt_target=None):
        identity = json.loads(packet.messages[-1]["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
        user = json.loads(packet.messages[1]["content"])
        user["context"] = identity
        with kernel.db.uow(write=False) as repo:
            task_kind = repo.one("tasks", {"task_id": identity["task_id"]})["task_kind"]
        native = profile.kind == "codex-cli"
        assert native if identity["role_id"] != "writer" else profile.model == "deepseek-flash"
        assert authorize(packet.attempt_id) is not False
        calls.append(({**identity, "task_kind": task_kind}, deepcopy(user), profile.kind))
        result = answer(user)
        if identity["role_id"] == "writer" and sizes is not None:
            result["text"] = "字" * next(sizes)
        elif identity["role_id"] == "chief_editor":
            frozen = json.loads(user["assignment"].rsplit("\n", 1)[1])
            diagnoses.append(frozen)
            repeated = diagnosis == "repeat" or (diagnosis == "repeat_after_first" and frozen["round"] == 2)
            method = (frozen["prior_methods"][-1] if repeated
                      else f"Synthetic diagnostic method {frozen['round']}: verify measured evidence before revision")
            result = {"decision": method, "reason": "Synthetic diagnosis: the prior method omitted a verification step",
                      "deferred_questions": ["Synthetic missing prerequisite"] if diagnosis == "needs_input" else [],
                      "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": frozen[key]}
                                        for key in ("source_draft_ref", "report_ref")]}
        elif editorial and identity["role_id"] == "reviewer" and task_kind == "literary" and not diagnoses:
            source = json.loads(user["authorized_materials"][0]["content"])["draft_ref"]
            result["issues"] = [{"issue_id": str(uuid4()), "category": "LITERARY", "severity": "BLOCKER",
                "subject_ref": source, "description": "Synthetic recurring editorial issue",
                "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": source}],
                "suggested_change": "Resolve the synthetic issue", "disposition": "OPEN", "issue_kind": "CHANGE_REQUEST"}]
        envelope = {**identity, "status": "SUCCEEDED", "payload": result, "unresolved_questions": []}
        turn = "synthetic-turn-" + packet.attempt_id
        raw = {"model": profile.model}
        if native:
            thread = "synthetic-thread-" + packet.attempt_id
            record(packet.attempt_id, thread, None, "THREAD_STARTED")
            record(packet.attempt_id, thread, turn, "TURN_STARTED")
            record(packet.attempt_id, thread, turn, "COMPLETED")
            raw.update(codex_thread_id=thread, codex_turn_id=turn, auth_mode="host-login",
                       billing="SUBSCRIPTION_AMOUNT_UNKNOWN")
        return ModelResponse(text=json.dumps(envelope), request_sha256=packet.request_sha256,
            provider_receipt=turn, input_tokens=25, output_tokens=40, cached_input_tokens=0,
            actual_amount=None if native else Decimal(0), currency=None if native else "USD", raw_response=raw)

    def forbidden(*args, **kwargs):
        pytest.fail("Synthetic diagnosis test reached a real model send boundary")

    monkeypatch.setattr(native_codex, "generate", forbidden)
    monkeypatch.setattr(native_codex, "start_host", forbidden)
    monkeypatch.setattr(providers, "post", forbidden)
    monkeypatch.setattr(providers.Profile, "preflight", lambda _: None)
    monkeypatch.setattr(runtime_providers, "invoke", invoke)
    monkeypatch.setattr(RuntimeModelAdapter, "generate", lambda adapter, packet: invoke(adapter.profile, packet,
        authorize=adapter.journal.authorize_send, record=adapter.journal.record))
    native = RuntimeModelAdapter(providers.Profile("codex-cli", "gpt-6.1-sol", "SYNTHETIC_NO_CREDENTIAL", 8192,
        auth_mode="host-login", model_output_ceiling=8192), HostJournal(kernel))
    creative = RuntimeModelAdapter(providers.Profile("deepseek", "deepseek-flash", "SYNTHETIC_NO_CREDENTIAL", 8192),
                                   HostJournal(kernel))
    gateway = AgentGateway(kernel, {"creative": creative, "review": native},
                           material_reader=boards.production_material_reader(kernel))
    register_rolling(kernel, gateway, owner, **params)
    return gateway, owner, wr, calls, diagnoses


def current_board(kernel, wr):
    with kernel.db.uow(write=False) as repo:
        return boards._load(kernel, repo, wr["work_id"], wr["branch_id"])


def drive(kernel, gateway, owner, wr, *, until=None, ticks=120):
    previous = None
    unchanged = 0
    results = []
    for _ in range(ticks):
        # Recreate the supervisor each tick to exercise durable state only.
        results.append(RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"]))
        board = current_board(kernel, wr)
        cursor = rolling_status(kernel, owner, wr["work_id"], wr["branch_id"])
        if cursor["state"] == "FROZEN" or (until and until(board)):
            return board, cursor
        snapshot = [(key, node["state"], node.get("reason"), node.get("task_id")) for key, node in board["nodes"].items()]
        unchanged = unchanged + 1 if snapshot == previous else 0
        previous = snapshot
        if unchanged >= 5:
            failures = [{key: result[key] for key in ("status", "reason", "task_id") if key in result}
                        for result in results if result.get("status") in {"BLOCKED", "FAILED"}]
            pytest.fail(f"Synthetic production stopped progressing: {snapshot!r}; cursor={cursor['state']}/{cursor['reason']}; failures={failures!r}")
    pytest.fail(f"Durable synthetic production exceeded its tick bound: {previous!r}")


@pytest.mark.parametrize("editorial", [False, True], ids=["extent-plateau", "editorial-recurrence"])
def test_codex_changes_local_method_then_flash_and_independent_review_freeze(kernel_env, monkeypatch, editorial):
    kernel = kernel_env["kernel"]
    lengths = None if editorial else [1, 5, 3, 2, 7]
    gateway, owner, wr, calls, diagnoses = synthetic_production(kernel, monkeypatch, lengths=lengths, editorial=editorial)
    board, cursor = drive(kernel, gateway, owner, wr)
    assert cursor["state"] == "FROZEN" and cursor["frozen_chapters"] == 1
    writer = next(n for n in board["nodes"].values() if n["stage"] == "draft")
    assert len(diagnoses) == len(writer["reassessments"]) == 1
    diagnosis_task = next(i for i, _, _ in calls if i["role_id"] == "chief_editor")
    writer_tasks = [i for i, _, _ in calls if i["role_id"] == "writer"]
    reviewer_tasks = [i for i, _, _ in calls if i["role_id"] == "reviewer"]
    assert all(t["task_id"] != diagnosis_task["task_id"] for t in writer_tasks + reviewer_tasks)
    assert all(kind == "deepseek" for i, _, kind in calls if i["role_id"] == "writer")
    entry = writer["reassessments"][0]
    with kernel.db.uow(write=False) as repo:
        record = kernel.artifact(repo, entry["strategy_ref"], wr["work_id"], kind="PRODUCTION_REASSESSMENT")["payload"]
        assert record["outcome"] == "STRATEGY_READY" and record["reusable"] is False
        task = repo.one("tasks", {"task_id": diagnosis_task["task_id"]})
        host = repo.one("host_turns", {"attempt_id": record["attempt_id"]})
        assert task["state"] == "ACCEPTED" and host["phase"] == "COMPLETED"
        assert record["request"]["source_draft_ref"] == writer["prior_draft_ref"]
    before = len(calls)
    for _ in range(4):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before and len(diagnoses) == 1


@pytest.mark.parametrize("diagnosis", ["repeat", "repeat_after_first", "needs_input"])
def test_diagnosis_without_new_solution_stops_without_more_paid_work(kernel_env, monkeypatch, diagnosis):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, diagnoses = synthetic_production(kernel, monkeypatch,
        lengths=[1, 5, 3, 2, 2], diagnosis=diagnosis)
    board, cursor = drive(kernel, gateway, owner, wr,
        until=lambda b: any(n.get("reason") == "REASSESSMENT_BLOCKED" for n in b["nodes"].values()))
    writer = next(n for n in board["nodes"].values() if n["stage"] == "draft")
    assert writer["state"] == "STOPPED" and len(diagnoses) == (2 if diagnosis == "repeat_after_first" else 1)
    assert cursor["frozen_chapters"] == 0
    before = len(calls)
    for _ in range(8):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before


@pytest.mark.parametrize("mode", ["plateau", "improving"])
def test_sustained_extent_failure_has_finite_total_repair_or_diagnosis_allowance(kernel_env, monkeypatch, mode):
    kernel = kernel_env["kernel"]
    target = (100, 101) if mode == "improving" else (6, 10)
    lengths = list(range(1, 15)) if mode == "improving" else [1, 5, 3, 2, 2, 2]
    gateway, owner, wr, calls, diagnoses = synthetic_production(kernel, monkeypatch, lengths=lengths, target=target)
    board, cursor = drive(kernel, gateway, owner, wr,
        until=lambda b: any(n.get("reason") == "REPAIR_BUDGET_EXHAUSTED" for n in b["nodes"].values()))
    writer = next(n for n in board["nodes"].values() if n["stage"] == "draft")
    assert writer["state"] == "STOPPED" and cursor["frozen_chapters"] == 0
    assert len(diagnoses) == (0 if mode == "improving" else 2)
    assert writer["revision_count"] == (12 if mode == "improving" else 5)
    before = len(calls)
    for _ in range(6):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before


def test_strategy_consumer_rejects_source_or_generation_rebinding(kernel_env, monkeypatch):
    from novel_factory.application.production_strategy import consume_strategy
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, diagnoses = synthetic_production(kernel, monkeypatch, lengths=[1, 5, 3, 2, 7])
    board, _ = drive(kernel, gateway, owner, wr,
                     until=lambda b: any(n["state"] == "STRATEGY_READY" for n in b["nodes"].values()))
    node = next(n for n in board["nodes"].values() if n["stage"] == "reassessment")
    target = node["reassessment_request"]["target"]
    for field, replacement in (
        ("generation", 999),
        ("output_ref", board["nodes"][target]["history"][0]["output_ref"]),
        ("repair_allowance", {"revision_baseline": 1, "reassessment_baseline": 0}),
    ):
        stale = deepcopy(board)
        stale["nodes"][target][field] = replacement
        with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as error:
            consume_strategy(kernel, repo, stale)
        assert error.value.code == "STALE_GENERATION"
    assert len(diagnoses) == 1
    before = len(calls)
    restored = current_board(kernel, wr)
    assert restored["nodes"][target]["generation"] == board["nodes"][target]["generation"]
    assert any(n["state"] == "STRATEGY_READY" for n in restored["nodes"].values())
    assert len(calls) == before
