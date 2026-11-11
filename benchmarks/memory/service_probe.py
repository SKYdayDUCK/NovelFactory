"""Exercise actual fixed-cut query/compiler services on PostgreSQL 17.

Use scripts/with_postgres.py --migrate -- ... for an isolated database. The
fixture calls the existing Kernel for every event acceptance, creates real
tasks/grants, and persists actual ContextBundles. No fixture selector replaces
server authorization. Comparison is focused vs broad compiler inputs, not an
LLM literary experiment or the offline ranker implemented in evaluate.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import tempfile
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import event, text

from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point, seed_sword_history
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def prepare(kernel, principal, world_ref, subjects, predicates, *, role, at=35, plane="WORLD", actor=None,
            manifest_creator=None):
    # The controller supplies selectors; a character never gains ARTIFACT_WRITE
    # merely to run this evaluation. The task owner remains the role principal.
    manifest = kernel.put_artifact(manifest_creator or principal, world_ref["work_id"],
                                  {"subject_ids": subjects, "predicates": predicates}, "INPUT_MANIFEST")
    task_id = str(uuid4())
    spec = {"schema_version": "2.0", "task_id": task_id, "generation": 0,
            "role_id": role, "task_kind": "P3_MEMORY_SERVICE_PROBE", "state": "READY",
            "world_ref": world_ref, "story_time": point(at), "context_bundle_id": None,
            "policy_version": "nf-policy-1", "dependency_fingerprint": None,
            "runtime_contract_version": "a" * 64, "capability_grant_id": principal.grant_id,
            "input_artifacts": [manifest], "budget": {"currency": "USD", "max_amount": "0", "token_budget": 50000},
            "wall_deadline": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    kernel.create_task(principal, spec, "memory-probe-task:" + task_id)
    task = kernel.claim_task(principal, "memory-probe-worker", lease_seconds=3600, task_id=task_id)
    cut = {"plane": plane}
    if plane == "BELIEF":
        cut.update(actor_id=actor, known_at=point(at))
    request = {"schema_version": "2.1", "task_id": task_id, "generation": task["generation"],
               "recipe_id": "ACTOR" if plane == "BELIEF" else "DIRECTOR", "input_manifest_ref": manifest,
               "read_contract": {"revision": {"mode": "FIXED", "world_ref": world_ref},
                                 "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
               "knowledge_cut": cut, "subject_time": {"kind": "POINT", "at": point(at)},
               "token_budget": 50000, "idempotency_key": "memory-probe-compile:" + task_id}
    return task, request


def query_request(task, wr, subjects, predicates, *, tick=35, actor=None):
    cut = {"plane": "WORLD"}
    if actor is not None:
        cut = {"plane": "BELIEF", "actor_id": actor, "known_at": point(tick)}
    return {"schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
            "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr},
                              "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
            "knowledge_cut": cut, "subject_time": {"kind": "POINT", "at": point(tick)},
            "query": {"kind": "STATE", "subject_ids": subjects, "predicates": predicates},
            "page_size": 100, "cursor": None}


def measure(db, function):
    count = 0

    def counted(*_args):
        nonlocal count
        count += 1

    event.listen(db.engine, "before_cursor_execute", counted)
    started = time.perf_counter_ns()
    try:
        result = function()
    finally:
        event.remove(db.engine, "before_cursor_execute", counted)
    return result, {"sql_statements": count,
                    "elapsed_ms": (time.perf_counter_ns() - started) / 1_000_000}


def expect_denial(function, expected_code):
    try:
        function()
    except DomainError as error:
        if error.code != expected_code:
            raise AssertionError(f"Expected {expected_code}, received {error.code}") from error
        return {"code": error.code, "status": "EXPECTED_REJECTION"}
    raise AssertionError("Expected rejection but request succeeded")


def run_probe(kernel, *, repeats=3):
    fixture = seed_sword_history(kernel, load_fixture(kernel))
    ids, wr, principal = fixture["ids"], fixture["world_ref"], fixture["principal"]
    query = WorldQueryService(kernel)
    with kernel.db.uow(write=False) as repo:
        before = {"head": kernel.head(repo, wr["work_id"]),
                  "event_count": len(repo.many("events", {"work_id": wr["work_id"]})),
                  "state_hash": kernel.revision(repo, wr)["state_hash"]}
    comparisons, traces, gates = [], {}, {}
    last_task = None
    for strategy in ("focused_state", "broad_state"):
        subjects = [ids["sword"]] if strategy == "focused_state" else [
            ids[key] for key in ("sword", "sword2", "protagonist", "friend", "rival", "north", "south", "warehouse")]
        predicates = ["possessed_by"] if strategy == "focused_state" else [
            "legal_owner", "possessed_by", "lifecycle", "active_use"]
        for repeat in range(repeats):
            task, request = prepare(kernel, principal, wr, subjects, predicates, role="control_operator")
            result, metrics = measure(kernel.db, lambda request=request: query.context_compile(principal, request))
            facts = [json.loads(fact["statement"]) for fact in result["compiled"]["bundle"]["facts"]]
            correct = any(fact["subject_id"] == ids["sword"] and fact["predicate"] == "possessed_by"
                          and fact["value"] == {"kind": "ENTITY", "entity_id": ids["rival"]} for fact in facts)
            assert correct, "Real compiler omitted the gold current holder"
            metrics.update(strategy=strategy, repeat=repeat, gold_exact_state_correct=correct,
                           source_world_ref=result["compiled"]["bundle"]["world_ref"],
                           section_bytes=sum(len(section["content"].encode()) for section in result["compiled"]["sections"]),
                           compiler_reported_estimate=result["compiled"]["estimated_tokens"],
                           tokenizer_version=result["compiled"]["tokenizer_version"],
                           context_bundle_id=result["compiled"]["bundle"]["context_bundle_id"],
                           context_sha256=hashlib.sha256(canonical_bytes(result)).hexdigest(),
                           hard_requirements_status=result["compiled"]["hard_requirements_status"])
            comparisons.append(metrics)
            traces[f"{strategy}:{repeat}"] = {"request": request, "response": result}
            assert query.context_compile(principal, request) == result
            last_task = task
    gates["focused_and_broad_exact_state"] = True
    gates["compiler_receipt_idempotency"] = True

    actor_grant = kernel.issue_grant(principal, wr["work_id"], role_id="character_actor",
                                     actor_id=ids["protagonist"], max_story_time=point(35))
    actor_principal = actor_grant["principal"]
    actor_task, actor_compile = prepare(kernel, actor_principal, wr, [ids["sword"]], ["possessed_by"],
                                        role="character_actor", plane="BELIEF", actor=ids["protagonist"],
                                        manifest_creator=principal)
    actor_context, actor_metrics = measure(kernel.db, lambda: query.context_compile(actor_principal, actor_compile))
    actor_facts = [json.loads(fact["statement"]) for fact in actor_context["compiled"]["bundle"]["facts"]]
    assert actor_facts[0]["value"] == {"kind": "ENTITY", "entity_id": ids["friend"]}
    assert ids["rival"] not in canonical_bytes(actor_context).decode()
    assert all(fact["status"] == "BELIEF" for fact in actor_context["compiled"]["bundle"]["facts"])
    gates["belief_does_not_inherit_objective_theft"] = True
    traces["actor_context"] = {"request": actor_compile, "response": actor_context, "metrics": actor_metrics}

    actor_read = query_request(actor_task, wr, [ids["sword"]], ["possessed_by"], actor=ids["protagonist"])
    # Exercise a warm service cache before revocation; the next access must still
    # perform current authorization rather than reuse a stale bearer result.
    query.query(actor_principal, actor_read)
    warm = query.query(actor_principal, actor_read)
    traces["actor_warm_query"] = warm
    world_attempt = deepcopy(actor_read)
    world_attempt["knowledge_cut"] = {"plane": "WORLD"}
    traces["actor_world_denial"] = expect_denial(lambda: query.query(actor_principal, world_attempt), "VIEW_DENIED")
    other_attempt = deepcopy(actor_read)
    other_attempt["knowledge_cut"]["actor_id"] = ids["friend"]
    traces["other_actor_denial"] = expect_denial(lambda: query.query(actor_principal, other_attempt), "VIEW_DENIED")
    gates["actor_world_and_other_actor_denied"] = True
    kernel.revoke_grant(principal, actor_principal.grant_id)
    traces["revoked_warm_query"] = expect_denial(lambda: query.query(actor_principal, actor_read), "VIEW_DENIED")
    traces["revoked_compile_receipt"] = expect_denial(lambda: query.context_compile(actor_principal, actor_compile), "VIEW_DENIED")
    gates["warm_cache_and_compile_receipt_reauthorize"] = True

    historical = query.query(principal, query_request(last_task, wr, [ids["sword"]], ["possessed_by"], tick=5))
    assert historical["payload"]["state_items"][0]["answer"]["data"] == {"kind": "ENTITY", "entity_id": ids["smith"]}
    traces["historical_before_ownership_changes"] = historical
    old_wr = fixture["loan"]["world_ref"]
    pending_old = query.query(principal, query_request(last_task, old_wr, [ids["sword"]], ["possessed_by"], tick=35))
    assert pending_old["read_cut"]["world_ref"] == old_wr
    assert pending_old["payload"]["state_items"][0]["answer"]["evaluation_status"] == "PENDING"
    traces["old_revision_still_pending"] = pending_old
    gates["history_and_fixed_old_progress_cut"] = True
    destroyed = query.query(principal, query_request(last_task, wr, [ids["sword"]], ["active_use"], tick=41))
    future = query.query(principal, query_request(last_task, wr, [ids["sword"]], ["active_use"], tick=50))
    assert destroyed["payload"]["state_items"][0]["answer"]["value_status"] == "NOT_APPLICABLE"
    assert future["payload"]["state_items"][0]["answer"]["evaluation_status"] == "PENDING"
    assert future["payload"]["state_items"][0]["answer"]["value_status"] == "UNRESOLVED"
    traces["destroyed_not_unknown"] = destroyed
    traces["future_not_evaluated"] = future
    gates["destroyed_and_pending_distinct"] = True

    budget_task, budget_request = prepare(kernel, principal, wr, [ids["sword"]], ["possessed_by"], role="control_operator")
    budget_request["token_budget"] = 256
    traces["budget_denial"] = expect_denial(lambda: query.context_compile(principal, budget_request), "CONTEXT_INCOMPLETE")
    with kernel.db.uow(write=False) as repo:
        assert repo.one("contexts", {"task_id": budget_task["task_id"]}) is None
        assert repo.one("tasks", {"task_id": budget_task["task_id"]})["context_bundle_id"] is None
    gates["hard_context_budget_does_not_bind_partial"] = True
    with kernel.db.uow(write=False) as repo:
        after = {"head": kernel.head(repo, wr["work_id"]),
                 "event_count": len(repo.many("events", {"work_id": wr["work_id"]})),
                 "state_hash": kernel.revision(repo, wr)["state_hash"]}
    assert after == before
    gates["retrieval_has_zero_world_effects"] = True
    with kernel.db.engine.connect() as conn:
        database_info = {name: str(conn.scalar(text("SHOW " + name)))
                         for name in ("server_version", "fsync", "synchronous_commit", "max_connections")}
    assert database_info["server_version"].startswith("17."), "Real PostgreSQL 17 is required"
    return {"status": "PASS_ENGINEERING_ONLY", "model_status": "NOT_EXECUTED",
            "fixture": "r1_sword: formal event acceptance history", "database": database_info,
            "python": platform.python_version(), "repeats_per_strategy": repeats,
            "platform": platform.platform(), "synthetic_entity_ids": ids,
            "seed_event_receipts": [step["receipt"] for step in fixture["steps"]],
            "implementation_sha256": {
                "query_service": hashlib.sha256(Path(__file__).resolve().parents[2].joinpath(
                    "src/novel_factory/application/queries.py").read_bytes()).hexdigest(),
                "probe": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
            "sql_count_scope": "SQLAlchemy before_cursor_execute during context_compile only; task preparation excluded",
            "comparisons": comparisons, "gates": gates, "before": before, "after": after,
            "traces": traces, "real_model_calls": 0, "provider_tokens": None, "model_cost": None,
            "limitations": ["Small real-service correctness/cost probe, not a 50/200 Agent capacity test.",
                            "Compiler focused/broad strategies are separate from offline literary retrieval methods.",
                            "Compiler estimated_tokens uses its declared UTF-8 bound, not a provider tokenizer.",
                            "No generated prose quality or independent author judgment is measured."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1 or args.repeats > 20:
        parser.error("repeats must be between 1 and 20")
    if not os.environ.get("NF_DATABASE_URL"):
        parser.error("Use an isolated migrated PostgreSQL 17 database via NF_DATABASE_URL")
    args.output.mkdir(parents=True, exist_ok=True)
    db = Database()
    try:
        with tempfile.TemporaryDirectory(prefix="nf-memory-probe-objects-") as objects:
            result = run_probe(Kernel(db, ObjectStore(objects)), repeats=args.repeats)
        (args.output / "service_probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: value for key, value in result.items() if key != "traces"}, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
