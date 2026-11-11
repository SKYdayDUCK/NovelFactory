from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from benchmarks.http_load import loopback_server
from novel_factory.application.queries import WorldQueryService
from novel_factory.contracts.registry import validate_contract
from novel_factory.interfaces.http import create_app, HttpSettings
from .conftest import make_client

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def headers(client):
    return {"Authorization": "Bearer " + client["token"]}


def test_http_reads_same_fixed_world_and_requires_authentication(service_env, service_world):
    client = make_client(service_env, service_world)
    app = create_app(service_env["kernel"])
    with loopback_server(app) as url, httpx.Client(base_url=url, trust_env=False) as http:
        response = http.post("/api/v1/queries/timeline", json=client["request"], headers=headers(client))
        assert response.status_code == 200, response.json()
        assert response.json()["payload"] == client["expected_payload"]
        assert response.json()["read_cut"]["world_ref"] == service_world["world_ref"]
        missing = http.post("/api/v1/queries/timeline", json=client["request"])
        assert missing.status_code == 401
        validate_contract("ErrorEnvelope", missing.json())
        assert missing.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"


def test_http_size_origin_duplicate_json_and_extra_fields_are_rejected(service_env, service_world):
    client = make_client(service_env, service_world)
    app = create_app(service_env["kernel"], settings=HttpSettings(max_body_bytes=2048))
    with loopback_server(app) as url, httpx.Client(base_url=url, trust_env=False) as http:
        oversized = http.post("/api/v1/queries/timeline", content=b"x" * 2049,
                              headers={**headers(client), "Content-Type": "application/json"})
        assert oversized.status_code == 413
        origin = http.post("/api/v1/queries/timeline", json=client["request"],
                           headers={**headers(client), "Origin": "https://untrusted.invalid"})
        assert origin.status_code == 403
        duplicate = http.post("/api/v1/queries/timeline", content='{"secret":"do-not-echo","secret":3}',
                              headers={**headers(client), "Content-Type": "application/json"})
        assert duplicate.status_code == 422
        assert "do-not-echo" not in duplicate.text
        extra = http.post("/api/v1/queries/timeline", json={**client["request"], "actor_id": str(uuid4())}, headers=headers(client))
        assert extra.status_code == 422
        unknown = http.get("/api/v1/not-implemented")
        assert unknown.status_code == 404
        for response in (oversized, origin, duplicate, extra, unknown):
            validate_contract("ErrorEnvelope", response.json())


def test_http_actor_cannot_switch_plane_or_read_another_tasks_context(service_env, service_world):
    actor = make_client(service_env, service_world, actor=True)
    other = make_client(service_env, service_world)
    app = create_app(service_env["kernel"])
    with loopback_server(app) as url, httpx.Client(base_url=url, trust_env=False) as http:
        permitted = http.post("/api/v1/queries/timeline", json=actor["request"], headers=headers(actor))
        assert permitted.status_code == 200, permitted.json()
        for item in permitted.json()["payload"]["state_items"]:
            assert item["answer"]["knowledge_status"] == "UNKNOWN"
            assert item["answer"]["data"] is None
            assert item["answer"]["evidence_refs"] == []
        assert service_world["ids"]["smith"] not in permitted.text
        attempted = deepcopy(actor["request"])
        attempted["knowledge_cut"] = {"plane": "WORLD"}
        forbidden = http.post("/api/v1/queries/timeline", json=attempted, headers=headers(actor))
        assert forbidden.status_code == 403
        cross_task = http.post("/api/v1/queries/timeline", json=other["request"], headers=headers(actor))
        assert cross_task.status_code == 403


def test_http_cached_response_does_not_survive_revocation(service_env, service_world):
    client = make_client(service_env, service_world)
    app = create_app(service_env["kernel"])
    with loopback_server(app) as url, httpx.Client(base_url=url, trust_env=False) as http:
        for _ in range(2):
            response = http.post("/api/v1/queries/timeline", json=client["request"], headers=headers(client))
            assert response.status_code == 200
        assert app.state.query_service.cache_hits >= 1
        service_env["kernel"].revoke_grant(service_world["principal"], client["principal"].grant_id)
        revoked = http.post("/api/v1/queries/timeline", json=client["request"], headers=headers(client))
        assert revoked.status_code == 401
        assert revoked.json()["code"] == "UNAUTHENTICATED"
        assert service_world["ids"]["smith"] not in revoked.text


