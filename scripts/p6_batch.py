"""Enroll three independent P6 projects and serve each through its native supervisor."""
import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Event
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bootstrap_p5_m0 import save
from p6_idea import configured_gateway, run_intake
from sqlalchemy import text

from novel_factory.application import frozen_editing
from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.kernel import Kernel, require
from novel_factory.application.production_pipeline import (
    pipeline_status,
    production_material_reader,
)
from novel_factory.application.rolling_production import rolling_status
from novel_factory.application.statistics import is_official_deepseek
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.native_files import file_lock
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.private_files import prepare_private_directory, read_private_bytes
from novel_factory.infrastructure.supervisor_wake import wait_for_change, wake_listener

ROOT = Path(__file__).resolve().parents[1]
BOOKS = {"A": "short_supernatural", "B": "medium_xuanhuan", "C": "medium_long_infinite"}
ROUTES = {"creative": "deepseek_flash_writer", "review": "deepseek_pro_reviewer"}
MODELS = {"creative": "deepseek-flash", "review": "deepseek-v4-pro"}
HARNESS = CreativeHarnessConfig(enabled=True, dynamic_agents=True, adaptive_instructions=True)
FIELDS = {"total_amount", "reserve_amount", "total_tokens", "token_reservation"}
PAUSES = {"CONTINUITY_REVIEW_RETURNED", "CONTINUITY_REVIEW_BLOCKED", "PAUSED", "STRATEGY_REASSESSMENT_REQUIRED", "DESIGN_REWORK_REQUIRED", "DESIGN_SCHEMA_REWORK_REQUIRED", "OPENING_REVIEW_REQUIRED",
          "EDITING_RETURNED", "EDITING_BLOCKED", "NEXT_PLAN_BLOCKED"}


def private(path):
    return json.loads(read_private_bytes(path, 2_000_000))


def now():
    return datetime.now(UTC).isoformat()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def check_models(gateway):
    """Only GET /models. A successful lookup is separate from a production attempt."""
    verified = {}
    for role, route in ROUTES.items():
        adapter = gateway.adapters[route]
        config = adapter.configuration
        require(is_official_deepseek(config) and config["model"] == MODELS[role],
                "INVALID_MODEL_CONFIG", "The official production model must match its assigned role")
        key = adapter.profile.preflight()
        if key not in verified:
            request = Request("https://api.deepseek.com/models", headers={"Authorization": "Bearer " + key})
            try:
                with build_opener(NoRedirect()).open(request, timeout=20) as response:
                    verified[key] = {row["id"] for row in json.load(response)["data"]}
            except (URLError, KeyError, ValueError, TimeoutError) as exc:
                raise DomainError("MODEL_UNAVAILABLE", "Provider catalog check failed") from exc
        require(config["model"] in verified[key], "MODEL_UNAVAILABLE", "Model missing from account catalog")


def limits(value):
    require(isinstance(value, dict) and set(value) in (FIELDS, FIELDS | {"meter_per_dispatch"})
            and type(value.get("meter_per_dispatch", False)) is bool,
            "INVALID_ARGUMENT", "Invalid ledger settings")
    try:
        total, reserve = (Decimal(str(value[k])) for k in ("total_amount", "reserve_amount"))
        valid = total.is_finite() and reserve.is_finite() and 0 < reserve <= total < 10**14
    except (InvalidOperation, ValueError):
        valid = False
    require(valid and all(type(value[k]) is int for k in ("total_tokens", "token_reservation"))
            and 0 < value["token_reservation"] <= value["total_tokens"] < 2**63,
            "INVALID_ARGUMENT", "Invalid ledger limits")
    return {**value, "total_amount": str(total), "reserve_amount": str(reserve),
            "adapter_id": ROUTES["creative"], "review_adapter_id": ROUTES["review"],
            "currency": "USD", "production_mode": "quality"}


