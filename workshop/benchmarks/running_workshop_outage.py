"""AT-49. Physically stop Workshop during a confirmed in-flight NF provider request."""
import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from threading import Event
from uuid import uuid4

from mcp import StdioServerParameters
import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import make_url

from cross_service_scene import AW, NF, backend, dual_databases, kernel_env, private_file
from test_p5_supervisor import _child
from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import reconcile, remote
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point


@contextmanager
def workshop_process(binding):
    child = subprocess.Popen([sys.executable, "-m", "agent_workshop.server", "--binding", binding],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "PYTHONPATH": str(AW / "src")})
    def send(message):
        child.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        child.stdin.flush()
        if "id" in message:
            assert select.select([child.stdout], [], [], 15)[0], "Workshop stdio deadline"
            response = json.loads(child.stdout.readline())
            assert response.get("id") == message["id"] and "error" not in response, response
            return response["result"]
    try:
        hello = send({"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "at49", "version": "1"}}})
        assert hello["serverInfo"]["name"] == "AgentWorkshop"
        send({"method": "notifications/initialized"})
        def call(tool, args):
            response = send({"id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
            assert not response.get("isError"), response
            return response["structuredContent"]
        yield child, call
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()


def test_workshop_loss_after_nf_provider_send_preserves_single_execution(kernel_env, dual_databases, tmp_path):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner, wr = world["principal"], world["world_ref"]
    org, instance = str(uuid4()), str(uuid4())
    aw_url = dual_databases["aw"]
    grant = install(kernel, owner, nf_instance_id=instance, org_id=org, work_id=wr["work_id"],
                    branch_id=wr["branch_id"], expires_at=datetime.now(UTC) + timedelta(hours=1))
    inst = grant["installation_id"]
    # The test administrator can disconnect only the unique disposable AW database.
    aw_name = make_url(aw_url).database
    assert aw_name.startswith("aw_test_") and aw_name != make_url(dual_databases["nf"]).database
    aw_ident = sql.Identifier(aw_name)
    with backend() as (adapter, requests, provider), psycopg.connect(
            os.environ["AW_DUAL_TEST_ADMIN_URL"], autocommit=True) as admin:
        provider.started, provider.release = Event(), Event()
        gateway = AgentGateway(kernel, {"test": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        frozen = RuntimeTaskService(kernel, gateway).define(
            owner, wr, role_id="evidence_maintainer", task_kind="summary", story_time=point(1),
            subject_ids=[world["ids"]["sword"]], predicates=["owner"], assignment="Summarize the trusted view.",
            adapter_id="test", max_amount="0.10", token_budget=100000)
        scope = "at49-summary"
        provision_slot(supervisor, owner, inst, scope_ref=scope, task_template_id="summary.v1", frozen=frozen,
                       budget_id=kernel.tasks.create_budget(wr["work_id"], "1"), reserve_amount="0.10",
                       token_reservation=100000, accept_summary=True)
        policy = dict(nf_instance_id=instance, work_id=wr["work_id"], branch_id=wr["branch_id"], generation=0,
                      grant_epoch=grant["grant_epoch"], scope_ref=scope, task_template_id="summary.v1",
                      actor_binding_ref=None, role_id="evidence_maintainer", max_amount="0.10", currency="USD", token_limit=100000)
        with connect(aw_url) as conn:
            conn.execute("INSERT INTO aw_installations(installation_id,nf_instance_id) VALUES (%s,%s)", (inst, instance))
            bootstrap(conn, org, "Long-form", installation_id=inst)
            conn.execute("INSERT INTO aw_budget_accounts VALUES (%s,1,'USD')", (org,))
            allow_slot(conn, org, inst, scope, "memory", policy)
            token = enroll(conn, inst, org, "chief", 1, 1)
        binding = private_file(tmp_path / "aw-binding.json", {"database_url": aw_url, "token": token})
        nf_binding = private_file(tmp_path / "nf-binding.json", {"database_url": dual_databases["nf"],
                                  "installation_id": inst, "object_root": str(kernel_env["store"].root), "token": grant["token"]})
        nf = remote(StdioServerParameters(command=sys.executable,
                    args=["-m", "novel_factory.interfaces.delegation_stdio", "--binding", nf_binding],
                    env={"PYTHONPATH": str(NF / "src")}))
        args = dict(operation_id="original-at49", slot_id=scope, max_amount="0.10", token_limit=100000)
        def tick():
            with connect(aw_url) as conn:
                conn.execute("UPDATE aw_delegations SET next_check=clock_timestamp() WHERE operation_id=%s", (op,))
            return asyncio.run(reconcile(aw_url, inst, org, op, nf))
        def aw_receipt():
            with connect(aw_url) as conn:
                rows = conn.execute("""SELECT r.task_id::text,r.receipt_ref::text,r.task_state,r.accounting_state,
                    h.status AS hold,h.settled_amount::text FROM aw_remote_receipts r
                    JOIN aw_budget_holds h USING(org_id,operation_id) WHERE r.operation_id=%s""", (op,)).fetchall()
                assert len(rows) == 1
                return rows[0]
        def nf_state():
            with kernel.db.uow(write=False) as repo:
                task = repo.one("tasks", {"task_id": frozen.spec["task_id"]})
                attempts = repo.many("call_attempts", {"task_id": task["task_id"]})
                return {"task_id": str(task["task_id"]), "generation": task["generation"], "state": task["state"],
                        "attempts": [{"id": str(a["attempt_id"]), "state": a["state"]} for a in attempts]}
        with workshop_process(binding) as (workshop, call):
            op = call("aw_delegate", args)["operation_id"]
            assert tick() == "BOUND"
            before = aw_receipt()
            assert before["task_state"] == "READY" and before["hold"] == "HELD"
            nf_worker = _child(kernel_env, world, adapter)
            try:
                assert provider.started.wait(15) and nf_worker.poll() is None
                active = nf_state()
                assert active["state"] == "RUNNING" and len(active["attempts"]) == 1
                assert active["attempts"][0]["state"] == "SENT"
                attempt = active["attempts"][0]["id"]
                assert workshop.poll() is None and len(requests) == 1
                workshop.kill()
                assert workshop.wait(timeout=5) == -9
                admin.execute(sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(aw_ident))
                try:
                    admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (aw_name,))
                    with pytest.raises(psycopg.OperationalError):
                        with connect(aw_url):
                            pass
                    assert nf_worker.poll() is None and not provider.release.is_set()
                    provider.release.set()
                    assert nf_worker.wait(timeout=20) == 0, nf_worker.stderr.read()
                    produced = json.loads(nf_worker.stdout.read())
                    assert produced["status"] == "RESULT_STORED"
                    supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
                    offline = nf_state()
                    assert offline["state"] == "ACCEPTED"
                    assert offline["attempts"] == [{"id": attempt, "state": "UNKNOWN"}] and len(requests) == 1
                    assert not admin.execute("SELECT datallowconn FROM pg_database WHERE datname=%s", (aw_name,)).fetchone()[0]
                finally:
                    admin.execute(sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(aw_ident))
            finally:
                provider.release.set()
                if nf_worker.poll() is None:
                    nf_worker.kill()
                nf_worker.wait(timeout=5)
        unchanged = aw_receipt()
        assert unchanged == before
        with workshop_process(binding) as (restarted, call):
            assert restarted.pid != workshop.pid and call("aw_delegate", args)["operation_id"] == op
            assert tick() == "BOUND"
            caught_up = aw_receipt()
            assert caught_up["task_state"] == "ACCEPTED" and caught_up["accounting_state"] == "UNKNOWN"
            assert caught_up["hold"] == "HELD"
            kernel.tasks.settle_call(attempt, state="SUCCEEDED", actual_amount="0.02", actual_tokens=18,
                                     provider_receipt="at49-synthetic-invoice-" + org)
            assert tick() == "SETTLED" and call("aw_delegate", args)["operation_id"] == op
            final = aw_receipt()
            assert final["hold"] == "SETTLED" and float(final["settled_amount"]) == 0.02
            assert all(r["task_id"] == before["task_id"] and r["receipt_ref"] == before["receipt_ref"]
                       for r in (unchanged, caught_up, final))
            with connect(aw_url) as conn:
                counts = {table: conn.execute("SELECT count(*) AS n FROM " + table + " WHERE operation_id=%s", (op,)).fetchone()["n"]
                          for table in ("aw_delegations", "aw_budget_holds", "aw_remote_receipts", "aw_cost_observations")}
                observations = conn.execute("""SELECT attempt_id::text,state FROM aw_cost_observations
                    WHERE operation_id=%s ORDER BY state""", (op,)).fetchall()
            assert counts == {"aw_delegations": 1, "aw_budget_holds": 1, "aw_remote_receipts": 1,
                              "aw_cost_observations": 2}, counts
            assert observations == [{"attempt_id": attempt, "state": "SUCCEEDED"}, {"attempt_id": attempt, "state": "UNKNOWN"}]
            assert len(requests) == 1
            assert nf_state()["attempts"] == [{"id": attempt, "state": "SUCCEEDED"}]
        out = Path(os.environ["AW_ACCEPTANCE_OUTPUT"])
        out.mkdir(exist_ok=True, parents=True)
        (out / "at49.json").write_text(json.dumps({
            "commercial_calls": 0, "op": op, "workshop_pid": workshop.pid, "workshop_exit": workshop.returncode,
            "restored_workshop_pid": restarted.pid, "nf_provider_worker_pid": nf_worker.pid, "workshop_db": aw_name,
            "nf_db": make_url(dual_databases["nf"]).database, "workshop_db_rejected_connections_until_nf_accepted": True,
            "initial_aw": before, "nf_at_signal": active, "nf_while_aw_offline": offline,
            "aw_before_reconcile": unchanged, "aw_after_reconcile": caught_up, "settled_aw": final,
            "nf_final": nf_state(), "aw_rows": counts, "immutable_cost_history": observations, "loopback_requests": len(requests)
        }, indent=2) + "\n")
