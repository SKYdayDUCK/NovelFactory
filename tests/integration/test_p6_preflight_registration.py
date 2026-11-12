"""Exercise the register-only P6 operator handoff with real PostgreSQL and Redis."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from novel_factory.application.planning import PlanningService
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.event_streams import STREAMS
from novel_factory.infrastructure.runtime_catalog import RuntimeCatalog
from tests.reliability.test_p55_eventhub import server
from test_planning import make_plan
from test_simulation import process_definition, register


ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("NF_TEST_REDIS_URL"), reason="requires disposable Redis"),
]


def private_file(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(value, f)
    return path


def test_operator_registers_once_and_leaves_dispatch_idle(kernel_env, server, tmp_path, record_testsuite_property):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel, "white_bridge")
    stale_world = fixture["world_ref"]
    ids = fixture["ids"]
    scopes = [ids["warehouse"], ids["south"]]
    first = process_definition(kernel, fixture, name="first", scopes=scopes, due=30)
    fixture, _ = register(kernel, fixture, first)
    second = process_definition(kernel, fixture, name="next", scopes=scopes, due=50)
    fixture, _ = register(kernel, fixture, second)
    world = fixture["world_ref"]
    project = {key: world[key] for key in ("work_id", "branch_id")}
    owner = fixture["principal"]
    other = load_fixture(kernel)
    plans = PlanningService(kernel)
    plan, _ = make_plan(plans, fixture)
    for index, node in enumerate(plan["nodes"]):
        node["target_time"] = {"kind": "EXACT", "at": point(50 + index)}
    plan_ref = plans.save_plan(owner, plan, "operator-plan")
    plans.adopt_plan(owner, world, plan_ref, None, "operator-adopt")
    budget = kernel.tasks.create_budget(world["work_id"], "0.50")

    env = {
        **os.environ, "NF_DATABASE_URL": kernel_env["url"], "NF_OBJECT_ROOT": str(kernel.objects.root),
        "NF_REDIS_URL": f"redis://127.0.0.1:{server.port}/0",
        "NF_MODEL_ENDPOINT": "https://p6-registration.invalid/v1/chat/completions",
        "NF_MODEL_NAME": "registration-fixture", "NF_MODEL_API_KEY": "synthetic-no-dispatch",
        "NF_MODEL_MAX_OUTPUT_TOKENS": "24", "NF_MODEL_TIMEOUT_SECONDS": "1",
    }

    def cli(script, *args, denied=None):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / script), *map(str, args)],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=45)
        if denied is not None:
            assert proc.returncode != 0 and denied in proc.stderr and not proc.stdout
            return
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    auth = {"database_url": kernel_env["url"], "object_root": str(kernel.objects.root), "binding": project}
    admin = private_file(tmp_path / "project.json", {**auth, "token": fixture["token"]})
    wrong = private_file(tmp_path / "wrong-project.json", {**auth, "token": other["token"]})
    access = private_file(tmp_path / "supervisor.json", {**project, "token": fixture["token"]})
    wrong_access = private_file(tmp_path / "foreign-supervisor.json", {**project, "token": other["token"]})
    budget_args = ("--budget-id", budget)
    cli("p6_budget.py", "--binding", wrong, "show", *budget_args, denied="VIEW_DENIED")
    cli("p6_budget.py", "--binding", wrong, "set", *budget_args, "--total-tokens", 5000, denied="VIEW_DENIED")
    cli("p6_budget.py", "--binding", admin, "set", *budget_args, "--total-tokens", 5000)
    balance = cli("p6_budget.py", "--binding", admin, "show", *budget_args)
    assert balance["available_tokens"] == 5000 and balance["limit_amount"] == "0.500000"

    scene = {
        "world_ref": world, "story_time": point(30), "scope_ids": scopes, "volume_id": plan["volume_id"],
        "scene_limit": 2, "participants": [{"actor_id": a} for a in first["required_actor_ids"]],
        "subject_id": first["required_actor_ids"][0], "destination_id": ids["south"], "budget_id": budget,
        "adapter_id": "external", "reserve_amount": "0.01", "token_reservation": 250,
        "alternative_destination_ids": None, "allowed_item_gift": None, "volume_end": None,
        "following_scenes": [{"node_id": plan["nodes"][0]["node_id"], "process_id": second["process_id"]}],
        "idempotency_key": "p6-register-only",
    }
    scene_path = private_file(tmp_path / "scene.json", scene)
    experiment = json.loads((ROOT / "reports/handoff/P6_experiment.example.json").read_text())
    experiment.update(nf_source_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                      skills_git_tree=subprocess.check_output(["git", "rev-parse", "HEAD:runtime/skills"],
                                                              cwd=ROOT, text=True).strip(),
                      accepted_plan_ref=plan_ref, material_seed=0)
    experiment["authorized_limits"].update(currency="USD", total_amount="0.50", max_amount_per_attempt="0.01",
                                          total_tokens=5000, max_output_tokens_per_attempt=24,
                                          provider_endpoint=env["NF_MODEL_ENDPOINT"], model_name=env["NF_MODEL_NAME"])
    preflight = subprocess.run(
        [sys.executable, str(ROOT / "scripts/P6_preflight.py"), "--nf-checkout", str(ROOT),
         "--experiment", str(private_file(tmp_path / "experiment.json", experiment)), "--scene-file", str(scene_path),
         "--budget-receipt", str(private_file(tmp_path / "budget.json", balance))],
        env=env, cwd=ROOT, capture_output=True, text=True, timeout=45)
    checks = json.loads(preflight.stdout)
    assert preflight.returncode == 2 and checks["local_input_checks"] == "BLOCKED_ENVIRONMENT"
    assert "authorized_limits.authorization_ref" in checks["missing_or_inconsistent"]
    assert checks["saved_shared_budget_receipt"] == "MATCHED_SAVED_NF_RECEIPT"
    assert checks["http_provider_requests_sent"] == checks["db_connections_opened"] == checks["scenes_dispatched"] == 0
    def scene_command(binding, filename, denied=None):
        return cli("run_supervisor.py", "--access-file", binding, "--register-scene", filename, denied=denied)

    scene_command(wrong_access, scene_path, denied="VIEW_DENIED")
    scene_command(access, private_file(tmp_path / "wrong-work.json", {**scene, "world_ref": other["world_ref"]}),
                  denied="work and branch")
    scene_command(access, private_file(tmp_path / "stale.json", {**scene, "world_ref": stale_world}),
                  denied="HEAD_CONFLICT")
    with kernel.db.uow(write=False) as repo:
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_trace_tasks")) == 0
    receipt = scene_command(access, scene_path)
    assert scene_command(access, scene_path) == receipt

    trace_id = receipt["interaction_id"]
    cli("p55_statistics.py", "--binding", wrong, "trace", "--trace-id", trace_id, denied="VIEW_DENIED")
    trace = cli("p55_statistics.py", "--binding", admin, "trace", "--trace-id", trace_id)
    events = [e for e in trace if e["event_type"] == "scene.registered"]
    assert len(events) == 1
    event = events[0]
    assert (event["event_id"], event["work_id"], event["branch_id"], event["world_revision_id"]) == (
        trace_id, world["work_id"], world["branch_id"], world["world_revision_id"])
    assert cli("p6_budget.py", "--binding", admin, "show", *budget_args) == balance
    runtime = RuntimeCatalog(ROOT / "runtime")
    skills = {}
    with kernel.db.uow(write=False) as repo:
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts")) == 0
        actor_tasks = receipt["actor_tasks"]
        assert set(actor_tasks) == set(first["required_actor_ids"])
        for actor, task_id in actor_tasks.items():
            job = repo.one("supervisor_jobs", {"task_id": task_id})["config"]
            assert job["knowledge_cut"] == {"plane": "BELIEF", "actor_id": actor, "known_at": point(30)}
            dispatch = job["dispatch"]
            assert dispatch["budget_id"] == budget and dispatch["token_reservation"] == 250
            agent = kernel.artifact(repo, dispatch["input_ref"], world["work_id"], kind="AGENT_INPUT")["payload"]
            snap = kernel.artifact(repo, agent["runtime_ref"], world["work_id"], kind="AGENT_RUNTIME")["payload"]
            registered = runtime.snapshot("character_actor", "character_action", snap["skill_name"])
            assert snap == registered["snapshot"] and agent["runtime_sha256"] == registered["sha256"]
            skills[actor] = {"skill": snap["skill_name"], "version": snap["skill_version"], "sha256": agent["runtime_sha256"]}
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_trace_tasks WHERE trace_id=:id"), {"id": trace_id}) == 6
        outbox = str(repo.conn.scalar(text("SELECT outbox_id FROM nf_outbox WHERE topic='audit.recorded' AND aggregate_id=:id"),
                                      {"id": trace_id}))
    relay = cli("run_eventhub.py", "relay", "--once", "--limit", 1000)
    assert relay["published"] > 0
    messages = server.client.xrange(STREAMS["observability"])
    assert any(json.loads(fields["envelope"])["event_id"] == outbox for _, fields in messages)
    record_testsuite_property("p6.register_only", json.dumps({
        "world_ref": world, "plan_ref": plan_ref, "budget": balance, "trace_id": trace_id,
        "preflight": {"shared_budget_receipt": checks["saved_shared_budget_receipt"],
                      "missing": checks["missing_or_inconsistent"]}, "redis_outbox_id": outbox, "actor_skills": skills, "registered_tasks": 6,
        "attempts": 0, "provider_calls": 0, "registration_replays": 1,
    }, sort_keys=True))
