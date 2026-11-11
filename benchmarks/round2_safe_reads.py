"""Measure complete B1/B2 operations on alternating, identically seeded PG17 clones.

Run as python benchmarks/round2_safe_reads.py --output evidence.json within
scripts/with_postgres.py. Every clone gets its own writable object store.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from benchmarks.scion_profile import Attribution, isolated_kernel, make_population, normalized_query
from novel_factory.application.derived_worker import DerivedWorldWorker
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import point, prepare_command, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from scion_paired import clone, drop, fresh_name
from scion_retcon_profile import prepare as prepare_repair


ROOT = Path(__file__).resolve().parents[1]
FILES = ("src/novel_factory/application/scion.py", "src/novel_factory/application/kernel.py",
         "src/novel_factory/application/derived_worker.py", "src/novel_factory/infrastructure/repository.py",
         "benchmarks/round2_safe_reads.py", "benchmarks/scion_profile.py")


def build_fixture(kernel, count):
    data = make_population(kernel, count, .8)
    world, ids = data["world"], data["ids"]
    reader = next(actor for actor in data["population"] if not actor["writer"])
    cmd = prepare_command(kernel, world["principal"], world["world_ref"], "RESOURCE_TRANSFER", {
        "from_account": ids["reserve"], "to_account": ids["recipient"], "resource_id": ids["grain"],
        "quantity": "1", "unit": "bag", "at": point(6)}, mode="DEPENDENCY_GUARDED", key="s2-guarded")
    fixture = prepare_repair(kernel, "history-retcon")
    repaired = run_command(kernel, fixture["principal"], fixture["fork"], "REPAIR_EVENT", fixture["args"],
                           reviewer=fixture["reviewer"], key="s2-repair",
                           task={"task_id": fixture["task_id"], "generation": fixture["generation"]})
    with kernel.db.uow(write=False) as repo:
        scope_count = len(repo.scope_map(world["world_ref"]["work_id"], world["world_ref"]["branch_id"]))
        revisions = len(repo.many("revisions", {"branch_id": fixture["fork"]["branch_id"]}, columns=["world_revision_id"]))
    return dict(world=world, reader=reader, command=cmd, repair=fixture, repaired=repaired["world_ref"],
                scope_count=scope_count, review_rows=revisions)


@contextmanager
def disposable_arm(base_url, template, root, source_objects):
    admin = create_engine(make_url(base_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = fresh_name()
    clone(admin, name, template)
    try:
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            store = Path(tmp) / "objects"
            shutil.copytree(source_objects, store)
            database = Database(make_url(base_url).set(database=name).render_as_string(hide_password=False), pool_size=8, max_overflow=0)
            try:
                yield Kernel(database, ObjectStore(store))
            finally:
                database.close()
    finally:
        drop(admin, name)
        admin.dispose()


def one_arm(kernel, fixture, enabled):
    principal = fixture["world"]["principal"]
    command, repair = fixture["command"], fixture["repair"]
    scion = kernel.scion
    scion.columns = scion.scopes = enabled
    scion.replay = False
    worker = DerivedWorldWorker(kernel)
    query = WorldQueryService(kernel)
    reader = fixture["reader"]
    summaries, samples = {}, {}
    with Attribution(kernel) as meter:
        # Prime the measured engine connection after the codec observer installs.
        with kernel.db.engine.connect() as conn:
            conn.scalar(text("SELECT 1"))
        detail = Counter()
        backend_start = {}
        def cpu_at(pid):
            # Timeslice runtime of this PostgreSQL backend in ns. The pid
            # comes from psycopg over the same local disposable connection.
            return int((Path("/proc") / str(pid) / "schedstat").read_text().split()[0])
        def checkout(dbapi_conn, record, proxy):
            if meter.active:
                pid = dbapi_conn.info.backend_pid
                if pid not in backend_start:
                    backend_start[pid] = cpu_at(pid)
        event.listen(kernel.db.engine, "checkout", checkout)
        def observe(conn, cursor, sql, params, context, many):
            if not meter.active or cursor.description is None:
                return
            s = " ".join(sql.lower().split())
            label = None
            if " from nf_scopes " in s and s.startswith("select "):
                label = "scopes"
            if " from nf_revisions " in s and s.startswith("select "):
                label = "revision"
            if label:
                detail[label + "_sql"] += 1
                detail[label + "_rows"] += max(0, cursor.rowcount)
        event.listen(kernel.db.engine, "after_cursor_execute", observe)
        def measure(label, fn):
            before = Counter(scion.counts())
            detail.clear()
            meter.reset()
            backend_start.clear()
            meter.active = True
            wall, cpu = time.perf_counter_ns(), time.process_time_ns()
            try:
                value = fn()
            finally:
                cpu_ns = time.process_time_ns() - cpu
                wall_ns = time.perf_counter_ns() - wall
                meter.active = False
            samples[label] = {"wall_ms": wall_ns / 1e6, "app_cpu_ms": cpu_ns / 1e6,
                              "pg_backend_cpu_ms": sum(cpu_at(pid) - ns for pid, ns in backend_start.items()) / 1e6,
                              "pg_connections_measured": len(backend_start),
                              "sql_calls": sum(meter.sql.values()), "selected_rows": meter.selected_rows,
                              "json_bytes_decoded": meter.json_stats["decode"]["bytes"],
                              "json_bytes_encoded": meter.json_stats["encode"]["bytes"],
                              "source_reads": dict(detail),
                              "fast_path_hits": dict(Counter(scion.counts()) - before)}
            return value
        try:
            # Same explicit warm-query policy for both arms. Remaining operations
            # are first business writes against a new independently cloned DB.
            query.query(reader["principal"], reader["state"])
            result = measure("fixed_query", lambda: query.query(reader["principal"], reader["state"]))
            summaries["fixed_query"] = fingerprint(normalized_query(result))
            result = measure("worker", lambda: worker.run_once("s2-worker"))
            assert result["consumed"] and result["activated"] and result["materialization"] == "STORED"
            summaries["worker"] = result["artifact_ref"]["sha256"]
            result = measure("promote", lambda: kernel.promote(repair["principal"], repair["old"],
                                                               fixture["repaired"], "s2-promote"))
            assert result == fixture["repaired"]
            summaries["promote"] = result
            # New idempotency key forces real certificate construction, then
            # use this exact issued certificate for the measured acceptance.
            cert = measure("prepare", lambda: kernel.prepare(principal, {
                **command["prepare_request"], "idempotency_key": "s2-measured-prepare"}))
            accept_request = {**command["request"], "acceptance_certificate_ref": cert}
            result = measure("accept", lambda: kernel.accept(principal, accept_request))
            assert kernel.accept(principal, accept_request) == result
            with kernel.db.uow(write=False) as repo:
                payload = kernel.artifact(repo, cert, fixture["world"]["world_ref"]["work_id"])["payload"]
                summaries["prepare"] = fingerprint({k: payload[k] for k in (
                    "read_scope_tokens", "write_scope_tokens", "base_world_ref", "validated_world_ref", "coverage_status",
                    "approval_bindings", "dependency_fingerprint")})
                rev = kernel.revision(repo, result["world_ref"])
                summaries["accept"] = {"state_hash": rev["state_hash"], "selection": rev["selection"],
                                       "scope_versions_hash": fingerprint(rev["scope_versions"])}
        finally:
            event.remove(kernel.db.engine, "after_cursor_execute", observe)
            event.remove(kernel.db.engine, "checkout", checkout)
    return samples, summaries


def run(args):
    base = os.environ["NF_TEST_DATABASE_URL"]
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        objects = root / "seed"
        with isolated_kernel(base, objects) as (kernel, seed_url):
            fixture = build_fixture(kernel, args.population)
            with kernel.db.engine.connect() as conn:
                pg = conn.scalar(text("SHOW server_version"))
            kernel.db.engine.dispose()
            evidence = {"postgres": pg, "pairs": args.pairs, "population": args.population,
                        "scope_count": fixture["scope_count"], "review_rows": fixture["review_rows"],
                        "affinity": sorted(os.sched_getaffinity(0)),
                        "pg_cpu_scope": "schedstat runtime of the PostgreSQL backends checked out by the measured operation, excluding background processes.",
                        "source_sha256": {p: sha256((ROOT / p).read_bytes()).hexdigest() for p in FILES},
                        "world_ref": fixture["world"]["world_ref"], "rows": [],
                        "measurement": "Single public business operation per dedicated PG17 clone; first pair discarded; alternating order. Warm fixed query, new worker/promotion/prepare/accept. Counts include verification and receipt work inside the operation."}
            oracle = None
            for pair in range(-1, args.pairs):
                for enabled in ((False, True) if pair % 2 == 0 else (True, False)):
                    with disposable_arm(base, make_url(seed_url).database, root, objects) as arm:
                        samples, summaries = one_arm(arm, fixture, enabled)
                    if oracle is None:
                        oracle = summaries
                    assert summaries == oracle, (pair, enabled, summaries, oracle)
                    if pair >= 0:
                        evidence["rows"].append({"pair": pair, "enabled": enabled, "operations": samples})
                print("completed pair", pair, flush=True)
            evidence["semantic_result_hash"] = fingerprint(oracle)
            args.output.write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pairs", default=9, type=int)
    parser.add_argument("--population", default=200, type=int)
    run(parser.parse_args())
