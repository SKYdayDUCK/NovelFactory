"""Server-timed, tenant-scoped observations from actual compiled bundles and model calls."""

import json
from datetime import timedelta

import pytest

from novel_factory.application.audit_log import LogService
from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.infrastructure.model_gateway import ModelCallFailure
from test_agent_gateway import FixtureAdapter, setup_gateway
from test_harness_context import EnvelopeFixture
from test_harness_planning_simulation import scene  # noqa: F401


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_native_compile_packet_and_attempt_have_scoped_content_free_trace(kernel_env, monkeypatch):
    monkeypatch.setenv(
        "NF_CREATIVE_HARNESS_CONFIG", json.dumps(CreativeHarnessConfig(enabled=True).as_dict())
    )
    site = setup_gateway(kernel_env, EnvelopeFixture())
    kernel = site["kernel"]
    result = site["gateway"].execute(site["principal"], site["request"])
    task = site["task"]
    world = site["world"]
    wr = world["world_ref"]

    def rows():
        return LogService.trace(kernel, world["principal"], wr["work_id"], wr["branch_id"], task["task_id"])

    first = rows()
    by_name = {row["event_type"]: row for row in first}
    native = by_name["context.compiled"]
    packet = by_name["harness.context.packet"]
    model = by_name["model.completed"]
    with kernel.db.uow(write=False) as repo:
        scoped = repo.one("artifacts", {"artifact_revision_id": packet["evidence_id"]})["payload"]
        compiled_ref = repo.one("contexts", {"context_bundle_id": native["evidence_id"]})["artifact_ref"]
        compiled = kernel.artifact(repo, compiled_ref, wr["work_id"])["payload"]
    assert native["duration_ms"] > 0 and packet["duration_ms"] > 0
    assert native["harness_details"]["packet_bytes"] == len(canonical_bytes(compiled))
    details = packet["harness_details"]
    assert details["packet_bytes"] == len(canonical_bytes(scoped["model_input"]))
    assert details["source_count"] == len(scoped["packet"]["sources"])
    assert details["omitted_count"] == len(scoped["packet"]["omissions"])
    assert details["switches"] == {
        "enabled": True,
        "dynamic_agents": False,
        "adaptive_instructions": False,
        "auto_promote": False,
    }
    assert details["role_version"] and details["skill_version"] and details["runtime_sha256"]
    assert (
        str(model["attempt_id"]) == result["attempt_id"]
        and model["harness_details"]["input_tokens"] is not None
    )
    assert model["harness_details"]["cost_status"] == "KNOWN"
    with kernel.db.uow(write=False) as repo:
        evidence = kernel.artifact(repo, result["evidence_ref"], wr["work_id"])["payload"]
    assert model["harness_details"]["prompt_ref"] == evidence["prompt_ref"]["artifact_revision_id"]
    assert model["harness_details"]["prompt_sha256"] == evidence["prompt_ref"]["sha256"]
    assert all(
        str(r["world_revision_id"]) == wr["world_revision_id"] and r["generation"] == task["generation"]
        for r in (native, packet, model)
    )
    assert site["gateway"].execute(site["principal"], site["request"])["attempt_id"] == result["attempt_id"]
    assert [r["event_id"] for r in rows()] == [r["event_id"] for r in first]
    protected = json.dumps(first, ensure_ascii=False, default=str)
    assert scoped["model_input"]["assignment"] not in protected
    for key in ("model_input", "role_contract", "skill_instructions", "statement", "messages"):
        assert '"' + key + '"' not in protected
    with pytest.raises(DomainError):
        LogService.trace(kernel, site["principal"], wr["work_id"], wr["branch_id"], task["task_id"])


def test_unknown_attempt_and_expired_lease_keep_correlated_sanitized_error(kernel_env):
    def uncertain(_):
        raise ModelCallFailure("private-provider-diagnostic-CANARY")

    site = setup_gateway(kernel_env, FixtureAdapter(uncertain))
    kernel, world, task = site["kernel"], site["world"], site["task"]
    result = site["gateway"].execute(site["principal"], site["request"])
    assert result["status"] == "BLOCKED" and result["cost_status"] == "UNKNOWN_COST"
    with kernel.db.uow() as repo:
        repo.change(
            "tasks", {"task_id": task["task_id"]}, {"lease_expiry": repo.now() - timedelta(seconds=1)}
        )
    kernel.tasks.recover_expired()
    wr = world["world_ref"]
    rows = LogService.trace(kernel, world["principal"], wr["work_id"], wr["branch_id"], task["task_id"])
    error = next(r for r in rows if r["event_type"] == "model.completed")
    assert error["error_code"] == "OTHER_MODEL_ERROR"
    assert (
        error["harness_details"]["actual_amount"] is None and error["harness_details"]["input_tokens"] is None
    )
    fenced = next(r for r in rows if r["event_type"] == "harness.task.recovered")
    assert fenced["outcome"] == "PAUSED" and fenced["generation"] == error["generation"] + 1
    assert (
        fenced["harness_details"]["transition"] == "LEASE_RECOVERY"
        and fenced["harness_details"]["unresolved_attempts"] == 1
    )
    assert "private-provider-diagnostic-CANARY" not in json.dumps(rows, default=str)


def test_simulation_logs_branch_limits_and_planning_choice_without_actor_facts(scene):
    from test_harness_planning_simulation import propose, simulate, decide

    candidate = propose(scene)
    run = simulate(scene, candidate, branches=1, steps=1)
    decide(scene, candidate, None, "observe-decline")
    wr = scene["wr"]
    trace = LogService.trace(
        scene["kernel"], scene["owner"], wr["work_id"], wr["branch_id"], scene["planner"].task["task_id"]
    )
    named = {row["event_type"]: row for row in trace}
    metrics = named["harness.simulation.completed"]["harness_details"]
    assert named["harness.plan.candidate"]["harness_details"]["candidate_count"] == len(
        candidate["candidate"]["alternatives"]
    )
    assert (
        metrics["step_count"] == sum(s["steps"] for s in run["stops"])
        and metrics["stop_reasons"]["BRANCH_LIMIT"] >= 1
    )
    assert metrics["model_attempts"] == 0
    assert named["harness.plan.decision"]["outcome"] == "DECLINED"
    evidence = json.dumps(trace, default=str)
    for field in ("path", "expected_value", "assumptions", "beliefs"):
        assert '"' + field + '"' not in evidence
