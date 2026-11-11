"""Immutable audit, independent Streams subscribers, versioned replay and authorization."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis import Redis
from sqlalchemy import text
from test_agent_gateway import setup_gateway

from novel_factory.application.audit_log import LogService, record
from novel_factory.application.mcp_runtime import McpRuntime, ProjectBinding
from novel_factory.application.statistics import StatisticsService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.eventhub import EventHub

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_snapshot_excludes_late_commit_and_rejects_other_work(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    admin = world["principal"]
    work, branch = (world["world_ref"][key] for key in ("work_id", "branch_id"))
    stats = StatisticsService(kernel)
    with kernel.db.uow() as pending:
        record(pending.conn, "system.sampled", work_id=work, branch_id=branch)
        with kernel.db.uow() as committed:
            record(committed.conn, "system.sampled", work_id=work, branch_id=branch)
        first = stats.rebuild(admin, work, branch)
    assert first["processed_events"] == 1
    assert stats.verify_statistics(admin, work, branch, source_cut=first["source_cut"])[
        "consistent"
    ]
    later = stats.verify_statistics(admin, work, branch)
    assert later["difference_count"] == 1 and later["source_events"] == 2
    other = load_fixture(kernel)
    with pytest.raises(DomainError, match="Unavailable statistics cut"):
        stats.read(
            other["principal"],
            other["world_ref"]["work_id"],
            other["world_ref"]["branch_id"],
            source_cut=first["source_cut"],
        )


def test_audit_denial_excludes_private_arguments_and_user_chosen_tool(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    work, branch = (world["world_ref"][key] for key in ("work_id", "branch_id"))
    grant = kernel.issue_grant(
        world["principal"],
        work,
        role_id="resume_operator",
        capabilities=["PROJECT_RESUME"],
        branch_ids=[branch],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    runtime = McpRuntime(kernel, grant["principal"], ProjectBinding(work, branch))
    started, scope = LogService.begin_tool(runtime, "UNREGISTERED", "private-string")
    LogService.finish_tool(runtime, started, scope, "VIEW_DENIED", 3)
    rows = LogService.trace(kernel, world["principal"], work, branch, started)
    assert [r["event_type"] for r in rows] == ["mcp.started", "mcp.finished"]
    assert rows[-1]["error_code"] == "VIEW_DENIED" and str(rows[-1]["parent_span_id"]) == started
    assert "private-string" not in str(rows)
    with pytest.raises(DomainError):
        StatisticsService(kernel).read(grant["principal"], work, branch)
    with pytest.raises(DomainError):
        LogService.trace(kernel, grant["principal"], work, branch, started)


@pytest.mark.skipif(not os.getenv("NF_TEST_REDIS_URL"), reason="requires disposable Redis Streams")
def test_paused_worker_catches_up_then_rebuilds_without_redis(kernel_env):
    item = setup_gateway(kernel_env)
    kernel, world = item["kernel"], item["world"]
    admin = world["principal"]
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    item["gateway"].execute(item["principal"], item["request"])
    redis = Redis.from_url(os.environ["NF_TEST_REDIS_URL"])
    hub = EventHub(kernel.db, redis)
    stats = StatisticsService(kernel)
    # A stopped consumer has no callback. PostgreSQL business operations and their audit continued.
    assert stats.read(admin, work, branch)["processed_events"] == 0
    for _ in range(10):
        if not hub.relay_once(100):
            break
    for _ in range(10):
        if not hub.consume_once(
            "log", "audit-test", LogService.handle, limit=100, reclaim_idle_ms=0
        ):
            break
    for _ in range(10):
        if not hub.relay_once(100):
            break
    for _ in range(10):
        if not hub.consume_once(
            "statistics", "statistics-test", StatisticsService.handle, limit=100, reclaim_idle_ms=0
        ):
            break
    observed = stats.verify_statistics(admin, work, branch)
    assert observed["consistent"] and observed["processed_events"] >= 3, observed
    assert observed["metrics"]["model.fixture.total_tokens"] == 65
    assert observed["metrics"]["model.fixture.cost.USD"] == "0.01"
    assert not any(k.startswith("model.actual_cost") for k in observed["metrics"])
    with kernel.db.uow() as repo:
        msg = repo.conn.scalar(
            text("""SELECT outbox_id FROM nf_outbox WHERE topic='audit.recorded'
            AND payload->>'work_id'=:work LIMIT 1"""),
            {"work": work},
        )
        StatisticsService.handle(repo.conn, SimpleNamespace(event_id=msg))
        StatisticsService.handle(repo.conn, SimpleNamespace(event_id=msg))
        repo.conn.execute(
            text("""UPDATE nf_metric_contributions SET metrics=jsonb_build_object('events',100000,'malformed','invalid-number')
           WHERE event_id=(SELECT event_id FROM nf_audit_events WHERE work_id=:work LIMIT 1)"""),
            {"work": work},
        )
    corrupt = stats.verify_statistics(admin, work, branch, source_cut=observed["source_cut"])
    assert (
        corrupt["difference_count"] == 1
        and corrupt["differences"][0]["differences"]["events"]["incremental"] == 100000
    )
    with kernel.db.uow() as repo:
        repo.conn.execute(
            text(
                "DELETE FROM nf_metric_contributions WHERE event_id IN (SELECT event_id FROM nf_audit_events WHERE work_id=:w)"
            ),
            {"w": work},
        )
    redis.close()
    assert (
        stats.rebuild(admin, work, branch, source_cut=observed["source_cut"])["metrics"]
        == observed["metrics"]
    )
    legacy = stats.rebuild(admin, work, branch, version=1)
    assert legacy["metrics"]["model.fixture.total_tokens"] == 65 and not any(
        k.startswith("duration_ms.") for k in legacy["metrics"]
    )
    assert stats.verify_statistics(admin, work, branch, version=1)["consistent"]
    actor = stats.rebuild(
        admin, work, branch, filters={"role_id": "writer", "model": "p2-gateway-fixture-v1"}
    )
    assert actor["processed_events"] == 1
    assert stats.import_legacy(admin, work, branch) == {
        "model_evidence": 0,
        "world_commits": 0,
        "more_possible": False,
    }
    live = stats.current(admin, work, branch)
    assert live["task_counts"] and live["outstanding"] == []
    experiment = uuid4()
    end = datetime.now(UTC)
    stats.register_experiment(admin, work, branch, experiment, end - timedelta(hours=1), end)
    assert (
        stats.read(admin, work, branch, filters={"experiment_id": experiment})["processed_events"]
        == observed["processed_events"]
    )


def test_cut_gc_and_work_wide_scope(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    admin = world["principal"]
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    stats = StatisticsService(kernel)
    first = stats.read(admin, work, None)
    for i in range(17):
        with kernel.db.uow() as repo:
            record(repo.conn, "system.sampled", work_id=work, branch_id=branch)
        current = stats.read(admin, work, None)
        assert current["source_events"] == i + 1
    with pytest.raises(DomainError, match="Unavailable statistics cut"):
        stats.verify_statistics(admin, work, None, source_cut=first["source_cut"])
    with kernel.db.uow(write=False) as repo:
        assert (
            repo.conn.scalar(
                text("SELECT count(*) FROM nf_metric_cuts WHERE work_id=:w"), {"w": work}
            )
            == 16
        )


def test_archived_world_outbox_rebuilds_only_new_effects(kernel_env):
    from novel_factory.fixtures import seed_sword_history

    kernel = kernel_env["kernel"]
    world = seed_sword_history(kernel, load_fixture(kernel))
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    owner = world["principal"]
    selector = "SELECT outbox_id FROM nf_outbox WHERE topic='world.accepted' AND payload->'world_ref'->>'work_id'=:w"
    with kernel.db.uow() as repo:
        # Isolated fixture represents an archive made before P5.5 was installed.
        repo.conn.execute(
            text(f"DELETE FROM nf_bus_publications WHERE event_id IN ({selector})"), {"w": work}
        )
        repo.conn.execute(
            text(f"DELETE FROM nf_outbox WHERE outbox_id IN ({selector})"), {"w": work}
        )
    stats = StatisticsService(kernel)
    assert stats.import_legacy(owner, work, branch)["world_commits"] == len(world["steps"])
    assert stats.import_legacy(owner, work, branch)["world_commits"] == 0
    metrics = stats.rebuild(owner, work, branch)["metrics"]
    assert metrics["world.accepted_events"] == sum(
        len(step["receipt"]["accepted_event_refs"]) for step in world["steps"]
    )
    assert stats.verify_statistics(owner, work, branch)["consistent"]


def test_selector_usage_and_unverified_host_amount_have_distinct_provenance(kernel_env):
    # A synthetic role-tool response plus the independent main generated candidate.
    # The same artifact writer, task grant, query and rebuilder run on PG17.
    item = setup_gateway(kernel_env)
    kernel, world, gateway = item["kernel"], item["world"], item["gateway"]
    with kernel.db.uow(write=False) as repo:
        binding = gateway._binding(repo, item["principal"], item["request"])
    data = {
        "text": "private",
        "input_tokens": 5,
        "output_tokens": 9,
        "cached_input_tokens": 2,
        "actual_amount": "0.02",
        "currency": "USD",
        "provider_receipt": "fixture",
        "cost_receipt_checked": True,
        "model_configuration": binding["config"],
    }
    gateway._audit_artifact(
        binding, str(uuid4()), "lookup-response", data, "AGENT_ROLE_TOOL_RESPONSE"
    )
    gateway.execute(item["principal"], item["request"])
    stats = StatisticsService(kernel)
    work, branch = (world["world_ref"][key] for key in ("work_id", "branch_id"))
    result = stats.rebuild(world["principal"], work, branch)
    assert result["metrics"]["model.fixture.total_tokens"] == 79
    assert result["metrics"]["model.fixture.calls"] == 2
    assert result["metrics"]["model.fixture.cost.USD"] == "0.03"
    assert result["measures"]["invoiced_supplier_cost"] is None
    from novel_factory.application.statistics import _usage

    source = {
        "usage": data,
        "accounting_state": "SUCCEEDED",
        "provider_receipt": "trusted-host-receipt",
        "model_configuration": {
            "adapter_kind": "TRUSTED_HOST",
            "execution_kind": "REAL_HOST",
            "provider": "organization",
        },
    }
    assert _usage(None, {}, source, 3)["model.host_reported_amount.USD"] == "0.02"
    source["model_configuration"]["endpoint"] = "https://openrouter.ai/api/v1/chat/completions"
    assert _usage(None, {}, source, 3)["model.provider_reported_charge.USD"] == "0.02"
