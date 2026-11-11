"""Paired policy decisions on the same PostgreSQL fixture. No unsafe arm can publish."""
import argparse
import json
import os
from math import ceil
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from novel_factory.application.kernel import Kernel
from novel_factory.application.planning import validate_task_plan_binding
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task, load_fixture, point, prepare_command, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def sample(kernel):
    fixture = load_fixture(kernel)
    world, principal, ids = fixture["world_ref"], fixture["principal"], fixture["ids"]
    inputs = {}
    for scenario, read_subject, read_predicate in [
        ("same_object", "guard1", "on_duty"),
        ("causal_read_other_write", "guard1", "on_duty"),
        ("independent", "sword", "legal_owner"),
    ]:
        task = create_bound_task(kernel, principal, world, point(3), [ids[read_subject]], [read_predicate])
        query = {"kind": "STATE", "subject_ids": [ids[read_subject]], "predicates": [read_predicate]}
        WorldQueryService(kernel).query(principal, {"schema_version": "2.1", "task_id": task["task_id"],
            "generation": task["generation"], "read_contract": {"revision": {"mode": "FIXED", "world_ref": world},
            "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
            "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": point(3)},
            "query": query, "page_size": 20, "cursor": None})
        if scenario == "same_object":
            kind = "GUARD_LEAVE"
            args = {"guard_id": ids["guard1"], "region_id": ids["north"], "at": point(3)}
        else:
            kind = "RESOURCE_TRANSFER"
            args = {"from_account": ids["warehouse"], "to_account": ids["relief"],
                    "resource_id": ids["grain"], "quantity": "1", "unit": "bag", "at": point(3)}
        prepared = prepare_command(kernel, principal, world, kind, args, task=task, mode="DEPENDENCY_GUARDED")
        inputs[scenario] = (prepared, ids[read_subject], read_predicate)
    change = run_command(kernel, principal, world, "SET_RELATION", {"subject_id": ids["guard1"],
        "predicate": "on_duty", "object_value": {"kind": "BOOLEAN", "value": False}, "at": point(2)})
    return world, principal, inputs, change


def guard(kernel, repo, world, principal, prepared, changed, arm):
    if arm == "A":
        return kernel.head(repo, world["work_id"], world["branch_id"]) != world
    req = prepared["request"]
    prop = kernel._proposal(repo, principal, req)
    if arm == "B":
        # Object-level writes only. The fixture changes a relation on one entity.
        return bool(set(prop["footprint"]["process_scopes"]) & set(changed["footprint"]["process_scopes"]))
    task = repo.one("tasks", {"task_id": req["task_id"]})
    try:
        validate_task_plan_binding(repo, kernel, task)
        cert = kernel.artifact(repo, req["acceptance_certificate_ref"], world["work_id"])["payload"]
        approvals = kernel._approvals(repo, prop, req["approval_refs"], "DEPENDENCY_GUARDED")
        grant = kernel.authorize(repo, principal, world["work_id"], "ACCEPT", world["branch_id"])
        kernel._verify_tokens(repo, world, cert, prop["footprint"], grant, approvals,
                             repo.one("branches", {"branch_id": world["branch_id"]}),
                             repo.one("works", {"work_id": world["work_id"]}))
        if arm == "D":
            check_task_dependencies(repo, task["task_id"], task["generation"])
    except DomainError as exc:
        if exc.code not in {"DEPENDENCY_CONFLICT", "STALE_CERTIFICATE", "STALE_PRECONDITION", "STALE_PLAN"}:
            raise
        return True
    return False


def measure(kernel, repeats):
    rows = []
    world, principal, inputs, change = sample(kernel)
    with kernel.db.uow(write=False) as repo:
        changed = kernel._proposal(repo, principal, change["request"])
    for arm in "ABCD":
        observations = []
        for scenario, (prepared, subject, predicate) in inputs.items():
            durations = []
            for _ in range(repeats):
                started = perf_counter_ns()
                with kernel.db.uow(write=False) as repo:
                    affected = guard(kernel, repo, world, principal, prepared, changed, arm)
                durations.append((perf_counter_ns() - started) / 1_000_000)
            # Execute one deterministic context rebuild for each decision to invalidate.
            # No candidate produced by these alternative policies receives acceptance.
            if affected:
                create_bound_task(kernel, principal, change["world_ref"], point(3), [subject], [predicate])
            observations.append({"scenario": scenario, "oracle_affected": scenario != "independent",
                                 "invalidated": affected, "context_reexecutions": int(affected), "samples_ms": durations})
        samples = sorted(x for row in observations for x in row["samples_ms"])
        rows.append({"arm": arm, "observations": observations,
            "false_positives": sum(r["invalidated"] and not r["oracle_affected"] for r in observations),
            "false_negatives": sum(not r["invalidated"] and r["oracle_affected"] for r in observations),
            "affected_fraction": sum(r["invalidated"] for r in observations) / len(observations),
            "valid_retention_fraction": sum(not r["invalidated"] and not r["oracle_affected"] for r in observations),
            "context_reexecutions": sum(r["context_reexecutions"] for r in observations),
            "mean_decision_ms": mean(samples), "p95_decision_ms": samples[ceil(.95 * len(samples)) - 1],
            "p99_decision_ms": samples[ceil(.99 * len(samples)) - 1], "serial_decisions_per_second": 1000 / mean(samples),
            "model_calls": 0, "model_tokens": 0, "invoiced_cost": None})
    return {"unit": "guard decision including PostgreSQL transaction; one eligible independent task",
            "workload": "three labelled tasks in one world; latency repeats reuse the same receipts",
            "old_world": world, "new_world": change["world_ref"], "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=12, choices=range(1, 101))
    args = parser.parse_args()
    db = Database(os.environ["NF_TEST_DATABASE_URL"])
    try:
        with TemporaryDirectory() as root:
            result = measure(Kernel(db, ObjectStore(Path(root))), args.repeats)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    finally:
        db.close()


if __name__ == "__main__":
    main()
