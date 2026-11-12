"""Compare four operations using a fresh disposable PostgreSQL database and the patched runtime."""
import argparse
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from sqlalchemy import text

from novel_factory.application.kernel import Kernel
from novel_factory.contracts import registry
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.world import replay
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def sample(invoke, repetitions):
    started = perf_counter_ns()
    for _ in range(repetitions):
        invoke()
    return (perf_counter_ns() - started) / repetitions / 1000


def paired(baseline, candidate, repetitions):
    if baseline() != candidate():
        raise AssertionError("Operation values differ")
    for function in (baseline, candidate):
        sample(function, 2)
    rows = []
    for repeat in range(12):
        row = {}
        pairs = [("baseline", baseline), ("candidate", candidate)]
        for label, function in (pairs if repeat % 2 == 0 else reversed(pairs)):
            row[label] = round(sample(function, repetitions), 3)
        rows.append(row)
    return {
        "returned_values_identical": True,
        "unit": "microseconds_per_operation",
        "operations_per_arm_per_pair": repetitions,
        "raw_pairs": rows,
        "baseline_median_us": median(row["baseline"] for row in rows),
        "candidate_median_us": median(row["candidate"] for row in rows),
    }


def paired_replay(kernel, world):
    kernel.scion.replay = True
    wr, ids = world["world_ref"], world["ids"]
    for tick in range(2, 10):
        base = wr
        args = {
            "from_account": ids["warehouse"], "to_account": ids["relief"],
            "resource_id": ids["grain"], "quantity": "1", "unit": "bag", "at": point(tick),
        }
        wr = run_command(kernel, world["principal"], wr, "RESOURCE_TRANSFER", args)["world_ref"]
    with kernel.db.uow(write=False) as repo:
        work = repo.one("works", {"work_id": wr["work_id"]})
        parent = kernel.revision(repo, base)
        child = kernel.revision(repo, wr)
        genesis = repo.one("genesis", {"work_id": wr["work_id"]})["payload"]
        events = {
            row["event_version_id"]: row["payload"]
            for row in repo.many("events", {"work_id": wr["work_id"]})
        }
        last, = set(child["selection"].values()) - set(parent["selection"].values())
        selected = [events[event_id] for event_id in (*parent["selection"].values(), last)]
    def baseline():
        return replay(genesis, selected)
    def candidate():
        return kernel.replay_proofs.evaluate(
            kernel.scion, genesis, selected, [selected[-1]], "RESOURCE_TRANSFER", parent, work,
            proposal=True,
        )
    assert fingerprint(baseline()) == child["state_hash"]
    hits = kernel.scion.counts().get("replay.hit", 0)
    result = paired(baseline, candidate, 10)
    result["verified_suffix_hits"] = kernel.scion.counts().get("replay.hit", 0) - hits
    assert result["verified_suffix_hits"] == 1 + 2 + 12 * 10
    result["event_count"] = len(selected)
    return result


def measure(db):
    with TemporaryDirectory() as objects:
        kernel = Kernel(db, ObjectStore(objects))
        world = load_fixture(kernel, "white_bridge")
        wr = world["world_ref"]
        keys = ["BENCH:" + str(i) for i in (2, 44, 150, 300)]
        with db.uow() as repo:
            repo.conn.execute(text("""
                INSERT INTO nf_scopes (work_id, branch_id, scope_key, version)
                SELECT :work, :branch, 'BENCH:'||i, 0 FROM generate_series(1,2048) AS i
            """), {"work": wr["work_id"], "branch": wr["branch_id"]})
        with db.uow(write=False, repeatable_read=True) as repo:
            columns = ("branch_seq", "state_hash", "manifest_hash")
            def metadata_full():
                row = repo.one("revisions", wr)
                return {key: row[key] for key in columns}
            def metadata_selected():
                return repo.one("revisions", wr, columns=columns)
            def scopes_full():
                scope = repo.scope_map(wr["work_id"], wr["branch_id"])
                return {key: scope[key] for key in keys}
            def scopes_selected():
                return repo.scope_versions_for(wr["work_id"], wr["branch_id"], keys)
            first = paired(metadata_full, metadata_selected, 10)
            second = paired(scopes_full, scopes_selected, 5)
            count = len(repo.scope_map(wr["work_id"], wr["branch_id"]))
            assert kernel.head(repo, wr["work_id"]) == wr
            server = repo.conn.scalar(text("SHOW server_version"))
        payload = json.loads(Path("contracts/examples/r1_fixed_owner_d25.json").read_text())
        def check_schema(flag):
            registry._STATIC_SCHEMA_LINKS = flag
            return registry.validate_contract("UnifiedQueryRequest", payload, "2.1")
        third = paired(lambda: check_schema(False), lambda: check_schema(True), 15)
        return {
            "measurement": "isolated_operations", "runtime": server, "base_git_sha":
            subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "scope_row_count": count, "selected_scopes": len(keys), "metadata_projection": first,
            "sparse_guard_read": second, "schema_positive_validation": third,
            "proved_suffix_replay": paired_replay(kernel, world), "provider_requests": 0,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json", type=Path)
    args = parser.parse_args()
    url = os.environ["NF_TEST_DATABASE_URL"]
    db = Database(url)
    try:
        with db.engine.connect() as conn:
            if conn.scalar(text("SELECT EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public')")):
                raise SystemExit("Benchmark requires an empty disposable database")
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True,
                       env={**os.environ, "NF_DATABASE_URL": url})
        args.json.write_text(json.dumps(measure(db), indent=2) + "\n")
    finally:
        db.close()
