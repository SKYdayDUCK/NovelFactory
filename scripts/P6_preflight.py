"""Offline checks for a P6 HTTP-model experiment and its actual first register-scene file."""
import argparse
import inspect
import json
import os
import shutil
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


def read_object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def git_id(root, ref):
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--verify", ref], text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def positive_integer(value):
    return type(value) is int and value > 0


def money(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
        if parsed.is_finite() and parsed >= 0 and parsed == parsed.quantize(Decimal("0.000001")):
            return parsed
    except InvalidOperation:
        pass
    return None


def budget_matches(receipt, scene, limits):
    world = scene.get("world_ref")
    if not isinstance(world, dict):
        return False
    amount = money(receipt.get("limit_amount"))
    held = money(receipt.get("reserved_amount"))
    spent = money(receipt.get("spent_amount"))
    requested = money(scene.get("reserve_amount"))
    counts = [receipt.get(name) for name in ("total_tokens", "reserved_tokens", "spent_tokens", "available_tokens")]
    if None in (amount, held, spent, requested) or not all(type(n) is int and n >= 0 for n in counts):
        return False
    total, reserved, used, available = counts
    attempt_tokens = scene.get("token_reservation")
    return (receipt.get("budget_id") == scene.get("budget_id") and
            receipt.get("work_id") == world.get("work_id") and
            receipt.get("currency") == limits.get("currency") and
            amount == money(limits.get("total_amount")) and amount - held - spent >= requested and
            total == limits.get("total_tokens") and total - reserved - used == available and
            positive_integer(attempt_tokens) and available >= attempt_tokens and
            type(receipt.get("unquantified_attempts")) is int and receipt["unquantified_attempts"] == 0)


def evaluate(root, experiment, scene, receipt=None):
    from novel_factory.application import scene_runtime
    from novel_factory.application.joint_handoff import validate_item_gift
    from novel_factory.application.planning import PlanningService
    from novel_factory.application.scene_continuation import validate_slots, validate_volume_end
    from novel_factory.contracts.registry import validate_contract
    from novel_factory.domain.canonical import fingerprint
    from novel_factory.domain.errors import DomainError
    from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter

    if not Path(scene_runtime.__file__).resolve().is_relative_to(root / "src"):
        raise ValueError("scene module outside requested checkout")
    errors = set()
    def check(field, condition):
        if not condition:
            errors.add(field)

    def valid(name, value):
        try:
            validate_contract(name, value)
        except DomainError:
            return False
        return True

    head = git_id(root, "HEAD")
    tree = git_id(root, "HEAD^{tree}")
    dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain",
                                     "--", "src", "contracts", "runtime", "scripts"], text=True)
    check("nf_source_sha", experiment.get("nf_source_sha") == head and not dirty)
    check("material_seed", type(experiment.get("material_seed")) is int)
    check("accepted_plan_ref", valid("ArtifactRef", experiment.get("accepted_plan_ref")))
    target = experiment.get("target_scene_count")
    check("target_scene_count", type(target) is int and 10 <= target <= 20)

    try:
        inspect.signature(scene_runtime.register_scene).bind(None, None, None, **scene)
        PlanningService._key(scene["idempotency_key"])
        slots = scene_runtime._participants(scene["participants"])
        if not 3 <= len(slots) <= 5:
            errors.add("first_scene.participants")
        if "allowed_item_gift" in scene:
            validate_item_gift(scene["allowed_item_gift"], slots)
        check("first_scene.subject_id", scene["subject_id"] in slots)
        limit = scene["scene_limit"]
        check("first_scene.scene_limit", type(limit) is int and 1 <= limit <= 32)
        if "following_scenes" in scene:
            validate_slots(scene["following_scenes"], limit)
        if "volume_end" in scene:
            validate_volume_end(scene["volume_end"])
            check("first_scene.final_window", "following_scenes" not in scene)
        deadline = scene.get("deadline_seconds", 3600)
        check("first_scene.deadline", type(deadline) is int and 1 <= deadline <= 86400)
        for name in ("volume_id", "budget_id", "destination_id"):
            check("first_scene." + name, valid("UUID", scene[name]))
        check("first_scene.world_ref", valid("WorldRef", scene["world_ref"]))
        check("first_scene.story_time", valid("StoryPoint", scene["story_time"]))
        scopes = scene["scope_ids"]
        check("first_scene.scope_ids", isinstance(scopes, list) and scopes and
              all(valid("UUID", scope) for scope in scopes))
        locations = [scene["destination_id"], *scene.get("alternative_destination_ids", [])]
        check("first_scene.locations", all(valid("UUID", item) for item in locations) and
              len(set(locations)) == len(locations) and scene["subject_id"] not in locations)
    except (DomainError, TypeError, KeyError, ValueError):
        errors.add("first_scene.register_scene_arguments")

    try:
        check("registered_runtime_skill_tree", experiment.get("skills_git_tree") ==
              git_id(root, "HEAD:runtime/skills"))
    except subprocess.CalledProcessError:
        errors.add("registered_runtime_skill_tree")
    labels = experiment.get("preregistered_conflict_labels")
    check("preregistered_conflict_labels", valid("ArtifactRef", labels))
    reviewers = experiment.get("reader_pseudonyms")
    readers_ready = (isinstance(reviewers, list) and all(nonempty(r) for r in reviewers) and
                     len({r.strip().casefold() for r in reviewers}) >= 2)

    auth = experiment.get("authorized_limits")
    if not isinstance(auth, dict):
        auth = {}
    check("authorized_limits.authorization_ref", nonempty(auth.get("authorization_ref")))
    # RuntimeTaskService.prepare currently uses USD for scene registrations.
    check("authorized_limits.currency", auth.get("currency") == "USD")
    limit, reserve = money(auth.get("total_amount")), money(scene.get("reserve_amount"))
    per_attempt = money(auth.get("max_amount_per_attempt"))
    check("authorized_limits.amounts", None not in (limit, reserve, per_attempt) and
          0 <= reserve <= per_attempt <= limit)
    tokens = auth.get("total_tokens")
    per = scene.get("token_reservation")
    output = auth.get("max_output_tokens_per_attempt")
    check("authorized_limits.tokens", all(positive_integer(x) for x in (tokens, per, output)) and
          output <= per <= tokens)

    config_hash = None
    route = "BLOCKED_ENVIRONMENT"
    try:
        backend = OpenAICompatibleAdapter.from_environment()
        backend.check_available()
        config_hash = fingerprint(backend.configuration)
        route = "CONFIGURED_NOT_DISPATCHED"
        check("authorized_limits.provider_endpoint", backend.endpoint == auth.get("provider_endpoint"))
        check("authorized_limits.model_name", backend.model == auth.get("model_name"))
        check("authorized_limits.output_vs_environment",
              positive_integer(output) and backend.max_output_tokens <= output)
    except (DomainError, ValueError, OverflowError):
        errors.add("model_route.environment")
    check("authorized_limits.provider_endpoint", nonempty(auth.get("provider_endpoint")))
    check("authorized_limits.model_name", nonempty(auth.get("model_name")))
    adapter_id = scene.get("adapter_id")
    check("first_scene.registered_adapter", adapter_id in
          ("external", "codex-text", "codex-role-mcp", "codex-role-tools"))
    if adapter_id in ("codex-text", "codex-role-mcp", "codex-role-tools"):
        check("codex_host_binary", shutil.which("codex"))
    if adapter_id in ("codex-role-mcp", "codex-role-tools"):
        relay = os.environ.get("NF_ROLE_MCP_RELAY")
        check("role_mcp_relay", relay and Path(relay).is_file() and os.access(relay, os.X_OK))

    budget_status = "NOT_SUPPLIED"
    if receipt is not None:
        if budget_matches(receipt, scene, auth):
            budget_status = "MATCHED_SAVED_NF_RECEIPT"
        else:
            budget_status = "MISMATCHED_OR_INSUFFICIENT"
            errors.add("shared_budget.receipt")

    return {
        "saved_shared_budget_receipt": budget_status,
        "nf_source_sha": head, "nf_tree": tree, "http_route": route,
        "backend_config_sha256": config_hash, "local_input_checks":
        "BLOCKED_ENVIRONMENT" if errors else "INPUTS_MATCH_LOCAL_CHECKS",
        "missing_or_inconsistent": sorted(errors),
        "distinct_readers_listed": readers_ready,
        "deployment_authority_and_runtime_quota": "REQUIRES_LIVE_VERIFICATION",
        "literary_acceptance": "NOT_EVALUATED", "scenes_dispatched": 0,
        "http_provider_requests_sent": 0, "production_model_requests_sent": 0,
        "actual_cost": None, "db_connections_opened": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nf-checkout", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--scene-file", type=Path, required=True)
    parser.add_argument("--budget-receipt", type=Path, help="Previously saved NF p6_budget.py show JSON")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.nf_checkout.resolve()
    def no_sockets(event, _args):
        if event.startswith("socket."):
            raise RuntimeError("Offline P6 preflight attempted network access")
    sys.addaudithook(no_sockets)
    sys.path.insert(0, str(root / "src"))
    try:
        if not (root / "scripts/run_supervisor.py").is_file():
            raise ValueError("missing NF registration entry")
        receipt = read_object(args.budget_receipt) if args.budget_receipt else None
        result = evaluate(root, read_object(args.experiment), read_object(args.scene_file), receipt)
    except (OSError, ValueError, ImportError, subprocess.CalledProcessError):
        parser.exit(2, "Cannot read P6 files, NF source or installed NF dependencies.\n")
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if result["local_input_checks"] == "INPUTS_MATCH_LOCAL_CHECKS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