def read_allowances(path):
    config = private(path)
    require(isinstance(config, dict) and set(config) == set(BOOKS), "INVALID_ARGUMENT", "Expected A/B/C allowances")
    for key, phases in config.items():
        require(isinstance(phases, dict) and set(phases) == {"preparation", "writing"},
                "INVALID_ARGUMENT", "Each phase has its own authorized ledger")
        config[key] = {name: limits(values) for name, values in phases.items()}
    return config


def source_release():
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    require(not dirty, "SOURCE_CHANGED", "Use a clean frozen checkout with private files outside the repository")
    return sha


def freeze(kernel, gateway, root, books, allowances):
    settings = {"source_sha": source_release(), "models": {k: gateway.adapters[v].configuration for k, v in ROUTES.items()},
                "harness": gateway.creative_harness.as_dict(), "ideas": {k: fingerprint(v) for k, v in books.items()},
                "allowances": allowances, "objects": str(kernel.objects.root),
                "database": kernel.db.engine.url.render_as_string(hide_password=True)}
    with file_lock(root / ".batch.lock"):
        path = root / "batch.json"
        if path.exists():
            require(private(path) == settings, "IDEMPOTENCY_MISMATCH", "Batch inputs or release have changed")
        else:
            require(not any(p.name != ".batch.lock" for p in root.iterdir()), "INVALID_ARGUMENT",
                    "Choose an unused private directory for a fresh batch")
            save(root, "batch.json", settings)
        # Registration is idempotent. Workers only see the ready marker after all three commits.
        ids = {}
        for book in BOOKS:
            result = run_intake("enroll", kernel, gateway, root / book, books[book], allowances[book]["preparation"])
            ids[book] = {"work_id": result["world_ref"]["work_id"], "branch_id": result["world_ref"]["branch_id"],
                         "task_id": result["task_id"]}
        require(len({r["work_id"] for r in ids.values()}) == 3, "INVALID_EVIDENCE", "Duplicate preparation identity")
        save(root, "enrolled.json", ids)
        return ids


def provision_metered_work(kernel, project, preparation):
    """Only for an explicitly metered phase. Preserve provider holds and the exact task reservation."""
    binding = private(project / ("preparation.json" if preparation else "supervisor.json"))
    wr = binding.get("world_ref", binding)
    owner = kernel.authenticate(binding["token"])
    with kernel.db.uow(write=False) as repo:
        kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
        jobs = repo.conn.execute(text("""
            SELECT DISTINCT j.config->'dispatch'->>'budget_id' AS budget,
                            j.config->'dispatch'->>'reserve_amount' AS amount,
                            (j.config->'dispatch'->>'token_reservation')::bigint AS tokens
            FROM nf_supervisor_jobs j JOIN nf_tasks t USING(task_id)
            WHERE t.work_id=:work AND t.branch_id=:branch AND t.state='READY'
        """), {"work": wr["work_id"], "branch": wr["branch_id"]}).all()
    for budget, amount, tokens in jobs:
        SharedBudgetService(kernel).provision_next(owner, wr["work_id"], wr["branch_id"], budget, amount, tokens)


