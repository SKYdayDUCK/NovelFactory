"""One stdio author resume and an unmodified service drive a complete banquet deviation."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from test_p5_mcp import config
from test_p5_process_resume import ROOT, _codex_children, _https
from test_p5_supervisor import backend
from test_planning import make_plan
from test_simulation import process_definition, register

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.planning import PlanningService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.scene_runtime import register_scene
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.domain.world import state_at
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.repository import KernelRepository

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _candidate(packet, choices, scopes):
    """Deterministic texts at the HTTPS provider boundary. No database authority."""
    ctx = packet["context"]
    actors = list(choices)
    if ctx["role_id"] == "character_actor":
        return {
            "schema_version": "2.0",
            "proposal_id": str(uuid4()),
            "task_id": ctx["task_id"],
            "generation": ctx["generation"],
            "context_bundle_id": ctx["context_bundle_id"],
            "world_ref": ctx["world_ref"],
            "actor_id": ctx["actor_id"],
            "intent": choices[ctx["actor_id"]],
            "targets": [],
            "story_interval": {"start": point(30), "end": point(31), "bounds": "[)"},
            "resource_requests": [],
            "preconditions": [],
            "evidence_refs": [],
            "dependency_fingerprint": ctx["dependency_fingerprint"],
            "idempotency_key": "sealed-action",
        }
    if ctx["role_id"] == "evidence_maintainer":
        return {
            "text": "宴会事务通知已登记，可以开始下一项工作。",
            "event_refs": [],
            "important_deltas": [],
            "evidence_refs": [],
        }
    if ctx["role_id"] == "adjudication_advisor":
        joint = packet["joint"]
        location = joint["allowed_location"]
        return {
            "schema_version": "2.0",
            "proposal_id": str(uuid4()),
            "input_world_ref": ctx["world_ref"],
            "interaction_id": joint["interaction_id"],
            "action_proposal_refs": [item["ref"] for item in joint["action_proposals"]],
            "ruleset_ref": joint["ruleset_ref"],
            "story_time": joint["story_time"],
            "effect_group_id": str(uuid4()),
            "representation_id": str(uuid4()),
            "effects": [
                {
                    "kind": "RELATION_SET",
                    "effect_key": "guard-southern-storehouse",
                    "subject_id": location["subject_id"],
                    "predicate": "located_in",
                    "object_id": location["destination_id"],
                    "value_status": "VALUE",
                }
            ],
            "reasoning_summary": "A 前往南岸守粮，错过北岸宴会。B 仍待消息，C 清点库存。",
            "unresolved_questions": [],
            "dependency_fingerprint": ctx["dependency_fingerprint"],
        }
    editorial = packet["editorial"]
    materials = editorial["materials"]
    if editorial["operation"] == "request_change":
        return {
            "text": "原定当面交接已不可行。调整接下来两场，改用书信验单。",
            "important_deltas": ["会面取消"],
            "event_refs": editorial["accepted_event_refs"],
            "evidence_refs": [],
        }
    if editorial["operation"] == "propose_window":
        return {
            "schema_version": "2.0",
            "window_plan_id": str(uuid4()),
            "volume_contract_revision_id": materials["volume_contract_ref"]["content"][
                "contract_revision_id"
            ],
            "input_world_ref": ctx["world_ref"],
            "story_interval": {"start": point(50), "end": point(51, 1), "bounds": "[)"},
            "scope_ids": scopes,
            "process_ids": [],
            "focus_actor_ids": actors,
            "resolution_strategy": "MICRO_INTERACTION",
            "candidate_goals": ["通过书信联系", "核实送达凭证"],
            "next_decision_reason": "等待回信后再判断。",
            "assumptions": [],
            "adopted_event_refs": materials["change_point_ref"]["content"]["event_refs"],
            "expected_outputs": ["SCENE_CONTRACT"],
            "dependency_fingerprint": ctx["dependency_fingerprint"],
            "status": "DRAFT",
        }
    assert editorial["operation"] == "review_and_adopt"
    return {
        "decision": "APPROVED",
        "reason": "按既有授权审阅受影响的两场及已接受证据。",
        "deferred_questions": [],
        "evidence_refs": [],
    }


def _task_diagnostics(repo, gateway, tasks):
    """Only durable IDs, phases and bounded application codes cross the CI log boundary."""
    report = []
    for task in tasks:
        attempts = []
        for attempt in repo.many("call_attempts", {"task_id": task["task_id"]}):
            identity = attempt["attempt_id"]
            host = repo.one("host_turns", {"attempt_id": identity})
            audit = gateway._read_audit(repo, identity)
            detail = {"attempt_id": identity, "call_state": attempt["state"],
                      "host_phase": host["phase"] if host else None,
                      "thread_started": bool(host and host["thread_id"]),
                      "turn_started": bool(host and host["turn_id"])}
            if audit:
                reason = audit["payload"]["reason"]
                detail["reason"] = reason if reason in {
                    None, "MODEL_ADAPTER_FAILURE", "CODEX_HOST_START_FAILED", "CODEX_HOST_CLEANUP_FAILED",
                    "CODEX_TRANSPORT_UNKNOWN", "CODEX_RPC_ERROR", "CODEX_TURN_INCOMPLETE",
                    "UNEXPECTED_HOST_TOOL_REQUEST", "HOST_BINDING_MISMATCH", "MODEL_HTTP_ERROR",
                    "MODEL_RATE_LIMITED", "MODEL_TRANSPORT_UNKNOWN", "INVALID_MODEL_OUTPUT",
                    "MODEL_IDENTITY_MISMATCH", "CALL_RECONCILIATION_REQUIRED",
                } else "OTHER_DOMAIN_ERROR"
            attempts.append(detail)
        report.append({"task_id": task["task_id"], "state": task["state"], "attempts": attempts})
    return report


def test_one_author_resume_through_autonomous_banquet(kernel_env, tmp_path):
    run_banquet(kernel_env, tmp_path)


def run_banquet(kernel_env, tmp_path, *, redis_workers=None, prepare_prerequisite=None):
    if not shutil.which("codex") or not Path("/proc/self/task").is_dir():
        required = "This process-level acceptance requires an installed Codex CLI on Linux"
        if os.environ.get("CI", "").lower() in {"true", "1"}:
            pytest.fail(required)
        pytest.skip(required)

    kernel = kernel_env["kernel"]
    base = load_fixture(kernel, "white_bridge")
    ids = base["ids"]
    process = process_definition(
        kernel, base, name="banquet", scopes=[ids["warehouse"], ids["south"]]
    )
    world, _ = register(kernel, base, process)
    wr, owner = world["world_ref"], world["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    binding = {"work_id": work, "branch_id": branch}
    actors = process["required_actor_ids"]
    choices = dict(
        zip(
            actors,
            ["我不赴北岸晚宴，去南岸守粮。", "我在北岸等来信，收到凭证再走。", "我先核对库单。"],
            strict=True,
        )
    )
    planning = PlanningService(kernel)
    plan, _ = make_plan(planning, world)
    for i, (node, goal) in enumerate(
        zip(plan["nodes"], ["赴北岸宴会", "当面验单", "独立清点剩余库存"], strict=True)
    ):
        node.update(goal=goal, target_time={"kind": "EXACT", "at": point(50 + i)})
    reference = planning.save_plan(owner, plan, "banquet-plan")
    planning.adopt_plan(owner, wr, reference, None, "select-banquet")
    volume = plan["volume_id"]
    selection = {"work_id": work, "selection_kind": planning.selection_key(volume)}
    auth = kernel.issue_grant(
        owner,
        work,
        role_id="resume_operator",
        branch_ids=[branch],
        capabilities=["PROJECT_RESUME"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    params = config(tmp_path / "mcp.json", kernel_env, auth["token"], binding)
    access = tmp_path / "service.json"
    with os.fdopen(os.open(access, os.O_WRONLY | os.O_EXCL | os.O_CREAT, 0o600), "w") as private:
        json.dump({**binding, "token": world["token"]}, private)

    with backend(concurrent=True) as (_, requests, server):
        cert = _https(server, tmp_path)
        endpoint = f"https://127.0.0.1:{server.server_port}/v1/chat/completions"
        model = "deterministic-banquet-fixture"
        upstream = OpenAICompatibleAdapter(
            endpoint=endpoint, model=model, api_key="fixture", timeout_seconds=60.0
        )
        gateway = AgentGateway(
            kernel, {"codex-text": CodexAppServerAdapter(upstream, HostJournal(kernel))}
        )
        sup = RuntimeSupervisor(kernel, gateway)
        budget = kernel.tasks.create_budget(work, "20")
        # External integration tests may provision a real same-branch prerequisite
        # through another service. Its receipt is an ordinary NF task dependency.
        extra_dependencies = (
            [prepare_prerequisite(world, budget, ids)] if prepare_prerequisite else []
        )
        signal_job = RuntimeTaskService(kernel, gateway).prepare(
            owner,
            wr,
            role_id="evidence_maintainer",
            task_kind="summary",
            story_time=point(30),
            subject_ids=[ids["warehouse"]],
            predicates=["located_in"],
            assignment="收到调度的业务通知后，为下一位参与者登记已送达的流程回执。仅使用自己的材料。",
            adapter_id="codex-text",
            queue_state="BLOCKED",
        )
        signal_id = signal_job.task["task_id"]
        sup.enroll(
            owner,
            signal_id,
            manifest_ref=signal_job.manifest_ref,
            input_ref=signal_job.agent_input["input_ref"],
            knowledge_cut={"plane": "WORLD"},
            context_tokens=20000,
            budget_id=budget,
            reserve_amount="1",
            token_reservation=100000,
            accept_summary=True,
        )
        participants = [
            {"actor_id": actors[0]},
            {"actor_id": actors[1], "after_tasks": [signal_id, *extra_dependencies]},
            {"actor_id": actors[2]},
        ]
        registration = {
            "world_ref": wr,
            "story_time": point(30),
            "scope_ids": process["scope_ids"],
            "volume_id": volume,
            "scene_limit": 2,
            "participants": participants,
            "subject_id": actors[0],
            "destination_id": ids["south"],
            "budget_id": budget,
            "adapter_id": "codex-text",
            "reserve_amount": "1",
            "token_reservation": 100000,
            "idempotency_key": "banquet",
        }

        def inventory():
            with kernel.db.uow(write=False) as repo:
                return {
                    table: len(repo.many(table, {"work_id": work}))
                    for table in (
                        "tasks",
                        "grants",
                        "simulation_interactions",
                        "selections",
                        "artifacts",
                        "receipts",
                    )
                }

        initial_inventory = inventory()
        # The cycle appears only after actor grants, tasks and sealed slots are staged.
        cycle = [
            {"actor_id": actors[0], "after_actors": [actors[1]]},
            {"actor_id": actors[1], "after_actors": [actors[0]]},
            participants[2],
        ]
        with pytest.raises(DomainError) as rejected:
            register_scene(kernel, gateway, owner, **{**registration, "participants": cycle})
        assert rejected.value.code == "WORKFLOW_CYCLE"
        assert inventory() == initial_inventory
        # Two independent controllers can receive the same first-time registration.
        start = Barrier(2)
        def concurrent_register():
            start.wait(timeout=10)
            return register_scene(kernel, gateway, owner, **registration)
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(concurrent_register)
            second = workers.submit(concurrent_register)
            scene = first.result(timeout=30)
            assert second.result(timeout=30) == scene
        registered_inventory = inventory()
        assert register_scene(kernel, gateway, owner, **registration) == scene
        assert inventory() == registered_inventory
        anchor, iid = scene["reference_timeline_ref"], scene["interaction_id"]
        control_id, joint_id, director_id = (
            scene[key] for key in ("control_task_id", "adjudicator_task_id", "director_task_id")
        )
        actor_tasks = [scene["actor_tasks"][actor] for actor in actors]
        sup.workflow.wait(
            signal_id,
            0,
            [actor_tasks[0]],
            mode="AND",
            **binding,
            authorize_in_tx=lambda c: kernel.authorize(
                KernelRepository(c, tables), owner, work, "ADMIN", branch
            ),
        )
        labels = {
            signal_id: "notification",
            **dict(zip(actor_tasks, ("A", "B", "C"), strict=True)),
            joint_id: "joint",
            control_id: "kernel_accept",
            director_id: "request_change",
        }
        dependencies = {
            signal_id: [actor_tasks[0]],
            actor_tasks[1]: [signal_id, *extra_dependencies],
            joint_id: actor_tasks,
            director_id: [control_id],
        }
        with kernel.db.uow(write=False) as repo:
            before_events = len(repo.many("events", {"work_id": work}))
            before_approvals = len(repo.many("approvals", {"work_id": work}))

        env = {
            **os.environ,
            "NF_DATABASE_URL": kernel_env["url"],
            "NF_OBJECT_ROOT": str(kernel_env["store"].root),
            "NF_MODEL_ENDPOINT": endpoint,
            "NF_MODEL_NAME": model,
            "NF_MODEL_API_KEY": "fixture",
            "NF_MODEL_MAX_OUTPUT_TOKENS": "2048",
            "NF_MODEL_TIMEOUT_SECONDS": "60",
            "NO_PROXY": "127.0.0.1",
            "SSL_CERT_FILE": str(cert),
        }
        observations, callback_errors = [], []
        started = time.monotonic()
        proc = None

        actor_overlap = Barrier(2)

        def respond(packet):
            try:
                task_id = packet["context"]["task_id"]
                if task_id in (actor_tasks[0], actor_tasks[2]):
                    actor_overlap.wait(timeout=20)
                if "editorial" in packet:
                    labels[task_id] = packet["editorial"]["operation"]
                with kernel.db.uow(write=False) as repo:
                    states = {
                        labels[r["task_id"]]: r["state"]
                        for r in repo.many("tasks", {"work_id": work})
                        if r["task_id"] in labels
                    }
                    counts = sup.workflow._counts(repo.conn, work, branch)["counts"]
                hosts = [pid for pid, name in _codex_children(proc.pid).items() if name == "codex"]
                observations.append(
                    {
                        "turn": labels[task_id],
                        "elapsed_s": round(time.monotonic() - started, 4),
                        "states": states,
                        "counts": counts,
                        "live_codex_pids": hosts,
                    }
                )
                if task_id in (actor_tasks[0], actor_tasks[2]):
                    actor_overlap.wait(timeout=20)
                return _candidate(packet, choices, process["scope_ids"])
            except Exception as exc:
                callback_errors.append(type(exc).__name__)
                raise

        server.packet_candidate = respond

        async def drive():
            nonlocal proc
            async with stdio_client(params) as streams, ClientSession(*streams) as session:
                handshake = await session.initialize()
                tools = sorted(t.name for t in (await session.list_tools()).tools)
                assert tools == ["nf_project_status", "nf_resume_project"]
                resume = await session.call_tool("nf_resume_project", {})
                assert not resume.isError
                initial = resume.structuredContent["counts"]
                assert initial["WAITING"] == 4 and not requests
                stdout_path, stderr_path = tmp_path / "service.log", tmp_path / "errors.log"
                command = [
                    sys.executable,
                    "-u",
                    "scripts/run_supervisor.py",
                    "--access-file",
                    str(access),
                ]
                if redis_workers is not None:
                    await asyncio.to_thread(redis_workers.start, command, env)
                    command.extend(["--processor", "model"])
                with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        "--workers",
                        "2",
                        cwd=ROOT,
                        env=env,
                        stdout=stdout,
                        stderr=stderr,
                        start_new_session=True,
                    )
                    try:
                        deadline = time.monotonic() + 90
                        while time.monotonic() < deadline:
                            with kernel.db.uow(write=False) as repo:
                                changed = (
                                    repo.one("selections", selection)["artifact_ref"] != reference
                                )
                                tasks = repo.many("tasks", {"work_id": work})
                                held = [task for task in tasks if task["state"] in {
                                    "WAITING_REVIEW", "STALE", "FAILED", "PAUSED", "UNKNOWN"}]
                                if proc.returncode is not None or held or callback_errors:
                                    pytest.fail(json.dumps({
                                        "supervisor_exit": proc.returncode, "fixture_error_count": len(callback_errors),
                                        "tasks": _task_diagnostics(repo, gateway, held or tasks),
                                    }))
                            if changed:
                                break
                            await asyncio.sleep(0.12)
                        else:
                            with kernel.db.uow(write=False) as repo:
                                tasks = repo.many("tasks", {"work_id": work})
                                pytest.fail("Supervisor stalled " + json.dumps(_task_diagnostics(repo, gateway, tasks)))
                        result = await session.call_tool("nf_project_status", {})
                        assert not result.isError
                        final = result.structuredContent["counts"]
                        assert final["READY"] == final["RUNNING"] == final["WAITING"] == 0
                    finally:
                        if proc.returncode is None:
                            os.killpg(proc.pid, signal.SIGTERM)
                        await asyncio.wait_for(proc.wait(), timeout=8)
                check = await asyncio.to_thread(
                    subprocess.run,
                    [*command, "--once"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=20,
                )
                assert json.loads(check.stdout)["status"] == "IDLE"
                return handshake.protocolVersion, tools, initial, final

        protocol, tools, initial, final = asyncio.run(drive())
        if redis_workers is not None:
            redis_workers.check(kernel, owner, work, branch, iid)
        elapsed = time.monotonic() - started
        completed_inventory = inventory()
        assert register_scene(kernel, gateway, owner, **registration) == scene
        assert inventory() == completed_inventory
        turns = [o["turn"] for o in observations]
        assert set(turns[:4]) == {"A", "B", "C", "notification"}
        assert turns.index("A") < turns.index("notification") < turns.index("B")
        assert turns[4:] == ["joint", "request_change", "propose_window", "review_and_adopt"]
        assert all(o["states"][o["turn"]] == "RUNNING" for o in observations)
        assert all(o["states"]["request_change"] == "BLOCKED" for o in observations[:5])
        for prior in ("A", "notification"):
            assert next(o for o in observations if o["turn"] == prior)["states"]["B"] == "BLOCKED"
        assert (
            next(o for o in observations if o["turn"] == "B")["states"]["notification"]
            == "ACCEPTED"
        )
        for o in observations:
            if o["turn"] in ("A", "C"):
                assert o["states"]["A"] == o["states"]["C"] == "RUNNING"
                assert len(o["live_codex_pids"]) == 2
            else:
                assert 1 <= len(o["live_codex_pids"]) <= 2
        host_pids = {pid for o in observations for pid in o["live_codex_pids"]}
        assert len(set(host_pids)) == 8 and not any(
            Path(f"/proc/{pid}").exists() for pid in host_pids
        )
        assert len(requests) == 8
        for request in requests[:4]:
            message = request["messages"][1]["content"]
            packet = json.loads(message)
            actor_cut = packet["read_cut"]["knowledge_cut"]
            if packet["context"]["role_id"] == "evidence_maintainer":
                assert actor_cut == {"plane": "WORLD"}
            else:
                assert (
                    actor_cut["plane"] == "BELIEF"
                    and actor_cut["actor_id"] == packet["context"]["actor_id"]
                )
            assert (
                not any(intent in message for intent in choices.values())
                and reference["sha256"] not in message
            )
        with kernel.db.uow(write=False) as repo:
            now = kernel.head(repo, work, branch)
            assert wr != now
            for revision, location in ((wr, ids["north"]), (now, ids["south"])):
                world_snapshot = kernel.revision(repo, revision)["snapshot"]
                assert (
                    state_at(world_snapshot, actors[0], "located_in", point(31))["data"][
                        "entity_id"
                    ]
                    == location
                )
            joint_log = repo.one(
                "outbox", {"aggregate_id": joint_id, "topic": "supervisor.joint.delivery"}
            )["payload"]
            assert joint_log["status"] == "JOINT_ACCEPTED" and joint_log["world_ref"] == now
            changes = len(repo.many("events", {"work_id": work})) - before_events
            approvals = len(repo.many("approvals", {"work_id": work})) - before_approvals
            assert changes == len(joint_log["accepted_event_refs"]) > 0 and approvals == 1
            sealed = repo.many("simulation_intents", {"interaction_id": iid})
            assert {r["actor_id"]: r["payload"]["intent"] for r in sealed} == choices
            selected = repo.one("selections", selection)["artifact_ref"]
            revised = kernel.artifact(repo, selected, work)["payload"]
            assert kernel.artifact(repo, anchor, work)["payload"]["initial_plan_ref"] == reference
            assert kernel.artifact(repo, reference, work)["payload"] == plan
            changed_slots = [
                i
                for i, nodes in enumerate(zip(plan["nodes"], revised["nodes"], strict=True))
                if nodes[0] != nodes[1]
            ]
            assert changed_slots == [0, 1]
            editorial = {
                labels[r["aggregate_id"]]: r["payload"]
                for r in repo.many("outbox", {"topic": "supervisor.editorial.delivery"})
                if r["aggregate_id"] in labels
            }
            assert editorial["review_and_adopt"]["decision"] == "APPROVED"
            assert all(
                kernel.tasks._task(repo.conn, task_id)["state"] == "ACCEPTED" for task_id in labels
            )
            latency = {}
            for target, preds in dependencies.items():
                wait = repo.one("workflow_waits", {"task_id": target, "generation": 0})
                last_event = max(
                    repo.one("outbox", {"dedupe_key": f"task:{p}:1:ACCEPTED"})["created_at"]
                    for p in preds
                )
                latency[labels[target]] = round(
                    (wait["woken_at"] - last_event).total_seconds() * 1000, 3
                )
            journals, usage = [], {}
            for task_id, label in labels.items():
                if task_id == control_id:
                    continue
                attempt = repo.one("call_attempts", {"task_id": task_id})
                journal = repo.one("host_turns", {"attempt_id": attempt["attempt_id"]})
                assert journal["phase"] == "COMPLETED"
                journals.append(
                    {"task": label, **{k: journal[k] for k in ("thread_id", "turn_id", "phase")}}
                )
                tokens = gateway._read_audit(repo, attempt["attempt_id"])["payload"]["usage"]
                usage[label] = {k: tokens[k] for k in ("input_tokens", "output_tokens")}
            assert (
                len({j["thread_id"] for j in journals})
                == len({j["turn_id"] for j in journals})
                == 8
            )
            assert all(item == {"input_tokens": 11, "output_tokens": 7} for item in usage.values())
            if output := os.environ.get("NF_P55_BANQUET_EVIDENCE" if redis_workers else "NF_P5_BANQUET_EVIDENCE"):
                from novel_factory.application.prompt_schema import inline_schema
                from novel_factory.domain.canonical import canonical_bytes

                payload_sizes = {}
                for request in requests:
                    messages = request["messages"]
                    packet = json.loads(messages[1]["content"])
                    ctx = packet["context"]
                    task = kernel.tasks.get(ctx["task_id"])
                    cfg = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
                    frozen = kernel.artifact(repo, cfg["dispatch"]["input_ref"], work, kind="AGENT_INPUT")
                    contract = frozen["payload"]["output_contract"]
                    schema_section = messages[0]["content"].split("\nPayload JSON Schema: ", 1)[1]
                    _, schema_end = json.JSONDecoder().raw_decode(schema_section)
                    payload_sizes[labels[task["task_id"]]] = {
                        "prompt_utf8_bytes": sum(len(m["content"].encode()) for m in messages),
                        "inlined_schema_bytes": len(canonical_bytes(inline_schema(contract["name"], contract["version"]))),
                        "emitted_schema_bytes": len(schema_section[:schema_end].encode()),
                        "authorized_materials": len(packet["authorized_materials"]),
                    }
                assert len(payload_sizes) == len(requests) == 8
                assert len({row["context_bundle_id"] for row in repo.many("contexts", {"work_id": work})
                            if row["task_id"] in labels and row["task_id"] != control_id}) == 8
                report = {
                    "test": "test_one_author_resume_through_autonomous_banquet",
                    "provider": "deterministic HTTPS loopback fixture; no live inference or paid tokens",
                    "mcp": {
                        "client": "Python MCP SDK via child stdio",
                        "protocol": protocol,
                        "tools": tools,
                        "resume_calls": 1,
                    },
                    "codex": subprocess.check_output(["codex", "--version"], text=True).strip(),
                    "postgres": repo.conn.exec_driver_sql("SHOW server_version").scalar_one(),
                    "initial": initial,
                    "final": final,
                    "scene_provider_turns": len(requests),
                    "observations": observations,
                    "accept_to_ready_ms": latency,
                    "scene_fixture_input_tokens": sum(v["input_tokens"] for v in usage.values()),
                    "scene_fixture_output_tokens": sum(v["output_tokens"] for v in usage.values()),
                    "tokens_by_turn": usage,
                    "observed_request_sizes": payload_sizes,
                    "completed_model_tasks_per_second": round(len(usage) / elapsed, 4),
                    "elapsed_s_including_restart": round(elapsed, 4),
                    "B_wake_source": "Distinct notification task completed after A; no private A text or in-world BELIEF is inferred",
                    "A_location_at_tick_31": {"reference": "north", "accepted": "south"},
                    "active_host_processes_after_completion": 0,
                    "waiting_provider_calls": 0,
                    "human_interventions": 0,
                    "ordinary_approvals": approvals,
                    "accepted_world_events": changes,
                    "sealed_actor_intents": list(choices.values()),
                    "accepted_world_ref": now,
                    "base_world_ref": wr,
                    "reference_timeline_ref": anchor,
                    "initial_plan_ref": reference,
                    "selected_plan_ref": selected,
                    "revised_future_nodes": changed_slots,
                    "change_point_ref": editorial["request_change"]["change_point_ref"],
                    "window_ref": editorial["propose_window"]["window_ref"],
                    "host_journal": journals,
                    "idle_restart": "IDLE",
                    "registration_replay": "same receipt, unchanged inventory before and after world advancement",
                    "failed_cyclic_registration": "transaction rolled back; no additional grants or tasks",
                    "errors": callback_errors,
                }
                if extra_dependencies:
                    report["external_prerequisites"] = extra_dependencies
                    report["dependent_actor_task"] = actor_tasks[1]
                Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
