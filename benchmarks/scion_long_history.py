"""Build real compact resource histories and run the paired stage-A meter on each checkpoint.

The seed uses one ordinary task, proposal, certificate, and official acceptance
per event. Seed and probes use scoped, non-production PostgreSQL databases.
"""
from __future__ import annotations

import argparse
import dataclasses
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


def transfer(ids, index):
    source, target = ("left", "right") if index % 2 else ("right", "left")
    return {"from_account": ids[source], "to_account": ids[target], "resource_id": ids["grain"],
            "unit": "bag", "quantity": "1", "at": {"tick": index, "phase": 0}}


def create_fixture(kernel):
    from novel_factory.fixtures import point
    ids = {key: str(uuid5(NAMESPACE_URL, "nf-long-history/" + key)) for key in ("left", "right", "grain")}
    genesis = {"entities": {eid: {"entity_id": eid, "entity_kind": "RESOURCE" if key == "grain" else "ACCOUNT",
                                 "name": key, "created_at": point(0)} for key, eid in ids.items()},
               "resources": [{"account_id": ids["left"], "resource_id": ids["grain"], "quantity": "1", "unit": "bag"}]}
    world = kernel.bootstrap_work("TEST_ONLY: long history paired transfers", genesis)
    return ids, {"world_ref": world["world_ref"], "principal": world["principal"]}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def seed(args):
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from benchmarks.p2_behavioral_load import isolated_kernel
    from benchmarks.scion_profile import setup_task
    from novel_factory.domain.canonical import fingerprint
    from novel_factory.domain.reducer import replay
    from novel_factory.fixtures import run_command

    os.environ.update(NF_SCION_REPLAY="1", NF_SCION_COLUMNS="1", NF_SCION_SCOPES="1")
    out = args.output.resolve()
    objects = out / "seed_objects"
    objects.mkdir(parents=True, exist_ok=True)
    with isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], objects) as (kernel, url):
        ids, world = create_fixture(kernel)
        predicate = "resource:" + ids["grain"] + ":bag"
        start = time.perf_counter()
        for index in range(1, max(args.checkpoints) + 1):
            command = transfer(ids, index)
            previous = world["world_ref"]
            accepted = run_command(kernel, world["principal"], previous, "RESOURCE_TRANSFER", command,
                                   key=f"build:{index}", mode="DEPENDENCY_GUARDED")
            world["world_ref"] = accepted["world_ref"]
            if index % 25 == 0:
                print(f"ACCEPTED {index} elapsed={time.perf_counter()-start:.2f}", flush=True)
            if index not in args.checkpoints:
                continue
            with kernel.db.uow(write=False, repeatable_read=True) as repo:
                revision = kernel.revision(repo, world["world_ref"])
                ancestor = kernel.revision(repo, previous)
                selection = list(ancestor["selection"].values()) + [accepted["receipt"]["accepted_event_refs"][0]["event_version_id"]]
                events = {row["event_version_id"]: row["payload"] for row in repo.many("events", {"work_id": previous["work_id"]})}
                genesis = repo.one("genesis", {"work_id": previous["work_id"]})["payload"]
                assert len(events) == len(selection) == index == revision["branch_seq"]
                assert replay(genesis, [events[eid] for eid in selection]) == revision["snapshot"]
                assert fingerprint(revision["snapshot"]) == revision["state_hash"]
                assert repo.conn.scalar(text("select count(*) from nf_commits")) == index
                assert repo.conn.scalar(text("select count(*) from nf_revision_events")) == index * (index + 1) // 2
            before = time.perf_counter()
            next_command = transfer(ids, index + 1)
            task = setup_task(kernel, world, [ids["left"], ids["right"]], next_command["at"], [predicate])
            pg_url = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
            backup = out / f"{index}.dump"
            subprocess.run(["pg_dump", "-Fc", "-Z", "1", "--no-owner", "-f", str(backup), pg_url], check=True)
            fixture = {"world_ref": world["world_ref"], "principal": dataclasses.asdict(world["principal"]),
                       "request": task, "ids": ids, "state_hash": revision["state_hash"], "count": index,
                       "source_sha": args.candidate_sha, "seed_elapsed": before - start,
                       "checkpoint_sha256": sha(backup), "seed_method": "one official kernel acceptance per event"}
            path = out / f"{index}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(fixture))
            tmp.replace(path)
            print(f"CHECKPOINT {index}", flush=True)


