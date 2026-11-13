"""Exercise the scoped deferred-invoice path with a synthetic transport; no live supplier call."""

import importlib
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from agent_runtime import providers
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.postpaid_delivery import require_postpaid_delivery
from novel_factory.application.production_extent import _real_candidate
from novel_factory.application.provider_evidence import reconcile_invoice
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def fake_transport(kernel, monkeypatch, mode, respond):
    profile = providers.Profile(
        kind="deepseek",
        model="deepseek-flash",
        max_output_tokens=2048,
        secret_ref="NF_TEST_SYNTHETIC_KEY",
        defer_invoice=mode != "legacy",
    )
    monkeypatch.setenv("NF_TEST_SYNTHETIC_KEY", "fixture-only")
    monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1")

    requests = []

    def fake_post(**kwargs):
        requests.append(kwargs["request_hash"])
        packet = json.loads(kwargs["payload"]["messages"][1]["content"])
        ctx = packet["context"]
        candidate = respond(packet)
        identity = {
            k: ctx[k]
            for k in (
                "world_ref",
                "task_id",
                "generation",
                "context_bundle_id",
                "actor_id",
                "role_id",
            )
        }
        envelope = {
            "schema_version": "2.1",
            **identity,
            "status": "SUCCEEDED",
            "unresolved_questions": [],
            "payload": candidate,
        }
        answer = {
            "id": "fixture-response-" + str(len(requests)),
            "model": "deepseek-flash",
            "choices": [{"message": {"content": json.dumps(envelope)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 19, "completion_tokens": 8, "prompt_cache_hit_tokens": 5},
        }
        if mode == "wrong_model":
            answer["model"] = "different-model"
        if mode == "missing_usage":
            del answer["usage"]["completion_tokens"]
        window = {"started_at": "2026-01-01T00:00:00Z", "received_at": "2026-01-01T00:00:01Z"}
        return (
            json.dumps(answer).encode(),
            "fixture-receipt-" + str(len(requests)),
            {} if mode == "missing_transport" else window,
        )

    monkeypatch.setattr(providers, "post", fake_post)
    return RuntimeModelAdapter(profile, HostJournal(kernel)), requests


@pytest.mark.parametrize(
    "mode", ["defer", "legacy", "missing_transport", "missing_usage", "wrong_model"]
)
def test_returned_design_pending_invoice(kernel_env, tmp_path, monkeypatch, mode):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("p6_idea")
    kernel = kernel_env["kernel"]

    def design_candidate(packet):
        ctx = packet["context"]
        if ctx["role_id"] == "world_designer":
            candidate = {
                "title": "夹具",
                "protagonist": "画师",
                "place": "城南",
                "storyline": "寻画",
                "world_rules": ["一诺千金"],
                "chapter_goals": ["开启", "兑现"],
            }
        else:
            candidate = {
                "summary": "夹具评阅",
                "issues": [],
                "evidence_refs": [],
                "counterinterpretations": [],
            }
        return candidate

    adapter, requests = fake_transport(kernel, monkeypatch, mode, design_candidate)
    gw = AgentGateway(kernel, {"design": adapter})
    idea = {"idea": "无名画师接到委托。", "genre": "现实", "length": "short"}
    allowance = {
        "adapter_id": "design",
        "reserve_amount": "0.1",
        "total_amount": "5",
        "currency": "USD",
        "total_tokens": 2000000,
        "token_reservation": 100000,
    }
    private = tmp_path / "project"

    def run(action):
        return cli.run_intake(action, kernel, gw, private, idea, allowance)

    enrolled = run("enroll")
    first = run("advance")
    assert first["status"] == "RESULT_STORED" and first["cost_status"] == "UNKNOWN_COST"
    assert len(requests) == 1
    if mode != "defer":
        for _ in range(2):
            with pytest.raises(DomainError) as pending:
                run("advance")
            assert pending.value.code == "CALL_RECONCILIATION_REQUIRED"
        assert len(requests) == 1
        return

    for _ in range(8):
        result = run("advance")
        if result.get("status") == "INITIAL_PLAN_MATERIALIZED":
            break
    assert result["status"] == "INITIAL_PLAN_MATERIALIZED" and len(requests) == 2
    assert run("advance") == result and len(requests) == 2
    work, branch = (enrolled["world_ref"][k] for k in ("work_id", "branch_id"))
    access = cli._load_private(private / "preparation.json")
    owner = kernel.authenticate(access["token"])
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": enrolled["task_id"]})
        audit, _candidate = _real_candidate(CandidateDelivery(kernel, gw), repo, task)
        call = repo.one("call_attempts", {"attempt_id": audit["attempt_id"]})
        budget = repo.one("budgets", {"budget_id": call["budget_id"]})
        assert task["state"] == "ACCEPTED" and call["state"] == "UNKNOWN"
        assert call["actual_tokens"] is None and call["actual_amount"] is None
        assert budget["spent_amount"] == 0 and budget["reserved_amount"] == Decimal("0.2")
        assert call["reserved_tokens"] == 100000 and audit["usage"]["input_tokens"] == 19
        original = [
            gw._read_audit(repo, call["attempt_id"]),
            kernel.artifact(repo, audit["response_ref"], work),
            kernel.artifact(repo, audit["output_ref"], work),
        ]
        for i, field, value in [
            (0, "request_sha256", "0" * 64),
            (0, "provider_receipt", "foreign"),
            (1, "text", "{}"),
            (2, "title", "foreign"),
        ]:
            corrupted = deepcopy(original)
            corrupted[i]["payload"][field] = value
            with pytest.raises(DomainError) as invalid:
                require_postpaid_delivery(kernel, repo, task, call, *corrupted)
            assert invalid.value.code == "CALL_RECONCILIATION_REQUIRED"

    args = {
        "receipt_id": call["provider_receipt"],
        "currency": "USD",
        "amount": "0.010000",
        "tokens": 27,
        "document_sha256": "a" * 64,
    }
    settled = reconcile_invoice(kernel, owner, work, branch, call["attempt_id"], **args)
    assert settled["state"] == "SUCCEEDED"
    assert reconcile_invoice(kernel, owner, work, branch, call["attempt_id"], **args) == settled
    with kernel.db.uow(write=False) as repo:
        budget = repo.one("budgets", {"budget_id": call["budget_id"]})
        assert budget["spent_amount"] == Decimal("0.01") and budget["reserved_amount"] == Decimal(
            "0.1"
        )
        assert len(repo.many("call_attempts", {"task_id": task["task_id"]})) == 1


def test_accepted_postpaid_candidates_advance_to_next_rolling_window(kernel_env, monkeypatch):
    from test_p6_rolling_production import answer, connect, request
    from test_release import setup_batch

    from novel_factory.application.rolling_production import register_rolling, rolling_status

    kernel = kernel_env["kernel"]
    fixture = setup_batch(kernel, count=2, dependent=True)
    wr, owner = fixture["world_ref"], fixture["principal"]
    scope = (owner, wr["work_id"], wr["branch_id"])
    params = request(kernel, fixture, size=1)
    adapter, requests = fake_transport(kernel, monkeypatch, "defer", answer)
    gw = connect(kernel, adapter)
    register_rolling(kernel, gw, owner, **params)
    for _ in range(100):
        RuntimeSupervisor(kernel, gw).run_once(*scope)
        cursor = rolling_status(kernel, *scope)
        if cursor["state"] == "FROZEN" and cursor["frozen_chapters"] == 2:
            break
    assert cursor["frozen_chapters"] == 2 and cursor["window_count"] == 2, cursor
    with kernel.db.uow(write=False) as repo:
        calls = repo.many("call_attempts", {"budget_id": params["budget_id"]})
        assert len(calls) == len(requests) == len(set(requests))
        assert len(calls) > 2 and all(
            c["state"] == "UNKNOWN" and c["actual_amount"] is None for c in calls
        )
        assert all(
            repo.one("tasks", {"task_id": c["task_id"]})["state"] == "ACCEPTED" for c in calls
        )
        budget = repo.one("budgets", {"budget_id": params["budget_id"]})
        assert budget["spent_amount"] == 0 and budget["reserved_amount"] == Decimal("0.10") * len(
            calls
        )
