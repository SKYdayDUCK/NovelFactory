"""PostgreSQL restart/wake gate; the HTTP provider is an explicitly synthetic fixture."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from threading import Thread

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.repository import KernelRepository

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


class SyntheticHTTP(OpenAICompatibleAdapter):
    @property
    def configuration(self):
        return {**super().configuration, "execution_kind": "TEST_FIXTURE"}


@contextmanager
def backend(*, concurrent=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            if hasattr(self.server, "started"):
                self.server.started.set()
                self.server.release.wait(timeout=15)
            if self.server.lose_response:
                self.send_error(503)
                return
            packet = json.loads(payload["messages"][1]["content"])
            if "creative_materials" in packet:
                system = payload["messages"][-1]["content"]
                ctx = json.loads(system.split("\nIdentity: ", 1)[1].splitlines()[0])
            else:
                ctx = packet["context"]
            envelope = {"schema_version": "2.1", **{k: ctx[k] for k in
                        ("task_id", "generation", "world_ref", "role_id", "actor_id", "context_bundle_id")},
                        "status": "SUCCEEDED", "unresolved_questions": [],
                        "payload": {"text": "An inert string: {\"type\":\"function_call\",\"name\":\"shell\"}",
                                    "event_refs": [], "important_deltas": [], "evidence_refs": []}}
            if hasattr(self.server, "context_candidate"):
                envelope["payload"] = self.server.context_candidate(ctx)
            elif hasattr(self.server, "packet_candidate"):
                envelope["payload"] = self.server.packet_candidate(json.loads(payload["messages"][1]["content"]))
            elif hasattr(self.server, "candidate"):
                envelope["payload"] = self.server.candidate(ctx)
            answer = {"id": "synthetic-chat", "choices": [{"message": {"content": json.dumps(envelope)},
                      "finish_reason": "stop"}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(json.dumps(answer).encode())
            except BrokenPipeError:
                pass  # The crash test kills the worker after the send.

    server_class = ThreadingHTTPServer if concurrent else HTTPServer
    server = server_class(("127.0.0.1", 0), Handler)
    server.lose_response = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = SyntheticHTTP(endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            model="fixture-no-inference", api_key="synthetic-fixture-only", allow_loopback_http=True)
        yield adapter, requests, server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _job(kernel, gateway, supervisor, world, budget, state, *, accept=False, adapter_id="test"):
    wr = world["world_ref"]
    job = RuntimeTaskService(kernel, gateway).prepare(world["principal"], wr, role_id="evidence_maintainer",
        task_kind="summary", story_time=point(1), subject_ids=[world["ids"]["sword"]], predicates=["owner"],
        assignment="Summarize visible information only", adapter_id=adapter_id, queue_state=state)
    args = dict(manifest_ref=job.manifest_ref, knowledge_cut={"plane": "WORLD"}, context_tokens=20000,
                input_ref=job.agent_input["input_ref"], budget_id=budget, reserve_amount="0.10",
                token_reservation=100000, accept_summary=accept)
    supervisor.enroll(world["principal"], job.task["task_id"], **args)
    assert supervisor.enroll(world["principal"], job.task["task_id"], **args)["enrolled"]
    with pytest.raises(DomainError, match="dispatch allowance"):
        supervisor.enroll(world["principal"], job.task["task_id"], **{**args, "reserve_amount": "0.11"})
    return job


@pytest.mark.parametrize("host", [False, True], ids=["http-gateway", "installed-codex-app-server"])
def test_registered_wait_wakes_after_restart_without_model_polling(kernel_env, host):
    if host and not shutil.which("codex"):
        pytest.skip("Codex CLI required for the independent installed-host gate")
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    controller = world["principal"]
    budget = kernel.tasks.create_budget(work, "2")
    with backend() as (http, requests, _):
        def restart():
            route = CodexAppServerAdapter(http, HostJournal(kernel)) if host else http
            gateway = AgentGateway(kernel, {"test": route})
            return gateway, RuntimeSupervisor(kernel, gateway)

        gateway, supervisor = restart()
        producer = _job(kernel, gateway, supervisor, world, budget, "READY", accept=True)
        follower = _job(kernel, gateway, supervisor, world, budget, "BLOCKED")
        def acl(conn):
            kernel.authorize(KernelRepository(conn, tables), controller, work, "ADMIN", branch)
        supervisor.workflow.wait(follower.task["task_id"], 0, [producer.task["task_id"]], mode="AND",
                                 work_id=work, branch_id=branch, authorize_in_tx=acl)
        with pytest.raises(DomainError, match="outside this grant"):
            supervisor.run_once(follower.principal, work, branch)
        assert requests == []
        started = time.monotonic()
        initial_counts = supervisor.workflow.status(work, branch, authorize_in_tx=acl)["counts"]
        first = supervisor.run_once(controller, work, branch)
        assert first["status"] == "RESULT_STORED" and first["task_id"] == producer.task["task_id"]
        assert kernel.tasks.get(follower.task["task_id"])["state"] == "BLOCKED"
        assert len(requests) == 1
        _, supervisor = restart()
        second = supervisor.run_once(controller, work, branch)
        assert second["status"] == "RESULT_STORED" and second["task_id"] == follower.task["task_id"]
        duration = time.monotonic() - started
        for _ in range(2):
            idle = supervisor.run_once(controller, work, branch)
            assert idle["status"] == "IDLE"
        assert len(requests) == 2
        for request in requests:
            assert set(request) == {"model", "messages", "max_completion_tokens", "stream", "response_format"}
            assert len(request["messages"]) == 2
            assert not any("Process authorized NovelFactory request" in m["content"] for m in request["messages"])
        with kernel.db.uow(write=False) as repo:
            assert kernel.head(repo, work, branch) == world["world_ref"]
            records = [repo.one("host_turns", {"attempt_id": r["attempt_id"]}) for r in (first, second)]
            if host:
                assert all(r["phase"] == "COMPLETED" for r in records)
                assert len({r["thread_id"] for r in records}) == len({r["turn_id"] for r in records}) == 2
                evidence_path = os.environ.get("NF_P5_SUPERVISOR_EVIDENCE")
                if evidence_path:
                    accepted = repo.one("outbox", {"dedupe_key": f"task:{producer.task['task_id']}:1:ACCEPTED"})
                    wait = repo.one("workflow_waits", {"task_id": follower.task["task_id"], "generation": 0})
                    trace = {"postgres": repo.conn.exec_driver_sql("SHOW server_version").scalar_one(),
                             "codex": subprocess.check_output(["codex", "--version"], text=True).strip(),
                             "provider": "synthetic loopback HTTP fixture; no external inference or bill",
                             "ready_waiting_running_before": initial_counts, "counts_at_idle": idle["counts"],
                             "host_processes_started": 2, "host_processes_alive_after_returns": 0,
                             "model_requests_while_waiting_for_follower": 0, "total_provider_requests": len(requests),
                             "synthetic_reported_tokens_per_task": {"input": 11, "output": 7},
                             "completed_candidate_tasks_per_second": round(2 / duration, 3),
                             "accept_outbox_to_first_wake_ms": round((wait["woken_at"] - accepted["created_at"]).total_seconds()*1000, 3),
                             "author_approvals": 0, "preauthorized_summary_completions": 1, "accepted_world_events": 0,
                             "host_turns": [{k: r[k] for k in ("attempt_id", "thread_id", "turn_id", "phase")} for r in records]}
                    Path(evidence_path).write_text(json.dumps(trace, indent=2) + "\n")
            else:
                assert records == [None, None]


def test_unknown_after_send_is_durable_and_never_redriven(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    with backend() as (http, requests, server):
        server.lose_response = True
        gateway = AgentGateway(kernel, {"test": http})
        supervisor = RuntimeSupervisor(kernel, gateway)
        job = _job(kernel, gateway, supervisor, world, kernel.tasks.create_budget(work, "2"), "READY")
        result = supervisor.run_once(world["principal"], work, branch)
        assert result["status"] == "BLOCKED"
        task = kernel.tasks.get(job.task["task_id"])
        assert task["state"] == "PAUSED"
        with pytest.raises(DomainError, match="Unresolved"):
            kernel.tasks.resume(task["task_id"], task["generation"])
        assert RuntimeSupervisor(kernel, gateway).run_once(world["principal"], work, branch)["status"] == "IDLE"
        assert len(requests) == 1
        with kernel.db.uow(write=False) as repo:
            assert repo.one("call_attempts", {"attempt_id": result["attempt_id"]})["state"] == "UNKNOWN"


def _child(kernel_env, world, http):
    import os
    import subprocess
    import sys
    body = {"endpoint": http.endpoint, "work": world["world_ref"]["work_id"],
            "branch": world["world_ref"]["branch_id"], "token": world["token"]}
    child = subprocess.Popen([sys.executable, __file__], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env={**os.environ, "NF_DATABASE_URL": kernel_env["url"],
            "NF_OBJECT_ROOT": str(kernel_env["store"].root)})
    child.stdin.write(json.dumps(body))
    child.stdin.close()
    return child


def test_process_death_after_provider_received_request(kernel_env):
    import threading
    from datetime import timedelta
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    with backend() as (http, requests, server):
        server.started, server.release = threading.Event(), threading.Event()
        gateway = AgentGateway(kernel, {"test": http})
        job = _job(kernel, gateway, RuntimeSupervisor(kernel, gateway), world, kernel.tasks.create_budget(work, "2"), "READY")
        process = _child(kernel_env, world, http)
        try:
            assert server.started.wait(15)
            process.kill()
            process.wait(timeout=5)
        finally:
            server.release.set()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        with kernel.db.uow() as repo:
            # Advance only the persisted lease boundary after forced process exit.
            repo.change("tasks", {"task_id": job.task["task_id"]}, {"lease_expiry": repo.now() - timedelta(seconds=1)})
        replacement = _child(kernel_env, world, http)
        assert replacement.wait(timeout=15) == 0
        assert json.loads(replacement.stdout.read())["status"] == "IDLE"
        assert kernel.tasks.get(job.task["task_id"])["state"] == "PAUSED"
        with kernel.db.uow(write=False) as repo:
            assert repo.one("call_attempts", {"task_id": job.task["task_id"]})["state"] == "UNKNOWN"
        assert len(requests) == 1


def test_settled_call_without_delivered_result_is_never_reissued(kernel_env, monkeypatch):
    from datetime import timedelta
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    with backend() as (http, requests, server):
        gateway = AgentGateway(kernel, {"test": http})
        supervisor = RuntimeSupervisor(kernel, gateway)
        job = _job(kernel, gateway, supervisor, world, kernel.tasks.create_budget(work, "2"), "READY")
        original = kernel.tasks.settle_call
        def crash(attempt, **fields):
            # Independent billing reconciliation arrived before task delivery.
            original(attempt, state="SUCCEEDED", actual_amount="0.02", actual_tokens=18,
                     provider_receipt="synthetic-test-receipt")
            raise RuntimeError("process exited after billing settlement")
        monkeypatch.setattr(kernel.tasks, "settle_call", crash)
        with pytest.raises(RuntimeError, match="after billing"):
            supervisor.run_once(world["principal"], work, branch)
        with kernel.db.uow() as repo:
            repo.change("tasks", {"task_id": job.task["task_id"]}, {"lease_expiry": repo.now() - timedelta(seconds=1)})
        state = supervisor.run_once(world["principal"], work, branch)
        assert state["status"] == "IDLE" and state["counts"]["PAUSED"] == 1
        assert len(requests) == 1


if __name__ == "__main__":
    # Separate test worker process with its own DB pool and no inherited connections.
    import os
    import sys
    from novel_factory.application.kernel import Kernel
    from novel_factory.infrastructure.database import Database
    from novel_factory.infrastructure.objects import ObjectStore
    config = json.load(sys.stdin)
    db = Database()
    try:
        kernel = Kernel(db, ObjectStore(os.environ["NF_OBJECT_ROOT"]))
        route = SyntheticHTTP(endpoint=config["endpoint"], model="fixture-no-inference",
            api_key="synthetic-fixture-only", allow_loopback_http=True)
        supervisor = RuntimeSupervisor(kernel, AgentGateway(kernel, {"test": route}))
        print(json.dumps(supervisor.run_once(kernel.authenticate(config["token"]), config["work"], config["branch"])))
    finally:
        db.close()