def repair_trace(kernel, world, ids, count, experiment, meter):
    from contextlib import ExitStack
    from copy import deepcopy
    from functools import wraps
    from unittest.mock import patch

    from novel_factory.domain.canonical import fingerprint
    from novel_factory.fixtures import point, run_command

    principal, original = world["principal"], world["world_ref"]
    with kernel.db.uow(write=False) as repo:
        rows = repo.many("events", {"work_id": original["work_id"]})
        timeline = sorted((r["payload"] for r in rows), key=lambda e: e["occurrence_time"]["at"]["tick"])
    replacement = deepcopy(timeline[-1])
    previous = timeline[-2]
    replacement["causal_parents"] = [{k: previous[k] for k in ("occurrence_id", "event_version_id")}]
    replacement["event_version_id"] = str(uuid5(NAMESPACE_URL, replacement["occurrence_id"] + "/repair-event"))
    replacement["representation_id"] = str(uuid5(NAMESPACE_URL, replacement["occurrence_id"] + "/repair-representation"))
    branch = kernel.fork(principal, original, "REPAIR", "history-retcon")
    reviewer = kernel.issue_grant(principal, original["work_id"], role_id="control_operator",
                                  branch_ids=[branch["branch_id"]], capabilities=["READ", "APPROVE", "REPAIR"],
                                  planes=["WORLD"], max_story_time=point(count + 200))["principal"]
    steps = []
    for phase, template, args in [
        ("retcon", "REPAIR_EVENT", {"occurrence_id": replacement["occurrence_id"], "replacement_event": replacement,
                                    "reason": "Record the selected preceding source version as a causal parent."}),
        ("causal_append", "RESOURCE_TRANSFER", transfer(ids, count + 1)),
    ]:
        before = kernel.scion.counts() if hasattr(kernel, "scion") else {}
        def timed(name, original_method, phase=phase):
            @wraps(original_method)
            def invoke(*pos, **kw):
                value, _ = experiment.measure(meter, name, lambda: original_method(*pos, **kw),
                                               suite=phase, checkpoint=count)
                return value
            return invoke

        with ExitStack() as samples:
            for name in ("propose", "approve", "prepare", "accept"):
                samples.enter_context(patch.object(kernel, name, timed(name, getattr(kernel, name))))
            result = run_command(kernel, principal, branch, template, args,
                                 reviewer=reviewer, key="long-history:" + phase)
        receipt = result["receipt"]
        with kernel.db.uow(write=False) as repo:
            revision = kernel.revision(repo, receipt["world_ref"])
            assert revision["selection"][replacement["occurrence_id"]] == replacement["event_version_id"]
            assert revision["parent_revision_id"] == branch["world_revision_id"]
            assert kernel.head(repo, original["work_id"]) == original
            assert len(revision["selection"]) == count + int(phase == "causal_append")
            stored = repo.one("events", {"event_version_id": replacement["event_version_id"]})
            assert stored["payload"] == replacement and stored["effect_group_id"] == timeline[-1]["effect_group_id"]
        after = kernel.scion.counts() if hasattr(kernel, "scion") else {}
        if after:
            outcome = "historical_replacement" if phase == "retcon" else "complex_causality_or_time"
            assert after.get("replay." + outcome, 0) - before.get("replay." + outcome, 0) == 2
        steps.append({"phase": phase, "parent": branch, "world_ref": receipt["world_ref"],
                      "receipt_sha256": fingerprint(receipt), "state_hash": revision["state_hash"],
                      "manifest": revision["manifest_hash"], "scion_counts_before": before, "scion_counts_after": after})
        branch = receipt["world_ref"]
    return steps


