"""Preview, enroll, and resume natural-language intake with native model provenance."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from bootstrap_p5_m0 import save

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.idea_intake import (
    design_round,
    enroll_idea,
    intake_receipt,
    materialize_design,
    parse_inspiration,
)
from novel_factory.application.kernel import Kernel, require
from novel_factory.application.model_routing import (
    POLICY_ROUTE,
    activate,
    check_idea_models,
    idea_policy,
)
from novel_factory.application.production_profiles import compile_dag
from novel_factory.application.rolling_production import register_rolling
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.native_files import file_lock
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.private_files import prepare_private_directory, read_private_bytes
from novel_factory.infrastructure.runtime_providers import load_runtime_profiles


def configured_gateway(kernel, route, review_route=None):
    if os.environ.get("NF_RUNTIME_PROFILES"):
        adapters = load_runtime_profiles(os.environ["NF_RUNTIME_PROFILES"], HostJournal(kernel))
    else:
        adapters = {}
    legacy = {route, review_route} & {"external", "codex-text"}
    if legacy:
        require(not legacy.intersection(adapters), "INVALID_MODEL_CONFIG", "Ambiguous provider name")
        adapter = OpenAICompatibleAdapter.from_environment()
        for name in legacy:
            adapters[name] = (
                adapter if name == "external" else CodexAppServerAdapter(adapter, HostJournal(kernel))
            )
    check_idea_models(adapters, route, review_route)
    return AgentGateway(kernel, adapters)


def preview(inspiration, adapter_id, review_adapter_id=None, production_mode=None):
    profile = parse_inspiration(inspiration, production_mode)
    try:
        configured_gateway(None, adapter_id, review_adapter_id)
    except DomainError as error:
        readiness = error.code
    else:
        readiness = "CONFIGURED_NOT_DISPATCHED"
    return {
        "input_sha256": fingerprint(inspiration),
        "profile": profile,
        "dag": compile_dag(profile),
        "route_status": readiness,
        **({"model_routes": {"creative": adapter_id, "review": review_adapter_id}}
           if review_adapter_id is not None else {}),
        "tasks_enrolled": 0,
        "model_requests": 0,
        "stage": "PREFLIGHT",
        "internal_releases": 0,
    }


def _load_private(path):
    return json.loads(read_private_bytes(path, 1000000))


def _requested_routes(args):
    # The marker survives restarts before either branch exists. Enrolled tasks
    # obtain their actual model from the immutable AgentInput in the database.
    if args.action in {"advance", "register-window", "authorize-ending-fix"}:
        saved = _load_private(args.private_dir / "project.json").get("model_routes")
        if saved:
            require(args.adapter in (None, saved["creative"]) and
                    args.review_adapter in (None, saved["review"]),
                    "IDEMPOTENCY_MISMATCH", "Requested routes differ from the enrolled idea")
            return saved["creative"], saved["review"]
    return args.adapter or "codex-text", args.review_adapter


def _project_control(kernel, directory, filename, token, wr):
    binding = {"work_id": wr["work_id"], "branch_id": wr["branch_id"]}
    save(
        directory,
        filename,
        {
            "token": token,
            "binding": binding,
            "object_root": str(kernel.objects.root),
            "database_url": kernel.db.engine.url.render_as_string(hide_password=False),
        },
    )


def _register_window(kernel, gateway, private, allowance):
    access = _load_private(private / "supervisor.json")
    owner = kernel.authenticate(access["token"])
    work, branch = access["work_id"], access["branch_id"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        bound = kernel.in_transaction(repo)
        repo.one("works", {"work_id": work}, lock="update")
        bound.authorize(repo, owner, work, "ADMIN", branch)
        source = bound._prior(repo, owner, work, "P6_IDEA_PROVENANCE", "initial", {})
        require(
            source and source["world_ref"]["branch_id"] == branch,
            "VIEW_DENIED",
            "No initial plan was materialized in this branch",
        )
        old = bound._prior(repo, owner, work, "P6_FIRST_WINDOW", "initial", allowance)
        if old:
            return old
        budget_id = bound.tasks.create_budget(
            work, allowance["total_amount"], currency=allowance["currency"]
        )
        SharedBudgetService(bound).set_limit(
            owner, work, branch, budget_id, allowance["total_tokens"]
        )
        profile = source["profile"]
        require("production_mode" not in allowance or allowance["production_mode"] == profile.get("production_mode", "standard"),
                "TASK_REBIND_REQUIRED", "Window must retain the enrolled production mode")
        length = source["creative_input"]["length"]
        target = (
            length["min"] if isinstance(length, dict) and length["unit"] == "chapters" else None
        )
        native_chapters = sum(len(volume["chapters"]) for volume in source["volumes"])
        horizon = {"tick": max(target or native_chapters, native_chapters), "phase": 1}
        route = allowance["adapter_id"]
        review_route = allowance.get("review_adapter_id")
        if "model_routes" in source:
            require(route == source["model_routes"]["creative"] and
                    review_route in (None, source["model_routes"]["review"]),
                    "IDEMPOTENCY_MISMATCH", "Window routes differ from the enrolled idea")
            route = POLICY_ROUTE
        elif review_route is not None:
            check_idea_models(gateway.adapters, route, review_route)
            activate(bound, owner, source["world_ref"],
                     idea_policy(source["world_ref"], {"creative": route, "review": review_route}),
                     gateway.adapters, expected_revision=0)
            route = POLICY_ROUTE
        routes = dict.fromkeys(("draft", "review", "plan"), route)
        registration = {
            "profile": {
                "preset": profile["preset"],
                "genre": profile["genre"],
                "complexity": profile["complexity"],
                **({"overrides": {"production_mode": profile["production_mode"]}} if "production_mode" in profile else {}),
            },
            "world_ref": source["world_ref"],
            "series_id": source["series_id"],
            "volumes": source["volumes"],
            "story_time": horizon,
            "subject_ids": source["subject_ids"],
            "predicates": source["predicates"],
            "routes": routes,
            "budget_id": budget_id,
            "reserve_amount": allowance["reserve_amount"],
            "token_reservation": allowance["token_reservation"],
            "deadline_seconds": 86400,
            "idempotency_key": "intake-first-window",
            "target_chapters": target,
        }
        if source.get("opening_version"):
            from novel_factory.application.idea_opening import enroll_opening

            result = enroll_opening(bound, repo, gateway.with_kernel(bound), owner, source, registration)
        else:
            result = register_rolling(bound, gateway.with_kernel(bound), owner, **registration)
        response = {
            "budget_id": budget_id,
            "writing_registration": result,
            "world_ref": source["world_ref"],
        }
        bound._receipt(
            repo, owner, source["world_ref"], "P6_FIRST_WINDOW", "initial", allowance, response
        )
        return response


def run_intake(mode, kernel, gateway, private, inspiration=None, allowance=None):
    """One private project directory serializes its local identity and file handoff."""
    prepare_private_directory(private)
    with file_lock(private / ".lock"):
        if mode == "register-window":
            return _register_window(kernel, gateway, private, allowance)
        marker_file = private / "project.json"
        if mode == "enroll":
            parse_inspiration(inspiration)
            scope = {"input": inspiration, "allowance": allowance}
            if not marker_file.exists():
                require(
                    not any(p.name != ".lock" for p in private.iterdir()),
                    "INVALID_ARGUMENT",
                    "Choose an empty private directory for this idea",
                )
                save(
                    private,
                    "project.json",
                    {"work_id": str(uuid4()), "request_hash": fingerprint(scope),
                     **({"model_routes": {"creative": allowance["adapter_id"],
                                          "review": allowance["review_adapter_id"]}}
                        if allowance.get("review_adapter_id") is not None else {})},
                )
            marker = _load_private(marker_file)
            require(
                marker["request_hash"] == fingerprint(scope),
                "IDEMPOTENCY_MISMATCH",
                "A different idea or allowance already owns this directory",
            )
            with kernel.objects.lock(), kernel.db.uow() as repo:
                bound = kernel.in_transaction(repo)
                if repo.one("works", {"work_id": marker["work_id"]}):
                    access = _load_private(private / "preparation.json")
                    receipt = intake_receipt(
                        bound, repo, bound.authenticate(access["token"]), access["world_ref"]
                    )
                    _project_control(
                        kernel,
                        private,
                        "preparation-control.json",
                        access["token"],
                        access["world_ref"],
                    )
                    return receipt
                created, receipt = enroll_idea(
                    bound, gateway, inspiration, allowance, marker["work_id"]
                )
                save(
                    private,
                    "preparation.json",
                    {"token": created["token"], "world_ref": created["world_ref"]},
                )
                _project_control(
                    kernel,
                    private,
                    "preparation-control.json",
                    created["token"],
                    created["world_ref"],
                )
                return receipt
        access = _load_private(private / "preparation.json")
        owner, wr = kernel.authenticate(access["token"]), access["world_ref"]
        if mode == "authorize-ending-fix":
            from novel_factory.application.idea_ending import authorize_fix
            return authorize_fix(kernel, gateway, owner, wr, allowance["review_attempt"])
        with kernel.db.uow(write=False) as repo:
            intake = intake_receipt(kernel, repo, owner, wr)
            _, designer, review = design_round(kernel, repo, owner, wr, intake)
            task = repo.one("tasks", {"task_id": (review or designer)["task_id"]})
        if mode == "status":
            return {
                "world_ref": wr,
                "task_id": task["task_id"],
                "generation": task["generation"],
                "state": task["state"],
                "context_bundle_id": task["context_bundle_id"],
            }
        repairable_design = (task["state"] == "FAILED" and intake.get("model_routes") and
                             (review is None or intake["profile"].get("production_mode") == "quality"))
        if mode == "advance" and task["state"] not in {"RESULT_STORED", "ACCEPTED"} and not repairable_design:
            # The supervisor controls call, budget, waiting, and lease recovery.
            return RuntimeSupervisor(kernel, gateway).run_once(
                owner, wr["work_id"], wr["branch_id"]
            )
        require(mode == "advance", "INVALID_ARGUMENT", "Unknown intake command")
        with kernel.objects.lock(), kernel.db.uow() as repo:
            bound = kernel.in_transaction(repo)
            created, receipt = materialize_design(bound, gateway, owner, wr)
            if created:
                seed = created["world_ref"]
                save(
                    private,
                    "supervisor.json",
                    {
                        "token": created["token"],
                        "work_id": seed["work_id"],
                        "branch_id": seed["branch_id"],
                    },
                )
            if receipt["status"] == "INITIAL_PLAN_MATERIALIZED":
                canonical = _load_private(private / "supervisor.json")
                literary_wr = receipt["world_ref"]
                literary_owner = bound.authenticate(canonical["token"])
                bound.authorize(
                    repo, literary_owner, literary_wr["work_id"], "ADMIN", literary_wr["branch_id"]
                )
                _project_control(kernel, private, "control.json", canonical["token"], literary_wr)
                _project_control(kernel, private, "preparation-control.json", access["token"], wr)
                save(private, "first-window.json", receipt)
            return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["preflight", "enroll", "advance", "status", "register-window", "authorize-ending-fix"]
    )
    parser.add_argument("--idea", type=Path, help="JSON containing only idea, length and genre")
    parser.add_argument("--production-mode", choices=("standard", "quality"), help="Freeze the revision policy on enrollment")
    parser.add_argument("--adapter", help="Creation profile (default: codex-text on enrollment)")
    parser.add_argument("--review-adapter", help="Use a distinct model profile for internal reviews")
    parser.add_argument("--private-dir", type=Path)
    parser.add_argument("--review-attempt", help="Exact rejected ending-review attempt authorized for one targeted correction")
    parser.add_argument(
        "--total-amount", help="Authorized monetary cap for the selected work, in USD"
    )
    parser.add_argument(
        "--reserve-amount", help="Authorized amount to reserve for one supervised attempt"
    )
    parser.add_argument("--total-tokens", type=int)
    parser.add_argument("--reserve-tokens", type=int)
    args = parser.parse_args()
    if args.action == "authorize-ending-fix" and not args.review_attempt:
        parser.error("--review-attempt is required for an ending correction")
    if args.action in {"preflight", "enroll"} and not args.idea:
        parser.error("--idea is required")
    if args.action != "preflight" and not args.private_dir:
        parser.error("--private-dir is required")
    if args.action in {"enroll", "register-window"} and None in (
        args.total_amount,
        args.reserve_amount,
        args.total_tokens,
        args.reserve_tokens,
    ):
        parser.error("specify the authorized budget and per-attempt reservations")
    db = None
    try:
        inspiration = json.loads(args.idea.read_text(encoding="utf-8")) if args.idea else None
        adapter, review_adapter = _requested_routes(args)
        if args.action == "preflight":
            result = preview(inspiration, adapter, review_adapter, args.production_mode)
        else:
            db = Database()
            kernel = Kernel(db, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")))
            gateway = None if args.action == "status" else configured_gateway(kernel, adapter, review_adapter)
            allowance = {
                "adapter_id": adapter,
                **({"review_adapter_id": review_adapter} if review_adapter is not None else {}),
                "currency": "USD",
                **({"production_mode": args.production_mode} if args.production_mode else {}),
                "total_amount": args.total_amount,
                "reserve_amount": args.reserve_amount,
                "total_tokens": args.total_tokens,
                "token_reservation": args.reserve_tokens,
            }
            if args.action == "authorize-ending-fix":
                allowance["review_attempt"] = args.review_attempt
            result = run_intake(
                args.action, kernel, gateway, args.private_dir, inspiration, allowance
            )
        print(json.dumps(result, ensure_ascii=False))
        return (
            0
            if result.get("route_status", "CONFIGURED_NOT_DISPATCHED")
            == "CONFIGURED_NOT_DISPATCHED"
            else 2
        )
    except DomainError as error:
        print(json.dumps({"error": error.code}, ensure_ascii=False))
        return 2
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