def writing_step(kernel, gateway, project, allowance):
    auth = private(project / "supervisor.json")
    work, branch = auth["work_id"], auth["branch_id"]
    owner = kernel.authenticate(auth["token"])
    if not (project / "writing-registration.json").exists():
        receipt = run_intake("register-window", kernel, gateway, project, allowance=allowance)
        save(project, "writing-registration.json", receipt)
    if allowance.get("meter_per_dispatch"):
        provision_metered_work(kernel, project, False)
    result = RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
    if result["status"] == "BLOCKED" and result.get("task_id"):
        with kernel.db.uow(write=False) as repo:
            kernel.authorize(repo, owner, work, "ADMIN", branch)
            task = repo.one("tasks", {"task_id": result["task_id"], "work_id": work, "branch_id": branch})
            job = repo.one("supervisor_jobs", {"task_id": result["task_id"]})
        if task and task["state"] == "FAILED" and job and "production_node" in job["config"]:
            return {"status": "BOARD_DIAGNOSIS_PENDING", "task_id": task["task_id"], "reason": result.get("reason")}
    if (result["status"] == "PRODUCTION_HANDOFF" and result.get("state") == "STOPPED"
            and result.get("reason") in {"STRATEGY_REASSESSMENT_REQUIRED", "CALL_RECONCILIATION_REQUIRED"}):
        return {**result, "status": "PAUSED"}
    # Re-read authoritative state even after a crash between a checkpoint and editor enrollment.
    if result["status"] in {"IDLE", "ROLLING_HANDOFF", "EDITORIAL_APPROVED"}:
        with kernel.db.uow(write=False) as repo:
            kernel.authorize(repo, owner, work, "ADMIN", branch)
            selected = repo.one("selections", {"work_id": work, "selection_kind": "P6_ROLLING:" + branch})
        if selected:
            cursor = rolling_status(kernel, owner, work, branch)
            complete_text = cursor["frozen_chapters"] == cursor["target_chapters"]
            if cursor["state"] == "FROZEN" and (complete_text or cursor["reason"] == "RELEASE_INCOMPLETE"):
                edit = frozen_editing.start(kernel, owner, work, branch)
                if edit["status"] == "EDITORIAL_APPROVED" and complete_text:
                    from novel_factory.application.rolling_publication import final_review_request
                    final_review_request(kernel, owner, work, branch)
                    return {"status": "AWAITING_INDEPENDENT_REVIEW", "frozen_chapters": cursor["frozen_chapters"]}
                if edit["status"] in PAUSES:
                    return edit
            if result["status"] == "IDLE" and cursor["state"] == "WAITING_ACCEPTANCE":
                nodes = pipeline_status(kernel, owner, work, branch)["nodes"].values()
                halted = next((n for n in nodes if n["state"] == "STOPPED"), None)
                if halted and not any(n["state"] == "ENROLLED" for n in nodes):
                    return {"status": "PAUSED", "reason": halted.get("reason") or "REVIEW_REQUIRED",
                            "task_id": halted.get("task_id")}

    return result


def emit(project, result):
    public = {k: result[k] for k in ("status", "reason", "state", "task_id", "generation", "frozen_chapters")
              if k in result}
    public["at"] = now()
    save(project, "worker-state.json", public)
    print(json.dumps(public), flush=True)