def probe_child(message):
    import importlib
    import itertools
    from contextlib import ExitStack
    from datetime import datetime
    from unittest.mock import patch
    from uuid import UUID

    from sqlalchemy import text

    from benchmarks import scion_profile as profile
    from novel_factory.application.authorization import Principal
    from novel_factory.application.kernel import Kernel
    from novel_factory.domain.canonical import fingerprint
    from novel_factory.infrastructure.database import Database
    from novel_factory.infrastructure.objects import ObjectStore
    from novel_factory.infrastructure.repository import KernelRepository
    from novel_factory.orchestration.tasks import TaskService

    fixture = message["fixture"]
    count, ids = fixture["count"], fixture["ids"]
    world = {"principal": Principal(**fixture["principal"]), "world_ref": fixture["world_ref"]}
    kernel = Kernel(Database(message["url"], pool_size=8), ObjectStore(message["objects"]))
    now = datetime.fromisoformat(message["now"])
    output = Path(message["output"])
    params = argparse.Namespace(output=output, repeats=0, profiles=False)
    root = Path.cwd().resolve()
    source_files = {str(p.relative_to(root)): sha(p) for p in sorted((root / "src").rglob("*.py"))}
    assert Path(importlib.import_module(Kernel.__module__).__file__).resolve().is_relative_to(root)

    class FixedDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    def ids_for_module(name):
        stream = itertools.count()
        def next_uuid():
            identity = uuid5(NAMESPACE_URL, f"nf-history-probe/{count}/{name}/{next(stream)}")
            return UUID(bytes=identity.bytes, version=4)
        return next_uuid

    try:
        with ExitStack() as patches, profile.DatabaseObserver(message["url"]) as observer, profile.Attribution(kernel) as meter:
            if message.get("profile_proofs"):
                # Optional diagnostic only. Fingerprint timings include Python
                # timer overhead and must not be mixed with uninstrumented pairs.
                from novel_factory.application import replay_proofs
                for name in ("fingerprint", "_fallback_reason"):
                    function = getattr(replay_proofs, name)
                    meter._replace(replay_proofs, name, meter._wrap(function, "proof." + name))
                function = replay_proofs.world._append_verified_suffix
                meter._replace(replay_proofs.world, "_append_verified_suffix", meter._wrap(function, "proof.suffix"))
            experiment = profile.Experiment(params, kernel, observer)
            for name in ("benchmarks.scion_profile", "novel_factory.orchestration.tasks",
                         "novel_factory.application.kernel", "novel_factory.application.queries", "novel_factory.fixtures"):
                module = importlib.import_module(name)
                if hasattr(module, "uuid4"):
                    patches.enter_context(patch.object(module, "uuid4", ids_for_module(name)))
                if getattr(module, "datetime", None) is datetime:
                    patches.enter_context(patch.object(module, "datetime", FixedDate))
            # Preserve the real SQL round trips and return a paired business
            # clock for artifact hashes and identical certificate requests.
            def fixed_after(read):
                def clock(*args):
                    read(*args)
                    return now
                return clock
            patches.enter_context(patch.object(KernelRepository, "now", fixed_after(KernelRepository.now)))
            patches.enter_context(patch.object(TaskService, "_now", staticmethod(fixed_after(TaskService._now))))
            subjects, predicates = [ids["left"], ids["right"]], [f"resource:{ids['grain']}:bag"]
            experiment.dataset(world, "long-resource")
            counter = lambda: kernel.scion.counts() if hasattr(kernel, "scion") else {}
            steps = []
            if message.get("repair", False):
                steps = repair_trace(kernel, world, ids, count, experiment, meter)
            for offset, phase in enumerate(() if steps else ("cold", "warm"), 1):
                current = world["world_ref"]
                initial = counter()
                with ExitStack() as scope:
                    if phase == "cold":
                        scope.enter_context(patch.object(profile, "setup_task", return_value=fixture["request"]))
                    experiment.pipeline(meter, world, "RESOURCE_TRANSFER", transfer(ids, count + offset),
                                        subjects, predicates, suite=phase, checkpoint=count + offset - 1)
                with kernel.db.uow(write=False) as repo:
                    result = kernel.revision(repo, world["world_ref"])
                    assert result["parent_revision_id"] == current["world_revision_id"]
                    assert len(result["selection"]) == count + offset
                    assert repo.conn.scalar(text("SHOW server_version_num")).startswith("17")
                steps.append({"phase": phase, "parent": current, "world_ref": world["world_ref"],
                              "state_hash": result["state_hash"], "manifest": result["manifest_hash"],
                              "scion_counts_before": initial, "scion_counts_after": counter()})
            experiment.summarize()
        report = experiment.result
        report.update(steps=steps, source_sha=message["sha"], app_file_sha256=source_files,
                      fixture_sha256=fixture["checkpoint_sha256"], fixture_state_hash=fixture["state_hash"],
                      seed_seconds=fixture["seed_elapsed"], fixture_request_sha256=fingerprint(fixture["request"]),
                      current_cpu_affinity=sorted(os.sched_getaffinity(0)), flag_env=message["env"],
                      database_observer=observer.result(), status="COMPLETED_WITH_INVARIANTS",
                      paired_time=message["now"], proof_profile=message.get("profile_proofs", False),
                      harness_sha256=sha(Path(__file__)))
        raw = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode()
        output.with_suffix(".json.gz").write_bytes(gzip.compress(raw, mtime=0))
        output.unlink()
    finally:
        kernel.db.close()