def test_http_real_task_cancellation_blocks_late_result(service_env, service_world):
    client = make_client(service_env, service_world)
    app = create_app(service_env["kernel"])
    task = client["task"]
    bundle = task["compiled"]["compiled"]["bundle"]
    result = {"schema_version": "2.0", "task_id": task["task_id"], "generation": task["generation"],
        "attempt_id": task["attempt_id"], "result_id": str(uuid4()), "context_bundle_id": bundle["context_bundle_id"],
        "dependency_fingerprint": bundle["dependency_fingerprint"], "status": "SUCCEEDED", "artifacts": [],
        "safe_summary": "A cancelled HTTP worker cannot store this result."}
    with loopback_server(app) as url, httpx.Client(base_url=url, trust_env=False) as http:
        read = http.get("/api/v1/tasks/" + task["task_id"], headers=headers(client))
        assert read.status_code == 200, read.json()
        assert isinstance(read.json()["lease_expiry"], str)
        cancelled = http.post("/api/v1/tasks/cancel", headers=headers(client), json={
            "task_id": task["task_id"], "generation": task["generation"], "idempotency_key": "http-cancel-1"})
        assert cancelled.status_code == 200, cancelled.json()
        late = http.post("/api/v1/tasks/results", headers=headers(client), json={
            "result": result, "worker_id": task["worker_id"]})
        assert late.status_code == 409, late.json()
        assert late.json()["code"] in {"STALE_GENERATION", "TASK_NOT_ACCEPTABLE"}
        current = http.get("/api/v1/tasks/" + task["task_id"], headers=headers(client))
        assert current.status_code == 200 and current.json()["state"] == "CANCELLED"


def test_http_rate_limit_is_explicit(service_env, service_world):
    client = make_client(service_env, service_world)
    app = create_app(service_env["kernel"], settings=HttpSettings(rate_per_second=.01, rate_burst=1))
    with loopback_server(app) as url, httpx.Client(base_url=url, trust_env=False) as http:
        assert http.post("/api/v1/queries/timeline", json=client["request"], headers=headers(client)).status_code == 200
        limited = http.post("/api/v1/queries/timeline", json=client["request"], headers=headers(client))
        assert limited.status_code == 429
        assert limited.json()["code"] == "RATE_LIMITED"
        assert limited.headers["retry-after"] == "1"


def test_http_backpressure_does_not_acquire_connections_for_waiting_work(service_env, service_world):
    client = make_client(service_env, service_world)
    ready, proceed = threading.Event(), threading.Event()
    counter, lock = [0], threading.Lock()

    class DelayedQuery(WorldQueryService):
        def query(self, principal, request):
            with lock:
                counter[0] += 1
                if counter[0] == 2:
                    ready.set()
            assert proceed.wait(5)
            return super().query(principal, request)

    app = create_app(service_env["kernel"], query_service=DelayedQuery(service_env["kernel"]),
        settings=HttpSettings(max_queued_operations=0, per_principal_concurrency=2))

    async def scenario(url):
        async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=10) as http:
            first = asyncio.create_task(http.post("/api/v1/queries/timeline", json=client["request"], headers=headers(client)))
            second = asyncio.create_task(http.post("/api/v1/queries/timeline", json=client["request"], headers=headers(client)))
            try:
                assert await asyncio.to_thread(ready.wait, 3)
                assert service_env["db"].engine.pool.checkedout() == 0
                refused = await http.post("/api/v1/queries/timeline", json=client["request"], headers=headers(client))
                assert refused.status_code == 429
                assert refused.json()["code"] == "BACKPRESSURE"
            finally:
                proceed.set()
            responses = await asyncio.gather(first, second)
            assert [response.status_code for response in responses] == [200, 200]

    with loopback_server(app) as url:
        asyncio.run(scenario(url))
    assert app.state.admission.snapshot()["active"] == 0


def test_http_database_errors_do_not_echo_sql_credentials_or_hidden_values(service_env, service_world, monkeypatch):
    client = make_client(service_env, service_world)
    kernel = service_env["kernel"]
    app = create_app(kernel)

    def failed_authentication(token):
        raise OperationalError("secret-sql", {"password": "must-not-echo"}, RuntimeError("private-db-host"))

    monkeypatch.setattr(kernel, "authenticate", failed_authentication)
    with loopback_server(app) as url, httpx.Client(base_url=url, trust_env=False) as http:
        failed = http.post("/api/v1/queries/timeline", json=client["request"], headers=headers(client))
        assert failed.status_code == 503
        validate_contract("ErrorEnvelope", failed.json())
        for secret in ("secret-sql", "must-not-echo", "private-db-host", client["token"]):
            assert secret not in failed.text


