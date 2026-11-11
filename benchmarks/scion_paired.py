"""Measure both application roots against separate PG17 clones of one trusted fixture.

Run the coordinator under scripts/with_postgres.py. Generated grants only pass via a
private subprocess pipe. The right-arm configuration JSON names its checkout,
commit SHA and experiment environment. Both arms use the unchanged stage-A meter
and the original service workload and capacity criteria.
"""
import argparse
import contextlib
import dataclasses
import gzip
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def install_worker_meter():
    """Count at the ASGI boundary before admission, with no request details."""
    from multiprocessing.util import Finalize
    from novel_factory.application.kernel import Kernel
    from novel_factory.interfaces.http import RequestBoundaryMiddleware
    samples = {"requests": 0, "first_ns": None, "last_ns": None}
    kernel_list = []
    old_call, old_init = RequestBoundaryMiddleware.__call__, Kernel.__init__

    async def measured(middleware, scope, receive, send):
        if scope["type"] == "http":
            now = time.monotonic_ns()
            if samples["first_ns"] is None:
                samples["first_ns"] = now
            samples["last_ns"] = now
            samples["requests"] += 1
        return await old_call(middleware, scope, receive, send)

    def capture(kernel, *args, **kwargs):
        old_init(kernel, *args, **kwargs)
        kernel_list.append(kernel)

    def save():
        samples["scion"] = [k.scion.counts() for k in kernel_list if hasattr(k, "scion")]
        target = Path(os.environ["NF_SCION_METER_DIR"]) / f"{os.getpid()}.json"
        target.write_text(json.dumps(samples))

    RequestBoundaryMiddleware.__call__ = measured
    Kernel.__init__ = capture
    Finalize(None, save, exitpriority=0)


def pg_cpu(pid):
    """Measure CPU of this disposable postmaster plus live and reaped children."""
    children = [pid]
    ticks = 0
    while children:
        current = children.pop()
        folder = Path("/proc") / str(current)
        try:
            fields = (folder / "stat").read_text().rsplit(")", 1)[1].split()
            ticks += sum(int(x) for x in fields[11:15])
            children.extend(int(x) for x in (folder / f"task/{current}/children").read_text().split())
        except FileNotFoundError:
            continue
    return ticks / os.sysconf("SC_CLK_TCK")


if __name__ == "__mp_main__" and os.environ.get("NF_SCION_METER_DIR"):
    install_worker_meter()


def fresh_name():
    return "nf_pair_" + uuid4().hex[:16]


def clone(admin, name, source):
    # Both identifiers come from PostgreSQL's database name or our UUID factory.
    quote = admin.dialect.identifier_preparer.quote_identifier
    with admin.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE {quote(name)} TEMPLATE {quote(source)}")


def drop(admin, name):
    with admin.connect() as conn:
        quote = admin.dialect.identifier_preparer.quote_identifier
        conn.exec_driver_sql(f"DROP DATABASE {quote(name)} WITH (FORCE)")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=encode,
                                    ensure_ascii=False).encode()).hexdigest()


def encode(obj):
    if dataclasses.is_dataclass(obj):
        return {"_fixture_principal": dataclasses.asdict(obj)}
    raise TypeError(type(obj).__name__)


def decode(obj):
    from novel_factory.application.authorization import Principal
    return Principal(**obj["_fixture_principal"]) if "_fixture_principal" in obj else obj


def prepare_statements(kernel, fixture, population=False):
    from benchmarks import scion_profile as profile
    from novel_factory.fixtures import point, entity_value
    world = fixture["world"]
    if population:
        actor = next(actor for actor in fixture["population"] if not actor["writer"])
        ids = fixture["ids"]
        action = {"from_account": ids["reserve"], "to_account": ids["recipient"],
                  "resource_id": ids["grain"], "quantity": "1", "unit": "bag", "at": point(6)}
        subjects = [action["from_account"], action["to_account"]]
        predicates = [f'resource:{ids["grain"]}:bag']
        principal, query, template = actor["principal"], actor["state"], "RESOURCE_TRANSFER"
    else:
        count, items, owner = fixture["history"], fixture["items"], fixture["owner"]
        subjects, predicates = [items[count % len(items)]], ["legal_owner"]
        action = {"subject_id": subjects[0], "predicate": "legal_owner",
                  "object_value": entity_value(owner), "at": point(3 + count)}
        query_task = profile.setup_task(kernel, world, items[:1], point(1), predicates)
        query = {key: query_task[key] for key in ("schema_version", "task_id", "generation",
                                                  "read_contract", "knowledge_cut", "subject_time")}
        query.update(query={"kind": "STATE", "subject_ids": items[:1], "predicates": predicates},
                     page_size=50, cursor=None)
        principal, template = world["principal"], "SET_RELATION"
    request = profile.setup_task(kernel, world, subjects, action["at"], predicates)
    return dict(world=world, query=query, read_principal=principal, request=request,
                subjects=subjects, predicates=predicates, template=template, action=action,
                count=4 if population else fixture["history"])


