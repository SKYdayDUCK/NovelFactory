"""Real PostgreSQL ContextCompiler/JSONB baseline, without external models.

Run inside scripts/with_postgres.py --migrate. Source data are generated through
the trusted bootstrap and Kernel services. Setup is excluded from timings; every
compile uses a new actual task/input and persists its full dependency manifest.
"""
from __future__ import annotations

import argparse
import cProfile
import json
import os
import platform
import pstats
import statistics
import subprocess
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import event, text

from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.fixtures import entity_value, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


class SQLMeter:
    def __init__(self, engine):
        self.engine = engine
        self.reset()
        event.listen(engine, "before_cursor_execute", self.before)
        event.listen(engine, "after_cursor_execute", self.after)

    def reset(self):
        self.count = 0
        self.seconds = 0.0
        self.kinds = Counter()
        self.dependencies_inserts = 0

    def before(self, conn, cursor, statement, parameters, context, executemany):
        context.nf_profile_start = time.perf_counter()
        self.count += 1
        self.kinds[statement.split()[0].upper()] += 1
        self.dependencies_inserts += int(statement.lstrip().startswith("INSERT INTO nf_dependencies"))

    def after(self, conn, cursor, statement, parameters, context, executemany):
        self.seconds += time.perf_counter() - context.nf_profile_start

    def result(self):
        return {"statements": self.count, "cursor_execution_seconds": self.seconds,
                "statement_kinds": dict(self.kinds), "dependency_insert_statements": self.dependencies_inserts,
                "excludes_pool_pre_ping_and_result_decoding": True}


def make_world(kernel, count):
    def uid(name):
        return str(uuid5(NAMESPACE_URL, "nf-context-profile-v1:" + name))
    owner = uid("owner")
    items = [uid(f"item-{i}") for i in range(count)]
    entities = {eid: {"entity_id": eid, "entity_kind": "ITEM", "name": f"测试物品{i}",
                      "created_at": point(0)} for i, eid in enumerate(items)}
    entities[owner] = {"entity_id": owner, "entity_kind": "CHARACTER", "name": "保管者", "created_at": point(0)}
    genesis = {"entities": entities, "relations": [
        {"subject_id": eid, "predicate": "legal_owner", "object_value": entity_value(owner),
         "valid_time": {"start": point(0), "end": None, "bounds": "[)"}} for eid in items
    ], "resources": [], "cognitions": [], "lifecycle": {}}
    world = kernel.bootstrap_work("TEST_FIXTURE:context-profile", genesis,
                                  frontiers={eid: point(2) for eid in entities})
    return world, items, owner


