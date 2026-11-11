"""Compare original and candidate compilers on the same real PostgreSQL cut.

The baseline class is loaded from the exact, hash-checked 0609f680 Git blob.
Both implementations use one Kernel and one fixed immutable WorldRef, with
separate real tasks. Complete facts, including evidence identities, must match.
Only task-derived query IDs and separately created input-artifact identities
are treated as task-local after their derivations/content are verified.

Run via scripts/with_postgres.py --migrate. This is a bounded engineering
experiment, not a capacity test or a real-model literary-quality evaluation.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import text

from benchmarks.context_profile import SQLMeter, make_world, measure, prepare
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.fixtures import entity_value, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "0609f680fad782c827dd3834a073f8f9bcddaf0d"
QUERY_PATH = "src/novel_factory/application/queries.py"
REPOSITORY_PATH = "src/novel_factory/infrastructure/repository.py"
BASELINE_QUERY_SHA256 = "4f7273352c88f61cf1b1e5ac6bc142bb2721a885f78989e590d60771ddc7832c"
UNCHANGED_CORE = (
    "src/novel_factory/application/kernel.py",
    "src/novel_factory/application/query_policy.py",
    "src/novel_factory/domain/world.py",
    "src/novel_factory/domain/canonical.py",
    "src/novel_factory/infrastructure/database.py",
    "src/novel_factory/orchestration/tasks.py",
)


def git_blob(path):
    return subprocess.check_output(["git", "show", BASELINE_COMMIT + ":" + path], cwd=ROOT)


def method_ast(source, class_name, method_name):
    owner = next(node for node in ast.parse(source).body
                 if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in owner.body
                  if isinstance(node, ast.FunctionDef) and node.name == method_name)
    return ast.dump(method, include_attributes=False)


def load_baseline(directory):
    source = git_blob(QUERY_PATH)
    digest = sha256(source).hexdigest()
    assert digest == BASELINE_QUERY_SHA256, "Baseline query source hash differs"
    path = Path(directory) / "queries_0609f680.py"
    path.write_bytes(source)
    assert sha256(path.read_bytes()).hexdigest() == digest
    name = "novel_factory.application._paired_baseline_0609f680"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.WorldQueryService


def source_checks():
    hashes = {}
    for path in UNCHANGED_CORE:
        current = (ROOT / path).read_bytes()
        assert current == git_blob(path), "Shared core changed relative to baseline: " + path
        hashes[path] = sha256(current).hexdigest()
    original = git_blob(REPOSITORY_PATH)
    current = (ROOT / REPOSITORY_PATH).read_bytes()
    assert method_ast(original, "KernelRepository", "add") == method_ast(
        current, "KernelRepository", "add"), "Baseline row insertion changed"
    for path in (QUERY_PATH, REPOSITORY_PATH, "benchmarks/context_profile.py",
                 "benchmarks/context_paired_profile.py"):
        hashes[path] = sha256((ROOT / path).read_bytes()).hexdigest()
    return {"baseline_commit": BASELINE_COMMIT, "baseline_query_sha256": BASELINE_QUERY_SHA256,
            "baseline_repository_sha256": sha256(original).hexdigest(),
            "original_repository_add_ast_unchanged": True,
            "shared_core_matches_baseline": list(UNCHANGED_CORE), "current_source_sha256": hashes}


def independent_world_identity_diagnosis(kernel):
    """Reproduce identity differences; do not reconstruct unretained old facts."""
    observations = []
    for _ in range(2):
        world, items, owner = make_world(kernel, 1)
        step = run_command(kernel, world["principal"], world["world_ref"], "SET_RELATION",
                           {"subject_id": items[0], "predicate": "legal_owner",
                            "object_value": entity_value(owner), "at": point(3)}, key="identity-diagnosis")
        world["world_ref"] = step["world_ref"]
        request = prepare(kernel, world, items)
        result = WorldQueryService(kernel).context_compile(world["principal"], request)
        facts = result["compiled"]["bundle"]["facts"]
        observations.append({"world_ref": world["world_ref"], "facts": facts,
                             "full_fact_sha256": fingerprint(facts),
                             "accepted_event_refs": step["receipt"]["accepted_event_refs"]})
    left, right = (row["facts"] for row in observations)
    assert [(f["statement"], f["status"]) for f in left] == [
        (f["statement"], f["status"]) for f in right]
    assert left[0]["evidence_refs"] and right[0]["evidence_refs"]
    assert left != right, "Independent identity reproduction unexpectedly identical"
    return {"status": "REPRODUCED_WITH_CURRENT_IMPLEMENTATION_IN_TWO_NEW_WORKS",
            "same_entity_ids_statements_statuses": True, "full_facts_identical": False,
            "observations": observations,
            "reason": "The later accepted SET_RELATION closes the initial relation and appends its "
                      "event reference. Querying the earlier point keeps that historical record's "
                      "evidence, qualified by the run's WorldRef. Independently generated event/" 
                      "world identities differ even when scalar values are equal.",
            "original_unpaired_limitation": "Old before/after JSON retained hashes, not full facts. "
                      "This is a new causal reproduction, not a recovered comparison of old facts."}


def world_identity(kernel, world):
    wr = world["world_ref"]
    with kernel.db.uow(write=False) as repo:
        revision = kernel.revision(repo, wr)
        return {"head": kernel.head(repo, wr["work_id"]), "state_hash": revision["state_hash"],
                "manifest_hash": revision["manifest_hash"], "scope_versions": revision["scope_versions"],
                "event_count": len(repo.many("events", {"work_id": wr["work_id"]}))}


def capture_persisted_material(kernel, world, request, compiled):
    """Verify task-local IDs, then retain complete facts and comparable guards."""
    bundle = compiled["compiled"]["bundle"]
    wr = world["world_ref"]
    assert bundle["world_ref"] == wr
    assert compiled["compiled"]["read_cut"]["world_ref"] == wr
    with kernel.db.uow(write=False) as repo:
        context = repo.one("contexts", {"context_bundle_id": bundle["context_bundle_id"]})
        dependency_rows = repo.many("dependencies", {"owner_type": "context", "owner_id": bundle["context_bundle_id"]})
        input_artifact = kernel.artifact(repo, request["input_manifest_ref"], wr["work_id"], kind="INPUT_MANIFEST")
    dependency = context["dependency_manifest"]
    assert fingerprint(dependency) == context["dependency_fingerprint"] == bundle["dependency_fingerprint"]
    assert context["payload"] == bundle
    subjects = set(input_artifact["payload"]["subject_ids"])
    queries = []
    for original in dependency["queries"]:
        entry = deepcopy(original)
        assert entry["scope_key"].startswith("ENTITY:")
        subject = entry["scope_key"].removeprefix("ENTITY:")
        assert subject in subjects
        assert entry.pop("query_id") == str(uuid5(UUID(request["task_id"]), subject))
        queries.append(entry)
    assert len(queries) == len(subjects)
    comparable_rows = sorted(
        ({key: row[key] for key in ("work_id", "branch_id", "owner_type", "scope_key", "observed_version", "dependency_kind")}
         for row in dependency_rows), key=lambda row: row["scope_key"])
    assert [{"scope_key": row["scope_key"], "version": row["observed_version"]}
            for row in comparable_rows] == dependency["scope_versions"]
    interpretation = compiled["compiled"]["read_cut"]["interpretation_ref"]
    assert dependency["artifact_refs"] == [request["input_manifest_ref"], interpretation]
    assert fingerprint(input_artifact["payload"]) == request["input_manifest_ref"]["sha256"]
    guard = {key: deepcopy(dependency[key]) for key in
             ("world_ref", "policy_version", "rule_version", "scope_versions", "unresolved_scopes")}
    guard.update(queries=queries, interpretation_ref=interpretation,
                 input_manifest_content=input_artifact["payload"],
                 input_manifest_content_sha256=request["input_manifest_ref"]["sha256"],
                 persisted_dependency_rows=comparable_rows)
    facts = bundle["facts"]
    return {"task_id": request["task_id"], "context_bundle_id": bundle["context_bundle_id"],
            "input_manifest_ref": request["input_manifest_ref"], "full_facts": facts,
            "full_fact_sha256": fingerprint(facts), "guard_material": guard,
            "guard_material_sha256": fingerprint(guard),
            "full_dependency_manifest": dependency,
            "verified_task_specific_query_ids": True,
            "scope_row_count": len(dependency_rows), "compiled_bytes": len(canonical_bytes(compiled))}


def run_pairs(kernel, baseline_class, meter, args, output):
    output["independent_world_identity_diagnosis"] = independent_world_identity_diagnosis(kernel)
    setup_started = time.perf_counter()
    world, items, owner = make_world(kernel, args.entities)
    receipts = []
    for index in range(args.history):
        step = run_command(kernel, world["principal"], world["world_ref"], "SET_RELATION",
                           {"subject_id": items[index], "predicate": "legal_owner",
                            "object_value": entity_value(owner), "at": point(3 + index)},
                           key=f"profile-history-{index}")
        world["world_ref"] = step["world_ref"]
        receipts.append(step["receipt"])
    output["setup_seconds_excluded_from_compilation"] = time.perf_counter() - setup_started
    output["dataset"] = {"entities": args.entities + 1, "genesis_relations": args.entities,
                          "accepted_world_events": args.history, "world_ref": world["world_ref"],
                          "fixture_namespace": "nf-context-profile-v1", "seed_event_receipts": receipts}
    output["world_before"] = world_identity(kernel, world)
    implementations = {"baseline": baseline_class(kernel), "candidate": WorldQueryService(kernel)}
    pairs = output["pairs"] = []
    for count in args.subjects:
        for repeat in range(args.repeats):
            # Prepare both real tasks outside timing. Neither immutable task is
            # rebound to the other implementation or reused as an idempotent hit.
            requests = {name: prepare(kernel, world, items[:count]) for name in implementations}
            order = ["baseline", "candidate"] if len(pairs) % 2 == 0 else ["candidate", "baseline"]
            pair = {"subjects": count, "repeat": repeat, "order": order, "samples": {}}
            pairs.append(pair)
            for name in order:
                request = requests[name]
                compiled, measured = measure(meter, "context_compile",
                    lambda name=name, request=request: implementations[name].context_compile(world["principal"], request))
                material = capture_persisted_material(kernel, world, request, compiled)
                assert len(material["full_facts"]) == count
                assert all(fact["status"] == "FACT" for fact in material["full_facts"])
                pair["samples"][name] = {**measured, **material}
            left, right = (pair["samples"][name] for name in ("baseline", "candidate"))
            pair["complete_facts_and_evidence_identical"] = left["full_facts"] == right["full_facts"]
            pair["verified_dependency_guards_identical"] = left["guard_material"] == right["guard_material"]
            assert pair["complete_facts_and_evidence_identical"], "Full factual/evidence content changed"
            assert pair["verified_dependency_guards_identical"], "Dependency guard material changed"
            print(json.dumps({"subjects": count, "repeat": repeat, "order": order,
                "baseline_sql": left["sql"]["statements"], "candidate_sql": right["sql"]["statements"],
                "full_facts_equal": True, "guards_equal": True}), flush=True)
    output["world_after"] = world_identity(kernel, world)
    assert output["world_before"] == output["world_after"], "Compilation changed world identity"
    output["all_complete_facts_and_evidence_identical"] = True
    output["all_verified_dependency_guards_identical"] = True
    output["compilation_has_zero_world_effects"] = True
    summary = []
    for count in args.subjects:
        group = [pair for pair in pairs if pair["subjects"] == count]
        medians = {name: statistics.median(pair["samples"][name]["elapsed_seconds"] for pair in group)
                   for name in implementations}
        summary.append({"subjects": count, "pairs": len(group), "median_seconds": medians,
            "median_elapsed_reduction_percent": 100 * (1 - medians["candidate"] / medians["baseline"]),
            "paired_elapsed_differences_seconds": [pair["samples"]["baseline"]["elapsed_seconds"] -
                pair["samples"]["candidate"]["elapsed_seconds"] for pair in group],
            "sql_counts": {name: sorted({pair["samples"][name]["sql"]["statements"] for pair in group})
                           for name in implementations},
            "dependency_insert_counts": {name: sorted({pair["samples"][name]["sql"]["dependency_insert_statements"]
                                                       for pair in group}) for name in implementations}})
    output["summary"] = summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entities", type=int, default=1000)
    parser.add_argument("--history", type=int, default=12)
    parser.add_argument("--subjects", type=int, nargs="+", default=[1, 20, 100])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not (100 <= args.entities <= 10000 and 1 <= args.history <= 100 and
            all(1 <= count <= min(100, args.entities) for count in args.subjects) and
            len(set(args.subjects)) == len(args.subjects) and 1 <= args.repeats <= 30):
        parser.error("bounded fixture: 100..10000 entities, 1..100 history, unique 1..100 subjects, 1..30 repeats")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sources = source_checks()
    output = {"schema_version": "context-paired-profile-1", "status": "RUNNING",
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "execution_kind": "TEST_FIXTURE_REAL_POSTGRESQL", "sources": sources,
        "configuration": vars(args) | {"output": str(args.output)},
        "model_requests": 0, "model_cost": None,
        "comparison_policy": {
            "facts": "Strict equality of complete facts, including all evidence identities; no fields removed.",
            "query_ids": "Verify UUID5(task_id, subject_id) individually, then compare every other query field.",
            "input_artifacts": "Distinct real tasks own independently created manifests; verify full payload/hash "
                               "and exact expected artifact refs. Interpretation ref is shared and compared exactly.",
            "dependency_rows": "Compare every scope/version/type/work/branch; context and dependency row IDs are local.",
            "order": "Alternate baseline/candidate first for every pair. Task setup and persistence inspection excluded."},
        "limitations": ["Same shared development host, not an isolated production benchmark or latency SLO.",
            "One workload, one process, five default pairs per size; no confidence interval or 200-Agent claim.",
            "PostgreSQL/OS buffers remain warm; no buffer flush or cold-disk claim.",
            "Payload hash reuse and SQL batching change together; their timing effects are not isolated.",
            "Baseline query class uses the shared current repository with verified unchanged original add method.",
            "Original unpaired runs remain recorded and are not promoted to strict semantic equality."]}
    db = Database(pool_size=4)
    meter = SQLMeter(db.engine)
    try:
        with db.engine.connect() as conn:
            settings = {name: conn.scalar(text("SHOW " + name)) for name in
                        ("max_connections", "shared_buffers", "fsync", "synchronous_commit")}
            postgres = conn.scalar(text("SELECT version()"))
        output["environment"] = {"platform": platform.platform(), "python": platform.python_version(),
            "cpu_count": os.cpu_count(), "cpu_models": sorted({line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")}),
            "postgres": postgres, "settings": settings, "pool_size": 4, "max_overflow": 0,
            "sql_count_scope": "SQLAlchemy cursor execution during context_compile only; excludes task setup, "
                               "post-compile inspection, pool pre-ping and result decoding"}
        assert "PostgreSQL 17." in postgres
        with tempfile.TemporaryDirectory(prefix="nf-context-paired-") as directory:
            baseline = load_baseline(directory)
            kernel = Kernel(db, ObjectStore(Path(directory) / "objects"))
            run_pairs(kernel, baseline, meter, args, output)
        assert sources == source_checks(), "Compared source files changed during measurement"
        output["status"] = "PASS_CANDIDATE_MEASUREMENT_ONLY"
    except Exception as error:
        output["status"] = "FAIL"
        output["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        db.close()
        output["finished_at_utc"] = datetime.now(UTC).isoformat()
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": output["status"],
                      "summary": output["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