def prepare(kernel, case):
    from benchmarks import scion_profile as profile
    from novel_factory.fixtures import point, entity_value, run_command
    if case.startswith("history"):
        _, count, entities = case.split("-")
        count, entities = int(count), int(entities)
        world, items, owner = profile.make_world(kernel, entities)
        for index in range(count):
            result = run_command(kernel, world["principal"], world["world_ref"], "SET_RELATION",
                                 {"subject_id": items[index % len(items)], "predicate": "legal_owner",
                                  "object_value": entity_value(owner), "at": point(3 + index)},
                                 key=f"scion-history:{index}")
            world["world_ref"] = result["world_ref"]
        return prepare_statements(kernel, dict(world=world, items=items, owner=owner, history=count))
    kind, size = case.split("-")
    fixture = profile.make_population(kernel, int(size), .2 if kind == "mixed" else .8)
    if kind == "population":
        return prepare_statements(kernel, fixture, population=True)
    return fixture


def profile_arm(kernel, url, fixture, output, case):
    from benchmarks import scion_profile as profile
    args = argparse.Namespace(output=output, repeats=3, profiles=False)
    with profile.DatabaseObserver(url) as observer, profile.Attribution(kernel) as meter:
        exp = profile.Experiment(args, kernel, observer)
        world, checkpoint = fixture["world"], fixture["count"]
        exp.dataset(world, case)
        exp.reads(meter, {**world, "principal": fixture["read_principal"]}, fixture["query"],
                  suite=case, checkpoint=checkpoint)
        cold_counts = kernel.scion.counts() if hasattr(kernel, "scion") else {}
        with patch.object(profile, "setup_task", return_value=fixture["request"]):
            exp.pipeline(meter, world, fixture["template"], fixture["action"],
                         fixture["subjects"], fixture["predicates"], suite=case, checkpoint=checkpoint)
        after_cold = kernel.scion.counts() if hasattr(kernel, "scion") else {}
        # A real consecutive official commit reuses the same Kernel/DB process.
        # It uses the preceding real receipt and newly minted per-arm references.
        action = dict(fixture["action"])
        action["at"] = {**action["at"], "tick": action["at"]["tick"] + 1}
        exp.pipeline(meter, world, fixture["template"], action,
                     fixture["subjects"], fixture["predicates"], suite=case + "-chain", checkpoint=checkpoint + 1)
        if hasattr(kernel, "scion"):
            exp.result["scion_counter_snapshots"] = {"before": cold_counts, "after_cold": after_cold,
                                                    "after_chain": kernel.scion.counts()}
        exp.summarize()
        exp.result["status"] = "PROFILE_COMPLETE_WITH_INVARIANTS_CHECKED"
        if hasattr(kernel, "scion"):
            exp.result["scion_counters_after_measurement"] = kernel.scion.counts()
    exp.result["database_observer"] = observer.result()
    exp.save()