def serve(kernel, gateway, root, book, stop):
    project = root / book
    manifest = private(root / "batch.json")
    require(manifest["source_sha"] == source_release() and manifest["harness"] == gateway.creative_harness.as_dict()
            and manifest["models"] == {k: gateway.adapters[v].configuration for k, v in ROUTES.items()}
            and manifest["database"] == kernel.db.engine.url.render_as_string(hide_password=True)
            and manifest["objects"] == str(kernel.objects.root), "SOURCE_CHANGED", "Frozen batch binding differs")
    identity = private(root / "enrolled.json")[book]
    access = private(project / "preparation.json")
    require(all(access["world_ref"][k] == identity[k] for k in ("work_id", "branch_id")),
            "IDEMPOTENCY_MISMATCH", "Wrong work binding")
    # One independent service per work. Process exit releases the local lock. The DB owns attempt recovery.
    with file_lock(project / ".worker.lock"), wake_listener(kernel.db.engine) as listener:
        while not stop.is_set():
            require(source_release() == manifest["source_sha"], "SOURCE_CHANGED", "Release changed during execution")
            prep = not (project / "first-window.json").exists()
            try:
                if prep:
                    if manifest["allowances"][book]["preparation"].get("meter_per_dispatch"):
                        provision_metered_work(kernel, project, True)
                    result = run_intake("advance", kernel, gateway, project)
                else:
                    result = writing_step(kernel, gateway, project, manifest["allowances"][book]["writing"])
            except DomainError as error:
                emit(project, {"status": "PAUSED", "reason": error.code})
                return 3
            emit(project, result)
            status = result["status"]
            if status == "AWAITING_INDEPENDENT_REVIEW":
                return 0
            if status in PAUSES:
                return 3
            if status in {"IDLE", "BLOCKED"}:
                if status == "BLOCKED" and result.get("reason") == "RUNTIME_CAPACITY_LIMIT":
                    # The quota path may queue a new generation only after recording a proven unsent attempt.
                    binding = private(project / ("preparation.json" if prep else "supervisor.json"))
                    wr = binding.get("world_ref", binding)
                    with kernel.db.uow(write=False) as repo:
                        kernel.authorize(repo, kernel.authenticate(binding["token"]), wr["work_id"], "ADMIN", wr["branch_id"])
                        task = repo.one("tasks", {"task_id": result["task_id"], "work_id": wr["work_id"],
                                                  "branch_id": wr["branch_id"]})
                    if task and task["state"] == "READY" and task["generation"] > result["generation"]:
                        wait_for_change(listener, 1)
                        continue
                if prep:
                    current = run_intake("status", kernel, None, project)
                    if (current["state"] == "FAILED" and status == "BLOCKED"
                            and current["task_id"] == result.get("task_id")
                            and result.get("reason") not in {"CALL_OUTCOME_UNKNOWN", "MODEL_TRANSPORT_UNKNOWN",
                                                             "CALL_RECONCILIATION_REQUIRED", "CALL_ALREADY_DISPATCHED"}):
                        # The intake service verifies the rejected response before granting a new attempt.
                        continue
                    if current["state"] in {"PAUSED", "FAILED", "CANCELLED"}:
                        emit(project, {"status": "PAUSED", "reason": result.get("reason") or current["state"],
                                       "task_id": current["task_id"]})
                        return 3
                if status == "BLOCKED":
                    return 3
                wait_for_change(listener, min(30, max(1, result.get("retry_after_seconds", 30))))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("preflight")
    start = sub.add_parser("enroll")
    start.add_argument("--private-dir", type=Path, required=True)
    start.add_argument("--allowances", type=Path, required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--private-dir", type=Path, required=True)
    worker.add_argument("--book", choices=BOOKS, required=True)
    args = parser.parse_args(argv)
    os.environ.setdefault("NF_RUNTIME_PROFILES", str(ROOT / "configs/deepseek-p6.example.json"))
    os.environ["NF_CREATIVE_HARNESS_CONFIG"] = json.dumps(HARNESS.as_dict())
    books = {k: json.loads((ROOT / "examples/p6" / directory / "inspiration.json").read_bytes())
             for k, directory in BOOKS.items()}
    db = None
    try:
        if args.action == "preflight":
            gateway = configured_gateway(None, ROUTES["creative"], ROUTES["review"])
            check_models(gateway)
            print(json.dumps({"status": "MODELS_AVAILABLE", "models": MODELS, "production_calls": 0}))
            return 0
        db = Database(pool_size=5)
        kernel = Kernel(db, ObjectStore(os.environ["NF_OBJECT_ROOT"]))
        gateway = configured_gateway(kernel, ROUTES["creative"], ROUTES["review"])
        gateway.material_reader = production_material_reader(kernel)
        prepare_private_directory(args.private_dir)
        if args.action == "enroll":
            check_models(gateway)
            ids = freeze(kernel, gateway, args.private_dir, books, read_allowances(args.allowances))
            print(json.dumps({"status": "ENROLLED", "preparation": ids, "production_calls": 0}))
            return 0
        import signal
        stop = Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        return serve(kernel, gateway, args.private_dir, args.book, stop)
    except DomainError as error:
        print(json.dumps({"status": "BLOCKED", "reason": error.code}), flush=True)
        return 3
    except (ValueError, KeyError, FileNotFoundError):
        print(json.dumps({"status": "BLOCKED", "reason": "INVALID_DEPLOYMENT_CONFIGURATION"}), flush=True)
        return 3
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
