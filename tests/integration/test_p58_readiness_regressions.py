"""P5.7 regression cases on real ledger and reader transactions with synthetic inference."""
from decimal import Decimal

import pytest
from test_harness_review_workflow import _args, _reviewer, manuscript  # noqa: F401
from test_p5_actor_tool_choice import ToolFixture

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.creative_metrics import CreativeMetricsService
from novel_factory.application.creative_review import CreativeReviewService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.codex_host import CodexRoleLookupAdapter

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.parametrize("layer", ["PLOT", "STAGING"])
@pytest.mark.parametrize("position", [None, "NO_OPINION", "NO_ISSUES"])
def test_explicit_literary_reader_position(manuscript, tmp_path, monkeypatch, layer, position):
    k, f, draft = manuscript
    job, args, _ = _reviewer(k, f, draft, tmp_path, monkeypatch)
    if position:
        args["reader_position"] = position
    result = CreativeReviewService(k).review(*_args(job), **{**args, "layer": layer})
    clear = position == "NO_ISSUES"
    assert result["report"]["recommendation"] == ("ACCEPT_CANDIDATE" if clear else "INSUFFICIENT_EVIDENCE")
    with k.db.uow(write=False) as repo:
        native = k.artifact(repo, result["native_review_ref"], f["world_ref"]["work_id"])["payload"]
        details = k.artifact(repo, result["details_ref"], f["world_ref"]["work_id"])["payload"]
    assert native["submission"]["outcome"] == ("PASS" if clear else "UNDETERMINED")
    assert details["reader_position"] == (position or "UNSTATED")


@pytest.mark.parametrize("unknown", [False, True])
def test_scoped_first_stage_usage_is_counted_once(kernel_env, monkeypatch, unknown):
    k = kernel_env["kernel"]
    w = load_fixture(k)
    wr, owner, actor = w["world_ref"], w["principal"], w["ids"]["protagonist"]
    fixture = ToolFixture("unknown-first" if unknown else "skip", w["ids"]["sword"])
    # Replace the host/transport only. All task, input, attempt and settlement paths are native.
    route = CodexRoleLookupAdapter(fixture, HostJournal(k), binary="unused", relay=None, context_reader=None)
    monkeypatch.setattr(route, "check_available", lambda: None)
    monkeypatch.setattr(route, "generate", fixture.generate)
    gw = AgentGateway(k, {"codex-role-tools": route})
    supervisor = RuntimeSupervisor(k, gw)
    job = RuntimeTaskService(k, gw).prepare(owner, wr, role_id="character_actor",
        task_kind="character_action", actor_id=actor, story_time=point(1),
        subject_ids=[w["ids"]["sword"]], predicates=["owner"], assignment="Read or wait",
        adapter_id="codex-role-tools", queue_state="READY")
    supervisor.enroll(owner, job.task["task_id"], manifest_ref=job.manifest_ref,
        input_ref=job.agent_input["input_ref"],
        knowledge_cut={"plane":"BELIEF","actor_id":actor,"known_at":point(1)},
        budget_id=k.tasks.create_budget(wr["work_id"], "2"), reserve_amount="1",
        context_tokens=20000, token_reservation=100000)
    supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
    report = CreativeMetricsService(k).report(owner, wr)
    row, = report["scenes"]
    assert row["execution_origin"] in {"TEST_FIXTURE", "LOOPBACK_OR_TEST_ENDPOINT"}
    assert row["attempts"] == len(fixture.calls) == (1 if unknown else 2)
    assert row["known_tokens"] == (29 if unknown else 58)
    assert row["tokens_from_unsettled_provider_receipts"] == int(unknown)
    if unknown:
        assert row["actual_amount"] is None
    else:
        assert Decimal(row["actual_amount"]) == Decimal(".10")
    assert sum(group["n"] for group in report["latency"]["model_call_ms_by_execution_kind"].values()) == len(fixture.calls)