def child():
    from benchmarks import p2_behavioral_load as load
    from novel_factory.application.kernel import Kernel
    from novel_factory.infrastructure.database import Database
    from novel_factory.infrastructure.objects import ObjectStore
    msg = json.load(sys.stdin, object_hook=decode)
    fixture = msg["fixture"]
    case, output = msg["case"], Path(msg["output"])
    root = Path.cwd().resolve()
    sources = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
               for path in ("src", "benchmarks") for p in sorted((root / path).rglob("*.py"))}
    for mod in ("novel_factory.application.kernel", "novel_factory.domain.world",
                "benchmarks.p2_behavioral_load", "benchmarks.scion_profile"):
        assert Path(importlib.import_module(mod).__file__).resolve().is_relative_to(root)
    db = Database(msg["url"], pool_size=8, max_overflow=0)
    kernel = Kernel(db, ObjectStore(msg["objects"]))
    with db.engine.connect() as conn:
        pgdata = Path(conn.scalar(text("SHOW data_directory")))
    pg_pid = int((pgdata / "postmaster.pid").read_text().splitlines()[0])
    cpu_start = pg_cpu(pg_pid)
    meters = tempfile.TemporaryDirectory()
    os.environ["NF_SCION_METER_DIR"] = meters.name
    try:
        if case.startswith(("history-", "population-")):
            profile_arm(kernel, msg["url"], fixture, output, case)
        elif case.startswith(("mixed-", "hotspot-")):
            with patch.object(load, "make_population", return_value=fixture):
                result = load.run_benchmark(kernel, msg["url"], agents=len(fixture["population"]),
                    hot_fraction=.2 if case.startswith("mixed-") else .8,
                    profile=case.split("-")[0], arrival_rps=10, open_loop_rps=1000, rounds=5,
                    max_attempts=5, retry_budget_seconds=30, service_mode="isolated-process",
                    api_processes=2 if case == "mixed-500" else 1)
            output.write_text(json.dumps(result))
        else:
            from benchmarks import p3_open_arrival, p3_stability
            open_arrival = case.startswith("open-")
            module = p3_open_arrival if open_arrival else p3_stability
            flags = ["--rounds", "5", "--arrival-rps", "1000"] if open_arrival else ["--seconds", "600", "--read-rps", "20"]
            @contextlib.contextmanager
            def fixed_kernel(url, dest):
                shutil.copytree(msg["objects"], dest, dirs_exist_ok=True)
                yield kernel, msg["url"]
            with patch.object(module, "make_population", return_value=fixture), patch.object(
                    module, "isolated_kernel", fixed_kernel), patch.object(sys, "argv", [
                    module.__name__, "--agents", str(len(fixture["population"])), "--api-processes", "2",
                    *flags, "--output", str(output)]):
                module.main()  # A reported NOT_MET is still a completed measurement.
    finally:
        db.close()
    content = json.loads(output.read_bytes())
    workers = [json.loads(path.read_bytes()) for path in Path(meters.name).glob("*.json")]
    meters.cleanup()
    live = [row for row in workers if row["requests"]]
    span = (max(row["last_ns"] for row in live)-min(row["first_ns"] for row in live)) / 1e9 if live else 0
    nr = sum(row["requests"] for row in workers)
    content["service_boundary"] = {"workers": workers, "count": nr, "first_to_last_seconds": span,
                                  "arrivals_per_second": (nr - 1)/span if span else None}
    content["pg_cluster_cpu_seconds"] = pg_cpu(pg_pid) - cpu_start
    content["paired_inputs"] = {"case": case, "fixture_sha256": msg["fixture_sha256"],
                                 "source_sha": msg["sha"], "source_files": sources,
                                 "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                 "fixture_method": "PG17 CREATE DATABASE TEMPLATE plus exact object directory copy",
                                 "experiment_env": msg["flags"],
                                 "cpu_affinity": sorted(os.sched_getaffinity(0))}
    # Lossless gzip, deterministic header. No tokens or request payloads in the results.
    output.with_suffix(".json.gz").write_bytes(gzip.compress(json.dumps(content, separators=(",", ":"),
                                              ensure_ascii=False).encode(), mtime=0))
    output.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    args = parser.parse_args()
    from benchmarks.p2_behavioral_load import isolated_kernel
    from novel_factory.infrastructure.objects import ObjectStore
    base_url = os.environ["NF_TEST_DATABASE_URL"]
    admin = create_engine(make_url(base_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    baseline = {"root": str(Path.cwd()), "sha": args.baseline_sha, "env": {}}

    def measure(job, arm, settings, run):
        case, db_name, objects, fixture, fixture_sha = job
        name = fresh_name()
        clone(admin, name, db_name)
        url = make_url(base_url).set(database=name).render_as_string(hide_password=False)
        try:
            with tempfile.TemporaryDirectory() as work:
                root = Path(settings["root"]).resolve()
                target = shutil.copytree(objects, Path(work) / "objects")
                output = args.output / f"{case}-{arm}-{run}.json"
                msg = dict(case=case, fixture=fixture, fixture_sha256=fixture_sha,
                           sha=settings["sha"], url=url, objects=str(target), output=str(output),
                           flags=settings["env"])
                env = dict(os.environ, PYTHONPATH=f"{root}/src:{root}")
                env.update(settings["env"])
                print(f"START {case} {arm} {run}", flush=True)
                subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child"],
                               cwd=root, env=env, input=json.dumps(msg, default=encode),
                               text=True, check=True)
                print(f"DONE {case} {arm} {run}", flush=True)
        finally:
            drop(admin, name)

    with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as clean:
        jobs = []
        for case in args.cases:
            objects = Path(temporary) / case
            objects.mkdir()
            print(f"FIXTURE {case}", flush=True)
            with isolated_kernel(base_url, objects) as (kernel, url):
                fixture = prepare(kernel, case)
                kernel.db.close()
                snapshot = fresh_name()
                clone(admin, snapshot, make_url(url).database)
                clean.callback(drop, admin, snapshot)
            job = (case, snapshot, objects, fixture, digest(fixture))
            jobs.append(job)
            measure(job, "A", baseline, 1)
            if args.candidate.is_file():
                right = json.loads(args.candidate.read_bytes())
                measure(job, "B", right, 1)
                for run in range(2, args.repetitions + 1):
                    for arm, config in ([("B", right), ("A", baseline)] if run%2==0 else [("A", baseline), ("B", right)]):
                        measure(job, arm, config, run)
        while not args.candidate.is_file():
            time.sleep(2)
        right = json.loads(args.candidate.read_bytes())
        for job in jobs:
            if not (args.output / f"{job[0]}-B-1.json.gz").is_file():
                measure(job, "B", right, 1)
                for run in range(2, args.repetitions + 1):
                    for arm, config in ([("B", right), ("A", baseline)] if run%2==0 else [("A", baseline), ("B", right)]):
                        measure(job, arm, config, run)
    admin.dispose()


if __name__ == "__main__":
    if sys.argv[1:] == ["--child"]:
        child()
    else:
        main()
