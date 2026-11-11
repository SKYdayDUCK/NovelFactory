"""MCP stdio → PostgreSQL barrier → independently restarted production Supervisor."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import sys
from threading import Event
import time

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point, prepare_command
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.repository import KernelRepository
from test_p5_mcp import config
from test_p5_supervisor import backend

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
ROOT = Path(__file__).resolve().parents[2]


def _https(server, directory):
    """Use the production HTTPS-only configuration with a disposable loopback CA."""
    cert = directory / "loopback.pem"
    key = directory / "loopback-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=IP:127.0.0.1"], capture_output=True, check=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert), str(key))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    return cert


def _codex_children(pid):
    # Read process comm/PPid only. Host commands may contain private paths.
    children = {}
    names = {}
    for entry in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(line.split(":", 1) for line in entry.read_text().splitlines() if ":" in line)
            found, parent = int(fields["Pid"]), int(fields["PPid"])
            names[found] = fields["Name"].strip()
            children.setdefault(parent, []).append(found)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    pending, descendants = [pid], []
    while pending:
        parent = pending.pop()
        group = children.get(parent, [])
        descendants.extend(group)
        pending.extend(group)
    return {item: names[item] for item in descendants}


def test_one_resume_drives_real_process_restart(kernel_env, tmp_path):
    if not shutil.which("codex") or not Path("/proc/self/task").is_dir():
        requirement = "Process observation requires Linux and the installed Codex CLI"
        if os.environ.get("CI", "").lower() in {"true", "1"}:
            pytest.fail(requirement)
        pytest.skip(requirement)
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr, owner = world["world_ref"], world["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    binding = {"work_id": work, "branch_id": branch}
    auth = kernel.issue_grant(owner, work, role_id="resume_operator", branch_ids=[branch],
            capabilities=["PROJECT_RESUME"], expires_at=datetime.now(UTC) + timedelta(hours=1))
    params = config(tmp_path / "resume.json", kernel_env, auth["token"], binding)
    operator = tmp_path / "supervisor.json"
    with os.fdopen(os.open(operator, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
        json.dump({**binding, "token": world["token"]}, stream)

    ids = world["ids"]
    command = prepare_command(kernel, owner, wr, "TRANSFER_ITEM", {"item_id": ids["sword"],
        "from_id": ids["smith"], "to_id": ids["protagonist"], "transfer_kind": "GIFT", "at": point(10)})
    gate = command["task"]["task_id"]
    observations, supervisors, hosts = [], [], []
    with backend() as (_, requests, server):
        cert = _https(server, tmp_path)
        endpoint = f"https://127.0.0.1:{server.server_port}/v1/chat/completions"
        model = "deterministic-p5-loopback-fixture"
        upstream = OpenAICompatibleAdapter(endpoint=endpoint, model=model, api_key="fixture", timeout_seconds=60.0)
        gateway = AgentGateway(kernel, {"codex-text": CodexAppServerAdapter(upstream, HostJournal(kernel))})
        supervisor = RuntimeSupervisor(kernel, gateway)
        runtime = RuntimeTaskService(kernel, gateway)
        budget = kernel.tasks.create_budget(work, "2")
        jobs = []
        for label in ("producer", "follower"):
            job = runtime.prepare(owner, wr, role_id="evidence_maintainer", task_kind="summary",
                story_time=point(1), subject_ids=[ids["sword"]], predicates=["owner"],
                assignment=f"Read the bound inputs and return the {label} summary.", adapter_id="codex-text",
                queue_state="BLOCKED")
            supervisor.enroll(owner, job.task["task_id"], manifest_ref=job.manifest_ref,
                knowledge_cut={"plane": "WORLD"}, context_tokens=20000, input_ref=job.agent_input["input_ref"],
                budget_id=budget, reserve_amount="0.1", token_reservation=100000, accept_summary=True)
            def acl(conn):
                kernel.authorize(KernelRepository(conn, tables), owner, work, "ADMIN", branch)
            supervisor.workflow.wait(job.task["task_id"], 0, [jobs[-1] if jobs else gate],
                **binding, mode="AND", authorize_in_tx=acl)
            jobs.append(job.task["task_id"])

        proc_env = {**os.environ, "NF_DATABASE_URL": kernel_env["url"],
                    "NF_OBJECT_ROOT": str(kernel_env["store"].root), "NF_MODEL_API_KEY": "fixture",
                    "NF_MODEL_ENDPOINT": endpoint, "NF_MODEL_NAME": model, "NO_PROXY": "127.0.0.1",
                    "SSL_CERT_FILE": str(cert), "NF_MODEL_MAX_OUTPUT_TOKENS": "2048",
                    "NF_MODEL_TIMEOUT_SECONDS": "60"}
        started = time.monotonic()

        async def probe():
            async with stdio_client(params) as streams, ClientSession(*streams) as session:
                handshake = await session.initialize()
                tools = [item.name for item in (await session.list_tools()).tools]
                assert set(tools) == {"nf_project_status", "nf_resume_project"}
                async def call(method):
                    result = await session.call_tool(method, {})
                    assert not result.isError
                    return result.structuredContent

                async def snapshot(label):
                    state = (await call("nf_project_status"))["counts"]
                    observations.append(dict(label=label, elapsed_s=round(time.monotonic()-started, 4),
                                             counts=state, provider_requests=len(requests)))
                    return state

                async def cycle(expected_task=None):
                    server.started, server.release = Event(), Event()
                    process = subprocess.Popen([sys.executable, "scripts/run_supervisor.py",
                        "--access-file", str(operator), "--once"], cwd=ROOT, env=proc_env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                    supervisors.append(process.pid)
                    captured = []
                    try:
                        if expected_task is not None:
                            assert await asyncio.to_thread(server.started.wait, 14), "Provider fixture received no request"
                            descendants = _codex_children(process.pid)
                            captured = [pid for pid, name in descendants.items() if name == "codex"]
                            assert len(captured) == 1, descendants
                            hosts.extend(captured)
                            active = await snapshot("provider processing " + expected_task)
                            assert active["RUNNING"] == 1
                            if expected_task == jobs[0]:
                                assert active["WAITING"] == 1
                            assert json.loads(requests[-1]["messages"][1]["content"])["context"]["task_id"] == expected_task
                        server.release.set()
                        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=25)
                        assert process.returncode == 0, stderr[-2000:]
                        receipt = json.loads(stdout)
                        assert receipt["status"] == ("RESULT_STORED" if expected_task else "IDLE")
                        assert all(not Path(f"/proc/{pid}").exists() for pid in captured)
                    finally:
                        server.release.set()
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)

                before = await snapshot("dependencies outstanding")
                assert before["READY"] == 0 and before["WAITING"] == 2
                assert (await call("nf_resume_project"))["awakened"] == 0
                await cycle()
                assert len(requests) == 0
                receipt = kernel.accept(owner, command["request"])
                assert receipt == kernel.accept(owner, command["request"])
                resumed = await call("nf_resume_project")
                assert resumed["awakened"] == 1 and resumed["counts"]["READY"] == 1
                for _ in range(99):
                    replay = await call("nf_resume_project")
                    assert replay["awakened"] == 0 and replay["counts"]["READY"] == 1
                assert not requests
                await snapshot("MCP woke producer")
                await cycle(jobs[0])
                await snapshot("first supervisor and host exited")
                await cycle(jobs[1])  # Fresh OS process consumes accepted summary and wakes follower.
                await cycle()
                idle = await snapshot("completed")
                assert idle["ACCEPTED"] == 3 and idle["READY"] == idle["RUNNING"] == idle["WAITING"] == 0
                assert (await call("nf_resume_project"))["awakened"] == 0 and len(requests) == 2
                return handshake.protocolVersion, sorted(tools), receipt
        protocol, tools, receipt = asyncio.run(probe())
        elapsed = time.monotonic() - started
        assert len(set(supervisors)) == 4 and len(set(hosts)) == 2
        with kernel.db.uow(write=False) as repo:
            targets = [repo.one("call_attempts", {"task_id": target}) for target in jobs]
            journal = [repo.one("host_turns", {"attempt_id": row["attempt_id"]}) for row in targets]
            assert all(row["phase"] == "COMPLETED" for row in journal)
            assert len({row["thread_id"] for row in journal}) == len({row["turn_id"] for row in journal}) == 2
            latencies = []
            tokens = []
            for previous, current, row in zip([gate, jobs[0]], jobs, targets):
                accepted = repo.one("outbox", {"aggregate_id": previous, "dedupe_key": f"task:{previous}:1:ACCEPTED"})
                wait = repo.one("workflow_waits", {"task_id": current, "generation": 0})
                latencies.append(round((wait["woken_at"] - accepted["created_at"]).total_seconds() * 1000, 3))
                usage = gateway._read_audit(repo, row["attempt_id"])["payload"]["usage"]
                tokens.append({"input": usage["input_tokens"], "output": usage["output_tokens"]})
            assert len(repo.many("approvals", {"work_id": work})) == 1
            assert kernel.head(repo, work, branch) == receipt["world_ref"]
            if output := os.environ.get("NF_P5_PROCESS_EVIDENCE"):
                evidence = {"postgres": repo.conn.exec_driver_sql("SHOW server_version").scalar_one(),
                    "codex": subprocess.check_output(["codex", "--version"], text=True).strip(),
                    "provider": "deterministic loopback HTTPS; fixture token reports; no paid inference",
                    "mcp": {"client": "Python SDK over spawned stdio", "protocol": protocol, "tools": tools},
                    "supervisor_pids_in_order": supervisors, "observed_codex_pids": hosts,
                    "codex_processes_alive_after_each_turn": 0, "observations": observations,
                    "waiting_provider_requests_before_gate": 0, "candidate_tasks": len(jobs),
                    "end_to_end_seconds": round(elapsed, 4),
                    "candidate_tasks_per_second_including_process_restarts": round(len(jobs)/elapsed, 4),
                    "accept_to_ready_ms": latencies, "provider_reported_tokens_per_task": tokens,
                    "ordinary_world_approvals": 1, "human_approvals": 0, "accepted_summary_tasks": 2,
                    "accepted_world_events": len(receipt["accepted_event_refs"]),
                    "source_world_ref": wr, "accepted_world_ref": receipt["world_ref"],
                    "host_turns": [{k:r[k] for k in ("attempt_id","thread_id","turn_id","phase")} for r in journal]}
                Path(output).write_text(json.dumps(evidence, indent=2) + "\n")