def prepare(kernel, world, subjects):
    principal, wr = world["principal"], world["world_ref"]
    manifest = kernel.put_artifact(principal, wr["work_id"],
        {"subject_ids": subjects, "predicates": ["legal_owner"]}, "INPUT_MANIFEST")
    task_id = str(uuid4())
    spec = {"schema_version": "2.0", "task_id": task_id, "generation": 0,
            "role_id": "control_operator", "task_kind": "CONTEXT_PROFILE", "state": "READY",
            "world_ref": wr, "story_time": point(1), "context_bundle_id": None,
            "policy_version": "nf-policy-1", "dependency_fingerprint": None,
            "runtime_contract_version": "a" * 64, "capability_grant_id": principal.grant_id,
            "input_artifacts": [manifest], "budget": {"currency": "USD", "max_amount": "0", "token_budget": 200000},
            "wall_deadline": (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
    kernel.create_task(principal, spec, task_id)
    claim = kernel.claim_task(principal, "context-profile", lease_seconds=3600, task_id=task_id)
    request = {"schema_version": "2.1", "task_id": task_id, "generation": claim["generation"],
               "recipe_id": "DIRECTOR", "input_manifest_ref": manifest,
               "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr}, "process_policy": "REQUIRE_EVALUATED",
                                 "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
               "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": point(1)},
               "token_budget": 200000, "idempotency_key": task_id}
    return request


def measure(meter, label, callback, *, profile=False):
    meter.reset()
    profiler = cProfile.Profile() if profile else None
    started = time.perf_counter()
    if profiler:
        profiler.enable()
    value = callback()
    if profiler:
        profiler.disable()
    elapsed = time.perf_counter() - started
    record = {"operation": label, "elapsed_seconds": elapsed, "sql": meter.result()}
    if profiler:
        stats = pstats.Stats(profiler)
        record["profile"] = [{"file": Path(key[0]).name, "line": key[1], "function": key[2],
                              "calls": values[1], "self_seconds": values[2], "cumulative_seconds": values[3]}
                             for key, values in sorted(stats.stats.items(), key=lambda x: x[1][3], reverse=True)[:25]]
    return value, record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--entities", type=int, default=1000)
    parser.add_argument("--history", type=int, default=12)
    parser.add_argument("--subjects", type=int, nargs="+", default=[1, 20, 100])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not (100 <= args.entities <= 10000 and 0 <= args.history <= 100 and
            all(1 <= count <= min(100, args.entities) for count in args.subjects) and 1 <= args.repeats <= 30):
        parser.error("bounded fixture: 100..10000 entities, 0..100 history, 1..100 subjects, 1..30 repeats")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    db = Database(pool_size=4)
    meter = SQLMeter(db.engine)
    records = []
    with tempfile.TemporaryDirectory(prefix="nf-context-profile-objects-") as objects:
        try:
            kernel = Kernel(db, ObjectStore(objects))
            world, items, owner = make_world(kernel, args.entities)
            # Append actual accepted relation events for JSONB revision storage measurements.
            for index in range(args.history):
                step = run_command(kernel, world["principal"], world["world_ref"], "SET_RELATION",
                    {"subject_id": items[index], "predicate": "legal_owner", "object_value": entity_value(owner),
                     "at": point(3 + index)}, key=f"profile-history-{index}")
                world["world_ref"] = step["world_ref"]
            service = WorldQueryService(kernel)
            for count in args.subjects:
                for repeat in range(args.repeats):
                    request = prepare(kernel, world, items[:count])
                    compiled, record = measure(meter, "context_compile",
                        lambda request=request: service.context_compile(world["principal"], request))
                    facts = compiled["compiled"]["bundle"]["facts"]
                    assert len(facts) == count and all(fact["status"] == "FACT" for fact in facts)
                    record.update(subjects=count, repeat=repeat, facts=len(facts),
                                  fact_content_sha256=fingerprint(facts),
                                  compiled_bytes=len(canonical_bytes(compiled)))
                    records.append(record)
                request = prepare(kernel, world, items[:count])
                _, profile = measure(meter, "context_compile_profile",
                    lambda request=request: service.context_compile(world["principal"], request), profile=True)
                profile["subjects"] = count
                records.append(profile)
            request = prepare(kernel, world, items[:20])
            query = {key: request[key] for key in ("schema_version", "task_id", "generation", "read_contract", "knowledge_cut", "subject_time")}
            query.update(query={"kind": "STATE", "subject_ids": items[:20], "predicates": ["legal_owner"]}, page_size=100, cursor=None)
            for repeat in range(args.repeats + 1):
                result, record = measure(meter, "state_query_app_cold" if repeat == 0 else "state_query_app_warm",
                    lambda: service.query(world["principal"], query))
                record.update(repeat=repeat, returned_items=len(result["payload"]["state_items"]))
                records.append(record)
            with db.engine.connect() as conn:
                data = dict(conn.execute(text("""SELECT count(*) AS revisions,
                    sum(pg_column_size(snapshot)) AS snapshot_stored_bytes,
                    sum(octet_length(snapshot::text)) AS snapshot_json_text_bytes,
                    max(octet_length(snapshot::text)) AS largest_snapshot_json_bytes,
                    sum(pg_column_size(selection)) AS selections_stored_bytes
                    FROM nf_revisions WHERE work_id=:work"""), {"work": world["world_ref"]["work_id"]}).mappings().one())
                pg_version = conn.scalar(text("SELECT version()"))
                settings = {name: conn.scalar(text("SHOW " + name)) for name in (
                    "max_connections", "shared_buffers", "fsync", "synchronous_commit")}
            grouped = []
            for count in args.subjects:
                samples = [r for r in records if r["operation"] == "context_compile" and r["subjects"] == count]
                grouped.append({"subjects": count, "samples": len(samples),
                    "median_seconds": statistics.median(r["elapsed_seconds"] for r in samples),
                    "sql_counts": sorted({r["sql"]["statements"] for r in samples}),
                    "dependency_insert_counts": sorted({r["sql"]["dependency_insert_statements"] for r in samples})})
            output = {"label": args.label, "execution_kind": "TEST_FIXTURE_REAL_POSTGRESQL",
                "recorded_at_utc": datetime.now(UTC).isoformat(),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "git_dirty_paths": subprocess.check_output(["git", "diff", "--name-only"], text=True).splitlines(),
                "source_sha256": {str(path): sha256(path.read_bytes()).hexdigest() for path in (
                    Path("benchmarks/context_profile.py"), Path("src/novel_factory/application/queries.py"),
                    Path("src/novel_factory/infrastructure/repository.py"))},
                "configuration": vars(args) | {"output": str(args.output)},
                "environment": {"platform": platform.platform(), "cpu_count": os.cpu_count(),
                    "cpu_models": sorted({line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")}),
                    "cgroup_cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip() if Path("/sys/fs/cgroup/cpu.max").exists() else None,
                    "postgres": pg_version, "settings": settings, "pool_size": 4, "max_overflow": 0},
                "dataset": {"entities": args.entities + 1, "genesis_relations": args.entities,
                    "accepted_world_events": args.history, "fixture_namespace": "nf-context-profile-v1", **data},
                "model_requests": 0, "model_cost": None,
                "host_exclusivity": "Shared development host; timings are observations, not a capacity SLA.",
                "cache_note": "Application result cache only; PostgreSQL/OS buffers are warm, not flushed.",
                "summary": grouped, "raw_samples": records}
            args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"label": args.label, "summary": grouped, "dataset": output["dataset"]}, ensure_ascii=False, indent=2))
        finally:
            db.close()


if __name__ == "__main__":
    main()
