from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from benchmarks.http_load import loopback_server, run_load, SqlMeasurements
from novel_factory.interfaces.http import create_app
from .conftest import make_client

pytestmark = [pytest.mark.postgres, pytest.mark.integration, pytest.mark.load]


@pytest.mark.parametrize("agents", [10, 50])
def test_real_http_logical_agents_fixed_cut_and_bounded_connections(service_env, service_world, agents, tmp_path):
    clients = [make_client(service_env, service_world) for _ in range(agents)]
    app = create_app(service_env["kernel"])
    db = service_env["db"]
    with db.engine.connect() as connection:
        before = connection.scalar(text("SELECT count(*) FROM nf_revisions"))
    idle_samples = []

    def observed_external_wait():
        idle_samples.append(db.engine.pool.checkedout())
        assert idle_samples[-1] == 0, "modeled Agent waiting must not retain any DB connection"

    with loopback_server(app) as url, SqlMeasurements(db.engine) as measurements:
        result = asyncio.run(run_load(url, clients, rounds=6, idle_observer=observed_external_wait))
        result["database"] = measurements.result(result["duration_seconds"])
    with db.engine.connect() as connection:
        after = connection.scalar(text("SELECT count(*) FROM nf_revisions"))
    result.update({"new_world_revisions": after - before, "idle_wait_checked_out_samples": idle_samples,
                   "admission": app.state.admission.snapshot(),
                   "authentication_admission": app.state.authentication_admission.snapshot(),
                   "configured_connection_budget": app.state.connection_budget.total,
                   "fixture": "r1_sword.initial.D1", "fixture_entities": len(service_world["genesis"]["entities"]),
                   "fixture_accepted_events": 0, "api_processes": 1,
                   "cache_hits": app.state.query_service.cache_hits,
                   "cache_misses": app.state.query_service.cache_misses})
    output = Path(os.environ.get("NF_LOAD_EVIDENCE_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / f"http_agents_{agents}.json").write_text(json.dumps(result, indent=2) + "\n")
    assert result["successful_requests"] == agents * 6, result["failures"][:5]
    assert result["failures"] == []
    assert result["not_emitted"] == 0
    assert result["database"]["peak_checked_out_connections"] <= db.engine.pool.size()
    assert result["database"]["checked_out_at_end"] == 0
    assert result["new_world_revisions"] == 0
    assert len(result["successful_requests_by_agent"]) == agents


def test_open_loop_records_planned_emitted_and_all_outcomes(service_env, service_world, tmp_path):
    clients = [make_client(service_env, service_world) for _ in range(10)]
    app = create_app(service_env["kernel"])
    with loopback_server(app) as url, SqlMeasurements(service_env["db"].engine) as measurements:
        result = asyncio.run(run_load(url, clients, rounds=10, mode="open_loop", arrival_rps=150))
        result["database"] = measurements.result(result["duration_seconds"])
    output = Path(os.environ.get("NF_LOAD_EVIDENCE_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / "http_open_loop.json").write_text(json.dumps(result, indent=2) + "\n")
    assert result["planned_requests"] == result["emitted_requests"] == result["completed_requests"] == 100
    assert not result["status_counts"].get("client_or_correctness_error")
    assert result["database"]["checked_out_at_end"] == 0
    assert set(result["status_counts"]) <= {"200", "429"}, result["failures"][:5]