@pytest.mark.concurrency
def test_two_independent_http_processes_share_task_and_revocation_state(service_env, service_world, tmp_path):
    """No in-process Principal/cache can keep a revoked grant alive in another API process."""
    client = make_client(service_env, service_world)
    processes, logs, urls = [], [], []
    env = {**os.environ, "NF_DATABASE_URL": service_env["url"],
           "NF_OBJECT_ROOT": str(service_env["kernel"].objects.root),
           "NF_POOL_SIZE": "4", "NF_POOL_OVERFLOW": "0", "NF_API_PROCESSES": "2",
           "NF_WORKER_PROCESSES": "0"}
    try:
        for index in range(2):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            stream = (tmp_path / f"api_{index}.log").open("w")
            logs.append(stream)
            process = subprocess.Popen([sys.executable, "-m", "uvicorn",
                "novel_factory.interfaces.http:from_environment", "--factory", "--host", "127.0.0.1",
                "--port", str(port), "--no-access-log", "--log-level", "warning"],
                env=env, stdout=stream, stderr=subprocess.STDOUT)
            processes.append(process)
            url = f"http://127.0.0.1:{port}"
            urls.append(url)
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, (tmp_path / f"api_{index}.log").read_text()
                try:
                    with httpx.Client(trust_env=False, timeout=.5) as http:
                        if http.get(url + "/health").status_code == 200:
                            break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "API process startup deadline"
                time.sleep(.02)
        assert processes[0].pid != processes[1].pid
        responses = []
        with httpx.Client(trust_env=False) as http:
            for url in urls:
                response = http.post(url + "/api/v1/queries/timeline", json=client["request"], headers=headers(client))
                assert response.status_code == 200, response.json()
                assert response.json()["payload"] == client["expected_payload"]
                responses.append(response.status_code)
            service_env["kernel"].revoke_grant(service_world["principal"], client["principal"].grant_id)
            for url in urls:
                response = http.post(url + "/api/v1/queries/timeline", json=client["request"], headers=headers(client))
                assert response.status_code == 401
                responses.append(response.status_code)
        output = Path(os.environ.get("NF_LOAD_EVIDENCE_DIR", tmp_path))
        output.mkdir(parents=True, exist_ok=True)
        (output / "two_api_processes.json").write_text(json.dumps({
            "api_process_ids": [process.pid for process in processes],
            "response_statuses": responses, "api_pool_size_each": 4,
            "api_connection_budget_sum": 8, "transport": "real_loopback_tcp",
            "shared_state": "PostgreSQL task and grant; independent API process caches"}, indent=2) + "\n")
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(timeout=10)
        for stream in logs:
            stream.close()


def test_http_query_checks_its_full_contract_once_and_keeps_direct_call_validation(service_env, service_world, monkeypatch):
    from novel_factory.application import queries
    from novel_factory.domain.errors import DomainError
    from novel_factory.interfaces import http as transport

    client = make_client(service_env, service_world)
    validations = []

    def audited(name, value, version='2.0'):
        if name == 'UnifiedQueryRequest':
            validations.append(name)
        return validate_contract(name, value, version)

    monkeypatch.setattr(queries, 'validate_contract', audited)
    monkeypatch.setattr(transport, 'validate_contract', audited)
    app = create_app(service_env['kernel'])
    with loopback_server(app) as url, httpx.Client(base_url=url, trust_env=False) as http:
        response = http.post('/api/v1/queries/timeline', json=client['request'], headers=headers(client))
        assert response.status_code == 200
        assert response.json()['payload'] == client['expected_payload']
        assert len(validations) == 1
        invalid = {**client['request'], 'extra_field': True}
        # Preserve request-schema rejection before bearer authentication.
        assert http.post('/api/v1/queries/timeline', json=invalid).status_code == 422

    with pytest.raises(DomainError) as failure:
        app.state.query_service.query(None, invalid)
    assert failure.value.code == 'INVALID_ARGUMENT'
    source = deepcopy(client['request'])
    prepared = queries.PreparedWorldQuery(source)
    source['query'].clear()
    first = prepared.materialize()
    first['query'].clear()
    assert prepared.materialize() == client['request']