def probe(args):
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    source = args.output.resolve()
    results = args.results.resolve()
    results.mkdir(parents=True, exist_ok=True)
    url = make_url(os.environ["NF_TEST_DATABASE_URL"])
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    flags = ("NF_SCION_COLUMNS", "NF_SCION_SCOPES", "NF_SCION_REPLAY")
    arms = {"A": (args.baseline_root, args.baseline_sha), "B": (args.candidate_root, args.candidate_sha)}
    try:
        for count in args.checkpoints:
            marker = source / f"{count}.json"
            while not marker.exists():
                time.sleep(2)
            fixture = json.loads(marker.read_bytes())
            backup = source / f"{count}.dump"
            assert fixture["checkpoint_sha256"] == sha(backup)
            template = "nf_long_template_" + uuid4().hex
            subprocess.run(["createdb", template], check=True)
            try:
                subprocess.run(["pg_restore", "--exit-on-error", "--no-owner", "--dbname", template, str(backup)], check=True)
                # All reference objects in this restore are immutable, even as
                # the seeder continues creating later worlds in its own cluster.
                base = create_engine(url.set(database=template))
                with base.connect() as conn:
                    objects = conn.execute(text("SELECT hash FROM nf_objects ORDER BY hash")).scalars().all()
                    now = datetime.now(UTC)
                    if args.b3_comparison:
                        # A restored historical checkpoint contains the original
                        # task lease. Use its authentic, in-lease business cut
                        # for both arms without modifying any stored deadline.
                        start, end = conn.execute(text(
                            "SELECT updated_at, LEAST(lease_expiry, deadline) FROM nf_tasks WHERE task_id = :task"
                        ), {"task": fixture["request"]["task_id"]}).one()
                        assert start < end
                        now = start + (end - start) / 2
                base.dispose()
                with tempfile.TemporaryDirectory() as temp:
                    template_store = Path(temp) / "source"
                    for value in objects:
                        path = Path("sha256", value[:2], value[2:])
                        original = source / "seed_objects" / path
                        assert sha(original) == value
                        dest = template_store / path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(original, dest)
                    now = now.isoformat()
                    for repeat in range(1, args.repetitions + 1):
                        pair = []
                        for arm in ("AB" if repeat % 2 else "BA"):
                            checkout, revision = arms[arm]
                            name = "nf_long_arm_" + uuid4().hex
                            with admin.connect() as conn:
                                conn.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
                            try:
                                store = Path(temp) / name
                                shutil.copytree(template_store, store, copy_function=os.link)
                                scenario = "long-retcon" if args.repair else "long-resource"
                                out = results / f"{scenario}-{count}-{arm}-{repeat}.json"
                                controls = {f: "1" if arm == "B" else "0" for f in flags}
                                if args.b3_comparison:
                                    controls = {f: "0" for f in flags}
                                    controls["NF_SCION_REPLAY"] = str(int(arm == "B" or args.b3_comparison == "legacy"))
                                    controls["NF_SCION_REPLAY_LAZY"] = str(int(arm == "B"))
                                msg = {"url": url.set(database=name).render_as_string(hide_password=False), "now": now,
                                       "output": str(out), "fixture": fixture, "sha": revision, "env": controls,
                                       "objects": str(store), "repair": args.repair, "profile_proofs": args.profile_proofs}
                                root = checkout.resolve()
                                print(f"PROBE {count} {arm} {repeat}", flush=True)
                                subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child"],
                                               input=json.dumps(msg), text=True, check=True, cwd=root,
                                               env={**os.environ, "PYTHONPATH": f"{root}/src:{root}", **controls})
                                raw = json.loads(gzip.decompress(out.with_suffix(".json.gz").read_bytes()))
                                pair.append(([{k: s[k] for k in ("phase", "parent", "world_ref", "state_hash", "manifest", "receipt_sha256") if k in s}
                                              for s in raw["steps"]],
                                             [r["receipt_sha256"] for r in raw["records"] if "receipt_sha256" in r]))
                            finally:
                                subprocess.run(["dropdb", "--force", name], check=True)
                        assert pair[0] == pair[1], "WorldRef, projection, manifest or receipt differs across arms"
            finally:
                subprocess.run(["dropdb", "--force", template], check=True)
    finally:
        admin.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Private seed checkpoints and object bytes")
    parser.add_argument("--checkpoints", type=int, nargs="+", default=[500, 1000])
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--repair", action="store_true", help="Paired Retcon plus causal follow-up (history <=200)")
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--baseline-sha")
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--profile-proofs", action="store_true", help="Time proof digests, guards and suffix separately")
    parser.add_argument("--b3-comparison", choices=("off", "legacy"),
                        help="Isolate revised B3 against full replay or original B3, with columns/scopes off")
    args = parser.parse_args()
    if args.probe:
        if not all((args.baseline_root, args.candidate_root, args.baseline_sha, args.results)):
            parser.error("probe requires both roots, source SHAs and a results directory")
        probe(args)
    else:
        seed(args)


if __name__ == "__main__":
    if sys.argv[1:] == ["--child"]:
        probe_child(json.load(sys.stdin))
    else:
        main()
